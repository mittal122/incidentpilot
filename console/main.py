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
from datetime import datetime, timezone
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
        json={"ask": question},
        timeout=httpx.Timeout(300, connect=10),
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

