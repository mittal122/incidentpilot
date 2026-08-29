# Production Incident Agent — Ops Console Design

Date: 2026-08-29
Status: Approved by user in chat

## Purpose

Open-source incident-response agent combining two upstream projects
(Robusta + HolmesGPT) with a self-built web ops console. The console is
the piece that upstream does not provide open-source (Robusta's SaaS
dashboard is closed).

## Repo layout (monorepo)

```
vendor/robusta/       # full source clone of robusta-dev/robusta
vendor/holmesgpt/     # full source clone of robusta-dev/holmesgpt
console/              # our app: FastAPI + htmx ops console
  main.py
  templates/          # server-rendered HTML + htmx
  static/
  incidents.db        # SQLite, runtime artifact (gitignored)
deploy/               # k8s/helm bits for the console + robusta values
docs/
```

Vendored clones have their `.git` removed so the monorepo tracks the
source directly. Upstream licenses (MIT) kept in place.

## Stack

- Backend: FastAPI (Python, same language as both upstreams)
- Frontend: plain HTML + htmx, server-rendered Jinja2 templates
- Storage: SQLite via stdlib `sqlite3`
- No auth in v1 (local/demo use). No build step for frontend.

## Data flow

```
Alertmanager -> Robusta runner (enrichment)
                  |- slack sink (kept, unchanged)
                  |- webhook sink (NEW) -> console POST /api/ingest
console stores incidents in SQLite
UI:
  incident list  (htmx polling)
  incident detail -> "Investigate" -> proxies to Holmes HTTP API
  chat page      -> proxies to Holmes HTTP API (/api/chat)
  action buttons -> restart pod via kubectl (explicit human click = approval)
```

## Environment facts (discovered)

- Cluster: single-node kind-style `helmsman-control-plane`, k8s v1.36
- Robusta v0.48.0 already installed in `default` ns, bundled
  kube-prometheus-stack, `clusterName: incidentpilot-dev`
- HolmesGPT enabled, service `robusta-holmes` in `default`,
  LLM = NVIDIA API (deepseek) via OpenAI-compatible endpoint
- Slack sink -> #all-autosre
- Loki stack in `loki` ns; Holmes has the loki toolset enabled
- Existing playbook: on_pod_crash_loop (ns prefix `robusta-test`) ->
  delete_pod
- Known issue: node memory *requests* exhausted; a Holmes rollout was
  stuck Pending. Console must keep tiny resource requests.

## v1 scope

1. Incident list page, auto-refresh, severity/pod/ns/time
2. Incident detail with Robusta enrichment + Holmes investigation inline
3. Chat page (free-form Holmes questions)
4. One action: restart pod (approve button), executed via kubectl
5. Runs locally via uvicorn against kubeconfig; in-cluster deploy
   manifest provided in deploy/

Out of scope v1: auth, multi-cluster, playbook editing UI, action audit
beyond SQLite rows, replacing Slack.

## Testing

End-to-end: crash-looping pod in throwaway namespace -> alert ->
webhook -> appears in console -> Holmes investigation returns real root
cause -> restart action works.
