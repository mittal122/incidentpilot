# IncidentPilot — Production Incident Agent

Open-source incident-response agent for Kubernetes: alerts get enriched,
AI-investigated, and acted on from one console.

Built by combining two upstream open-source projects with our own ops console:

| Piece | What it does | Source |
|---|---|---|
| [Robusta](https://github.com/robusta-dev/robusta) | Watches the cluster, enriches Prometheus alerts, runs playbooks, fans out to sinks (Slack + this console) | `vendor/robusta/` |
| [HolmesGPT](https://github.com/robusta-dev/holmesgpt) | AI root-cause investigator with kubectl/Prometheus/Loki toolsets | `vendor/holmesgpt/` |
| **Console** (ours) | Web UI: live incident feed, one-click Holmes investigations, chat, human-approved remediation | `console/` |

## Architecture

```
Alertmanager ──► Robusta runner ──► Slack sink            (unchanged)
                       │
                       └──► webhook sink (json) ──► console /api/ingest ──► SQLite
console UI:
  /            incident list (htmx auto-refresh)
  /incidents/x detail + "Investigate with Holmes" + approve-restart button
  /chat        free-form questions to HolmesGPT (/api/chat proxy)
```

## Run the console locally

```bash
./run.sh          # does everything: venv, deps, Holmes port-forward, server
# open http://localhost:8010
```

Manual equivalent:

```bash
python3 -m venv .venv && .venv/bin/pip install -r console/requirements.txt
kubectl port-forward svc/robusta-holmes 10001:80 &   # Holmes API
.venv/bin/uvicorn console.main:app --host 0.0.0.0 --port 8010
```

Env vars: `HOLMES_URL` (default `http://localhost:10001`), `HOLMES_MODEL`
(default `nvidia-deepseek` — must name a model from Holmes' modelList,
its built-in default may not be configured), `CLUSTER_NAME`, `DB_PATH`.

## Wire Robusta to the console

Add to your Robusta Helm values (keep your existing sinks!) and
`helm upgrade`:

```yaml
sinksConfig:
  - webhook_sink:
      name: console_sink
      url: http://<console-host>:8010/api/ingest   # kind: use docker network gateway, e.g. 172.19.0.1
      format: json
      size_limit: 65536
```

## Remediation model

Nothing is auto-executed from the console. Each incident with an
identified pod shows an **Approve: restart pod** button; a human click
deletes the pod so its controller recreates it. Robusta playbooks
(e.g. `on_pod_crash_loop` → `delete_pod`) can automate specific cases —
configure those deliberately in Helm values.

## Licenses

`vendor/robusta` and `vendor/holmesgpt` are MIT-licensed by their
authors; their LICENSE files are preserved in place. Console code is
ours, same spirit.
