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
    return render(request, "detail.html", page="incidents", i=i)


# ── HolmesGPT ────────────────────────────────────────────────────────

def ask_holmes(question: str) -> str:
    resp = httpx.post(
        f"{HOLMES_URL}/api/chat",
        json={"ask": question, "model": HOLMES_MODEL},
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
        f"Details: {row['description'] or '(none)'}"
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
    return HTMLResponse(f'<pre class="investigation">{escape(analysis)}</pre>')


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    return render(request, "chat.html", page="chat")


@app.post("/api/chat", response_class=HTMLResponse)
def chat(ask: str = Form(...)):
    try:
        analysis = ask_holmes(ask)
    except Exception as e:
        analysis = f"Holmes error: {e}"
    return HTMLResponse(
        f'<div class="chat-entry"><div class="q">You: {escape(ask)}</div>'
        f"<pre>{escape(analysis)}</pre></div>"
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

