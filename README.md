# IncidentPilot

AI-powered incident response platform for Kubernetes: failures are
detected in seconds, explained by an AI investigator with live cluster
access, and fixed with one approved click — or automatically, where you
allow it.

## Features

- **Cluster overview** — a card per namespace: health status, restarts,
  incidents, uptime; critical namespaces sort first, refreshed live
- **Incident feed** — every failure auto-captured and enriched with
  logs and events, pushed to Slack and the dashboard in seconds
- **IncidentChat** — conversational AI with real tools (kubectl,
  Prometheus, Loki): ask questions, investigate failures, run
  operational commands with an approval gate; full conversation history
- **AI root-cause analysis** — one click per incident; the AI reads the
  actual logs and cites its evidence
- **Log viewer** — terminal-style raw logs plus a plain-English AI
  summary of what happened
- **Auto-Heal** — opt-in per namespace: crash-looping pods restarted
  automatically under a strict, audited policy (rate-limited,
  databases excluded, escalates to humans when restarts don't help)
- **Zero-code setup** — Slack, AI providers/keys/models, webhook and
  cluster settings all configured from the Settings page

## Quickstart

```bash
./run.sh          # venv, dependencies, AI engine port-forward, server
# open http://localhost:8010
```

Requires: a Kubernetes cluster with the IncidentPilot backend services
installed (see `deploy/`), `kubectl`, Python 3.11+.

## Repo layout

```
console/     the IncidentPilot application (FastAPI + htmx + SQLite)
vendor/      third-party components (see CREDITS.md)
deploy/      Helm values and deployment examples
scripts/     demo lab — safe one-command failure simulations
docs/        architecture overview, demo playbook, screenshots
```

## Demo lab

```bash
./scripts/demo.sh up          # healthy sample app in a sandbox namespace
./scripts/demo.sh crashloop   # watch detection → AI diagnosis → recovery
./scripts/demo.sh recover     # everything back to green
```

All simulations are confined to throwaway namespaces — real workloads
are never touched.

## Third-party components

IncidentPilot builds on open-source components vendored under
`vendor/`; their licenses are preserved there and summarized in
[CREDITS.md](CREDITS.md).
