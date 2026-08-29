"""IncidentPilot ops console.

FastAPI + htmx UI over Robusta (incident feed via webhook sink) and
HolmesGPT (investigations via its /api/chat HTTP API).
"""

import json
import os
import re
import sqlite3
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import httpx
import markdown as md_lib
import nh3
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).parent
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "incidents.db"))
HOLMES_URL = os.environ.get("HOLMES_URL", "http://localhost:10001")
HOLMES_MODEL = os.environ.get("HOLMES_MODEL", "nemotron-fast")
CLUSTER_NAME = os.environ.get("CLUSTER_NAME", "incidentpilot-dev")
K8S_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")

app = FastAPI(title="IncidentPilot Console")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
# project docs (architecture overview, screenshots); /docs is FastAPI's swagger
app.mount("/project-docs", StaticFiles(directory=BASE_DIR.parent / "docs"),
          name="project-docs")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS incidents (
            id TEXT PRIMARY KEY,
            title TEXT, description TEXT, severity TEXT,
            namespace TEXT, resource TEXT, kind TEXT,
            status TEXT DEFAULT 'firing',
            investigation TEXT,
            raw TEXT,
            created_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS providers (
            provider TEXT PRIMARY KEY,
            api_key TEXT,
            model TEXT,
            active INTEGER DEFAULT 0,
            status TEXT,            -- last test outcome: ok / error text
            latency_ms INTEGER,
            last_ok TEXT,           -- timestamp of last successful test
            models_json TEXT,       -- models discovered on last test
            usage_json TEXT         -- quota info if the provider exposes it
        )"""
    )
    return conn


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.update(cluster_name=CLUSTER_NAME)
    return templates.TemplateResponse(request, name, ctx)


# ── incident feed ────────────────────────────────────────────────────

@app.post("/api/ingest")
async def ingest(request: Request):
    """Receiver for Robusta's webhook sink (format: json)."""
    payload = await request.json()
    subject = payload.get("subject") or {}
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO incidents"
            " (id, title, description, severity, namespace, resource, kind, raw, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                payload.get("id") or str(uuid.uuid4()),
                payload.get("title") or "(untitled finding)",
                payload.get("description") or "",
                payload.get("severity") or "INFO",
                subject.get("namespace"),
                subject.get("name"),
                subject.get("kind"),
                json.dumps(payload),
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            ),
        )
    return {"ok": True}


def fetch_incidents():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM incidents ORDER BY created_at DESC LIMIT 200"
        ).fetchall()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    try:
        cards = cluster_overview()
    except Exception:
        cards = []
    return render(request, "home.html", page="home", cards=cards)


@app.get("/partials/namespaces", response_class=HTMLResponse)
def namespace_cards(request: Request):
    try:
        cards = cluster_overview()
    except Exception:
        cards = []
    return render(request, "_ns_cards.html", cards=cards)


@app.get("/incidents", response_class=HTMLResponse)
def index(request: Request):
    return render(request, "index.html", page="incidents", incidents=fetch_incidents())


@app.get("/partials/incidents", response_class=HTMLResponse)
def incident_rows(request: Request):
    return render(request, "_incident_rows.html", incidents=fetch_incidents())


@app.get("/incidents/{incident_id}", response_class=HTMLResponse)
def incident_detail(request: Request, incident_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
    if row is None:
        return HTMLResponse("incident not found", status_code=404)
    i = dict(row)
    i["raw_pretty"] = json.dumps(json.loads(i["raw"] or "{}"), indent=2)
    if i.get("investigation"):
        i["investigation_html"] = render_ai(i["investigation"])
    return render(request, "detail.html", page="incidents", i=i)


# ── AI provider settings ─────────────────────────────────────────────
# The user manages their own provider API keys on the /settings page.
# Keys are stored in the console DB and, when a provider is activated,
# written into Holmes' model_list ConfigMap (plaintext in both — same
# trust level as the existing Robusta install; noted in the UI).

PROVIDERS = {
    "openrouter": {
        "label": "OpenRouter",
        "endpoint": "https://openrouter.ai/api/v1",
        "models_url": "https://openrouter.ai/api/v1/models",
        "litellm": lambda m: {"model": f"openrouter/{m}"},
        "default_model": "anthropic/claude-sonnet-4.5",
        "has_usage_api": True,
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "endpoint": "https://api.anthropic.com/v1",
        "models_url": "https://api.anthropic.com/v1/models",
        "litellm": lambda m: {"model": f"anthropic/{m}"},
        "default_model": "claude-sonnet-4-5",
        "has_usage_api": False,
    },
    "openai": {
        "label": "OpenAI (ChatGPT)",
        "endpoint": "https://api.openai.com/v1",
        "models_url": "https://api.openai.com/v1/models",
        "litellm": lambda m: {"model": f"openai/{m}"},
        "default_model": "gpt-4o",
        "has_usage_api": False,
    },
    "gemini": {
        "label": "Google Gemini",
        "endpoint": "https://generativelanguage.googleapis.com",
        "models_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "litellm": lambda m: {"model": f"gemini/{m}"},
        "default_model": "gemini-2.5-flash",
        "has_usage_api": False,
    },
    "nvidia": {
        "label": "NVIDIA NIM",
        "endpoint": "https://integrate.api.nvidia.com/v1",
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        "litellm": lambda m: {"model": f"openai/{m}",
                              "api_base": "https://integrate.api.nvidia.com/v1"},
        "default_model": "nvidia/nemotron-3.5-lightning-30b-a3b",
        "has_usage_api": False,
    },
}


def provider_auth(provider: str, api_key: str) -> dict:
    if provider == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    if provider == "gemini":
        return {"x-goog-api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def test_provider(provider: str, api_key: str) -> dict:
    """Validate the key by listing the provider's models; measure latency."""
    import time as _t
    p = PROVIDERS[provider]
    t = _t.time()
    try:
        r = httpx.get(p["models_url"], headers=provider_auth(provider, api_key),
                      timeout=15)
        latency = int((_t.time() - t) * 1000)
        if r.status_code in (401, 403):
            return {"ok": False, "latency_ms": latency,
                    "error": f"API key rejected (HTTP {r.status_code})"}
        r.raise_for_status()
        body = r.json()
        raw = body.get("data") or body.get("models") or []
        models = sorted((m.get("id") or m.get("name", "")).removeprefix("models/")
                        for m in raw)
        return {"ok": True, "latency_ms": latency, "models": models}
    except Exception as e:
        return {"ok": False, "latency_ms": int((_t.time() - t) * 1000),
                "error": str(e)[:200]}


def fetch_provider_usage(provider: str, api_key: str) -> dict | None:
    """Quota/usage where the provider has an API for it (only OpenRouter)."""
    if not PROVIDERS[provider]["has_usage_api"]:
        return None
    try:
        r = httpx.get("https://openrouter.ai/api/v1/auth/key",
                      headers=provider_auth(provider, api_key), timeout=15)
        r.raise_for_status()
        d = r.json().get("data", {})
        return {k: d.get(k) for k in
                ("usage", "limit", "limit_remaining", "is_free_tier", "rate_limit")}
    except Exception as e:
        return {"error": str(e)[:150]}


HOLMES_CONFIGMAP = "custom-toolsets-configmap"


def activate_in_holmes(provider: str, api_key: str, model: str) -> str:
    """Write the chosen model into Holmes' model list and restart Holmes.

    Returns the model alias. NOTE: a later `helm upgrade` overwrites the
    ConfigMap — mirror the entry into Helm values to make it permanent.
    """
    import yaml
    alias = f"console-{provider}"
    out = subprocess.run(
        ["kubectl", "get", "configmap", HOLMES_CONFIGMAP, "-n", "default",
         "-o", "jsonpath={.data.model_list\\.yaml}"],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    model_list = yaml.safe_load(out.stdout) or {}
    entry = dict(PROVIDERS[provider]["litellm"](model))
    entry["api_key"] = api_key
    entry["temperature"] = 0
    model_list[alias] = entry
    patch = json.dumps({"data": {"model_list.yaml": yaml.safe_dump(model_list)}})
    out = subprocess.run(
        ["kubectl", "patch", "configmap", HOLMES_CONFIGMAP, "-n", "default",
         "--type", "merge", "-p", patch],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    subprocess.run(
        ["kubectl", "rollout", "restart", "deployment/robusta-holmes",
         "-n", "default"],
        capture_output=True, text=True, timeout=30,
    )
    return alias


def active_model() -> str:
    """Model alias for Holmes calls: the activated provider, else default."""
    with db() as conn:
        row = conn.execute(
            "SELECT provider FROM providers WHERE active = 1"
        ).fetchone()
    return f"console-{row['provider']}" if row else HOLMES_MODEL


# ── AI response rendering ────────────────────────────────────────────

# words that get a colored status chip when they appear in AI output
BADGES = {
    "ok": ("Healthy", "Running", "Online", "Ready", "Connected", "Succeeded"),
    "warn": ("Warning", "Degraded", "Pending", "Unknown"),
    "err": ("Error", "Critical", "Unhealthy", "Failed", "CrashLoopBackOff",
            "ImagePullBackOff", "ErrImagePull", "OOMKilled", "Offline"),
}
BADGE_RE = re.compile(
    r"\b(" + "|".join(w for ws in BADGES.values() for w in ws) + r")\b"
)
BADGE_CLASS = {w: cls for cls, ws in BADGES.items() for w in ws}

MD_STYLE_HINT = (
    "\n\nFormat the answer in Markdown for a dashboard: start with one bold "
    "one-line summary, use a table when listing pods/resources (columns like "
    "namespace, pod, status, reason, action), bullet lists for steps, and "
    "`inline code` for names. Be compact — scannable in seconds."
)


def render_ai(text: str) -> str:
    """Markdown -> sanitized HTML with status badges, for AI responses."""
    html = md_lib.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    html = nh3.clean(html, attributes={"a": {"href"}, "span": {"class"},
                                       "code": {"class"}, "th": {"align"},
                                       "td": {"align"}})
    html = BADGE_RE.sub(
        lambda m: f'<span class="chip {BADGE_CLASS[m.group(1)]}">{m.group(1)}</span>',
        html,
    )
    return f'<div class="ai-md">{html}</div>'


# ── HolmesGPT ────────────────────────────────────────────────────────

def ask_holmes(question: str) -> str:
    resp = httpx.post(
        f"{HOLMES_URL}/api/chat",
        json={"ask": question, "model": active_model()},
        timeout=httpx.Timeout(600, connect=10),
    )
    resp.raise_for_status()
    return resp.json().get("analysis", "(no analysis returned)")


@app.post("/incidents/{incident_id}/investigate", response_class=HTMLResponse)
def investigate(incident_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
    if row is None:
        return HTMLResponse("incident not found", status_code=404)
    question = (
        f"Investigate this alert and explain root cause and a suggested fix.\n"
        f"Alert: {row['title']}\n"
        f"Severity: {row['severity']}\n"
        f"Resource: {row['kind']} {row['namespace']}/{row['resource']}\n"
        f"Details: {row['description'] or '(none)'}" + MD_STYLE_HINT
    )
    try:
        analysis = ask_holmes(question)
    except Exception as e:
        return HTMLResponse(f'<pre class="action-err">Holmes error: {e}</pre>')
    with db() as conn:
        conn.execute(
            "UPDATE incidents SET investigation = ? WHERE id = ?",
            (analysis, incident_id),
        )
    return HTMLResponse(render_ai(analysis))


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    return render(request, "chat.html", page="chat")


@app.post("/api/chat", response_class=HTMLResponse)
def chat(ask: str = Form(...)):
    try:
        body = render_ai(ask_holmes(ask + MD_STYLE_HINT))
    except Exception as e:
        body = f'<div class="action-err">Holmes error: {escape(str(e))}</div>'
    return HTMLResponse(
        f'<div class="chat-entry"><div class="q">You: {escape(ask)}</div>{body}</div>'
    )


# ── namespace dashboard ──────────────────────────────────────────────

# plain-language translations of Kubernetes error reasons
SIMPLE_ERRORS = {
    "CrashLoopBackOff": "App keeps crashing right after starting — check its logs; usually bad config, missing dependency, or an unreachable database.",
    "ImagePullBackOff": "Kubernetes can't download the container image — image name/tag is wrong or the registry is unreachable.",
    "ErrImagePull": "Kubernetes can't download the container image — image name/tag is wrong or the registry is unreachable.",
    "OOMKilled": "The app used more memory than its limit and was killed — raise the memory limit or fix a leak.",
    "Error": "The app exited with an error — check its logs for the reason.",
    "Completed": "The container finished and exited — normal for jobs, a problem for servers.",
    "ContainerCreating": "Still starting up — waiting for image/volumes. Only a problem if stuck for minutes.",
    "Pending": "Pod can't be scheduled — usually not enough CPU/memory free on the nodes.",
}


def humanize_age(start_iso: str | None) -> str:
    if not start_iso:
        return "—"
    delta = datetime.now(timezone.utc) - datetime.fromisoformat(
        start_iso.replace("Z", "+00:00")
    )
    secs = int(delta.total_seconds())
    if secs < 120:
        return f"{secs}s"
    if secs < 7200:
        return f"{secs // 60}m"
    if secs < 172800:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d"


def parse_pod(item: dict) -> dict:
    statuses = item.get("status", {}).get("containerStatuses", [])
    restarts = sum(s.get("restartCount", 0) for s in statuses)
    ready = sum(1 for s in statuses if s.get("ready"))
    # find the current problem reason, if any
    reason = None
    for s in statuses:
        state = s.get("state", {})
        if "waiting" in state:
            reason = state["waiting"].get("reason")
        elif "terminated" in state:
            reason = state["terminated"].get("reason")
        if reason:
            break
    phase = item["status"].get("phase", "Unknown")
    if reason is None and phase == "Pending":
        reason = "Pending"
    started = item["status"].get("startTime")
    # uptime of the current run = newest container start (resets on restart)
    run_starts = [
        s["state"]["running"]["startedAt"]
        for s in statuses if "running" in s.get("state", {})
    ]
    healthy = phase == "Running" and ready == len(statuses) and not reason
    return {
        "name": item["metadata"]["name"],
        "namespace": item["metadata"]["namespace"],
        "phase": phase,
        "ready": f"{ready}/{len(statuses)}",
        "restarts": restarts,
        "started": started,
        "age": humanize_age(started),
        "uptime": humanize_age(max(run_starts)) if run_starts else "—",
        "images": [s.get("image", "?") for s in statuses],
        "reason": reason,
        "simple_error": SIMPLE_ERRORS.get(
            reason, f"Problem state: {reason}" if reason else None
        ),
        "healthy": healthy,
    }


def kubectl_pods(*args: str) -> list[dict]:
    out = subprocess.run(
        ["kubectl", "get", "pods", *args, "-o", "json"],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return [parse_pod(i) for i in json.loads(out.stdout).get("items", [])]


def namespace_pods(namespace: str) -> list[dict]:
    return kubectl_pods("-n", namespace)


SYSTEM_NS_PREFIXES = ("kube-", "local-path-storage")


def cluster_overview() -> list[dict]:
    """Group all pods by namespace with rollup health for the card grid."""
    groups: dict[str, list[dict]] = {}
    for pod in kubectl_pods("-A"):
        if pod["namespace"].startswith(SYSTEM_NS_PREFIXES):
            continue
        groups.setdefault(pod["namespace"], []).append(pod)

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    with db() as conn:
        incident_counts = dict(conn.execute(
            "SELECT namespace, COUNT(*) FROM incidents"
            " WHERE created_at > ? AND namespace IS NOT NULL GROUP BY namespace",
            (cutoff,),
        ).fetchall())

    cards = []
    severity_rank = {"crit": 0, "warn": 1, "ok": 2}
    for ns, pods in sorted(groups.items()):
        healthy = sum(1 for p in pods if p["healthy"])
        reasons = {p["reason"] for p in pods if p["reason"]}
        if healthy == len(pods):
            status, status_label = "ok", "Healthy"
        elif reasons & {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "OOMKilled"}:
            status, status_label = "crit", "Critical"
        else:
            status, status_label = "warn", "Degraded"
        started = [p["started"] for p in pods if p["started"]]
        run_uptimes = [p["uptime"] for p in pods if p["uptime"] != "—"]
        cards.append({
            "name": ns,
            "status": status,
            "status_label": status_label,
            "healthy": healthy,
            "total": len(pods),
            "restarts": sum(p["restarts"] for p in pods),
            "incidents_24h": incident_counts.get(ns, 0),
            "live_since": humanize_age(min(started)) if started else "—",
            "images": len({img for p in pods for img in p["images"]}),
            "problem": next(iter(reasons), None),
        })
    cards.sort(key=lambda c: (severity_rank[c["status"]], c["name"]))
    return cards


@app.get("/ns/{namespace}", response_class=HTMLResponse)
def ns_dashboard(request: Request, namespace: str):
    if not K8S_NAME.match(namespace):
        return HTMLResponse("invalid namespace", status_code=400)
    try:
        pods = namespace_pods(namespace)
    except Exception as e:
        return HTMLResponse(f"<pre>kubectl error: {escape(str(e))}</pre>", status_code=502)
    with db() as conn:
        incidents = conn.execute(
            "SELECT * FROM incidents WHERE namespace = ? ORDER BY created_at DESC LIMIT 20",
            (namespace,),
        ).fetchall()
    return render(
        request, "namespace.html", page="incidents", namespace=namespace,
        pods=pods,
        healthy=sum(1 for p in pods if p["healthy"]),
        total_restarts=sum(p["restarts"] for p in pods),
        incidents=incidents,
    )


# ── logs ─────────────────────────────────────────────────────────────

LOG_LEVELS = (
    ("err", ("error", "fatal", "panic", "exception", "traceback", "fail")),
    ("warn", ("warn", "warning", "deprecat")),
)


def fetch_logs(namespace: str, pod: str, previous: bool = False) -> str:
    cmd = ["kubectl", "logs", "-n", namespace, pod, "--tail=500", "--timestamps"]
    if previous:
        cmd.append("--previous")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return out.stdout


def highlight_logs(raw: str) -> str:
    """Wrap each line in a span classed by log level for terminal colors."""
    lines = []
    for line in raw.splitlines():
        low = line.lower()
        cls = "info"
        for level, needles in LOG_LEVELS:
            if any(n in low for n in needles):
                cls = level
                break
        lines.append(f'<span class="log-{cls}">{escape(line)}</span>')
    return "\n".join(lines) or '<span class="log-info">(no log output)</span>'


@app.get("/ns/{namespace}/logs", response_class=HTMLResponse)
def logs_page(request: Request, namespace: str, pod: str = ""):
    if not K8S_NAME.match(namespace):
        return HTMLResponse("invalid namespace", status_code=400)
    try:
        pods = namespace_pods(namespace)
    except Exception as e:
        return HTMLResponse(f"<pre>kubectl error: {escape(str(e))}</pre>", status_code=502)
    pod_names = [p["name"] for p in pods]
    if not pod and pod_names:
        pod = pod_names[0]
    return render(request, "logs.html", page="incidents", namespace=namespace,
                  pod=pod, pod_names=pod_names)


@app.get("/partials/logs/{namespace}/{pod}", response_class=HTMLResponse)
def logs_partial(namespace: str, pod: str, previous: int = 0):
    if not (K8S_NAME.match(namespace) and K8S_NAME.match(pod)):
        return HTMLResponse("invalid name", status_code=400)
    try:
        raw = fetch_logs(namespace, pod, previous=bool(previous))
    except Exception as e:
        return HTMLResponse(f'<span class="log-err">cannot fetch logs: {escape(str(e))}</span>')
    return HTMLResponse(highlight_logs(raw))


@app.post("/api/summarize-logs", response_class=HTMLResponse)
def summarize_logs(namespace: str = Form(...), pod: str = Form(...)):
    if not (K8S_NAME.match(namespace) and K8S_NAME.match(pod)):
        return HTMLResponse("invalid name", status_code=400)
    try:
        raw = fetch_logs(namespace, pod)
    except Exception:
        raw = ""
        try:  # crashed pods often only have logs in the previous container
            raw = fetch_logs(namespace, pod, previous=True)
        except Exception as e:
            return HTMLResponse(f'<div class="action-err">cannot fetch logs: {escape(str(e))}</div>')
    tail = "\n".join(raw.splitlines()[-200:])
    question = (
        f"Below are the last log lines of pod {namespace}/{pod}. Do not use any tools. "
        "Explain them for a developer in a few seconds of reading. Answer in exactly "
        "these sections: **Summary** (2-3 plain-English sentences), **Key events** "
        "(grouped, not line-by-line), **Warnings**, **Errors** (with likely cause), "
        "**Recommended actions** (concrete next steps). Skip noise and repetition. "
        "If a section is empty write 'none'.\n\nLOGS:\n" + tail
    )
    try:
        analysis = ask_holmes(question)
    except Exception as e:
        return HTMLResponse(f'<div class="action-err">Holmes error: {escape(str(e))}</div>')
    return HTMLResponse(render_ai(analysis))


# ── settings page ────────────────────────────────────────────────────

def mask_key(key: str | None) -> str:
    if not key:
        return ""
    return "•••• " + key[-4:] if len(key) > 8 else "••••"


def settings_rows() -> list[dict]:
    with db() as conn:
        saved = {r["provider"]: dict(r) for r in
                 conn.execute("SELECT * FROM providers").fetchall()}
    rows = []
    for pid, meta in PROVIDERS.items():
        row = saved.get(pid, {})
        rows.append({
            "id": pid, **meta,
            "configured": bool(row.get("api_key")),
            "masked_key": mask_key(row.get("api_key")),
            "model": row.get("model") or meta["default_model"],
            "active": bool(row.get("active")),
            "status": row.get("status"),
            "latency_ms": row.get("latency_ms"),
            "last_ok": row.get("last_ok"),
            "models": json.loads(row.get("models_json") or "[]"),
            "usage": json.loads(row.get("usage_json") or "null"),
        })
    return rows


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return render(request, "settings.html", page="settings",
                  providers=settings_rows())


def _provider_card(request: Request, pid: str) -> HTMLResponse:
    row = next(r for r in settings_rows() if r["id"] == pid)
    return render(request, "_provider_card.html", p=row)


@app.post("/settings/{pid}/save", response_class=HTMLResponse)
def settings_save(request: Request, pid: str, api_key: str = Form(""),
                  model: str = Form("")):
    if pid not in PROVIDERS:
        return HTMLResponse("unknown provider", status_code=404)
    with db() as conn:
        existing = conn.execute(
            "SELECT api_key FROM providers WHERE provider = ?", (pid,)
        ).fetchone()
        key = api_key.strip() or (existing["api_key"] if existing else "")
        conn.execute(
            "INSERT INTO providers (provider, api_key, model) VALUES (?,?,?)"
            " ON CONFLICT(provider) DO UPDATE SET api_key = ?, model = ?",
            (pid, key, model, key, model),
        )
    return _provider_card(request, pid)


@app.post("/settings/{pid}/delete", response_class=HTMLResponse)
def settings_delete(request: Request, pid: str):
    with db() as conn:
        conn.execute("DELETE FROM providers WHERE provider = ?", (pid,))
    return _provider_card(request, pid)


@app.post("/settings/{pid}/test", response_class=HTMLResponse)
def settings_test(request: Request, pid: str):
    if pid not in PROVIDERS:
        return HTMLResponse("unknown provider", status_code=404)
    with db() as conn:
        row = conn.execute(
            "SELECT api_key FROM providers WHERE provider = ?", (pid,)
        ).fetchone()
    if not row or not row["api_key"]:
        return HTMLResponse('<div class="action-err">save an API key first</div>')
    result = test_provider(pid, row["api_key"])
    usage = fetch_provider_usage(pid, row["api_key"]) if result["ok"] else None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with db() as conn:
        conn.execute(
            "UPDATE providers SET status = ?, latency_ms = ?, last_ok = "
            "COALESCE(?, last_ok), models_json = ?, usage_json = ? WHERE provider = ?",
            ("ok" if result["ok"] else result.get("error", "error"),
             result["latency_ms"], now if result["ok"] else None,
             json.dumps(result.get("models", [])[:400]),
             json.dumps(usage), pid),
        )
    return _provider_card(request, pid)


@app.post("/settings/{pid}/activate", response_class=HTMLResponse)
def settings_activate(request: Request, pid: str):
    if pid not in PROVIDERS:
        return HTMLResponse("unknown provider", status_code=404)
    with db() as conn:
        row = conn.execute(
            "SELECT api_key, model FROM providers WHERE provider = ?", (pid,)
        ).fetchone()
    if not row or not row["api_key"]:
        return HTMLResponse('<div class="action-err">save an API key first</div>')
    try:
        activate_in_holmes(pid, row["api_key"], row["model"])
    except Exception as e:
        return HTMLResponse(f'<div class="action-err">activation failed: {escape(str(e))}</div>')
    with db() as conn:
        conn.execute("UPDATE providers SET active = 0")
        conn.execute("UPDATE providers SET active = 1 WHERE provider = ?", (pid,))
    return HTMLResponse(
        '<div class="action-ok">✓ Activated — Holmes is restarting (~1 min). '
        'If running locally, re-run ./run.sh to refresh the port-forward.</div>'
    )


# ── actions ──────────────────────────────────────────────────────────

@app.post("/incidents/{incident_id}/restart", response_class=HTMLResponse)
def restart_pod(incident_id: str):
    """Human clicked approve: delete the pod so its controller recreates it."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
    if row is None:
        return HTMLResponse("incident not found", status_code=404)
    ns, pod = row["namespace"], row["resource"]
    if not (ns and pod and K8S_NAME.match(ns) and K8S_NAME.match(pod)):
        return HTMLResponse('<div class="action-err">invalid namespace/pod name</div>')
    # ponytail: shells out to kubectl — works locally and in-cluster (image
    # bundles kubectl); swap for kubernetes-python client if it ever hurts.
    result = subprocess.run(
        ["kubectl", "delete", "pod", "-n", ns, pod, "--wait=false"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        return HTMLResponse(f'<div class="action-err">{escape(result.stderr)}</div>')
    with db() as conn:
        conn.execute(
            "UPDATE incidents SET status = 'restarted' WHERE id = ?", (incident_id,)
        )
    return HTMLResponse(f'<div class="action-ok">✓ {escape(result.stdout)}</div>')

