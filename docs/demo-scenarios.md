# IncidentPilot Demo Scenarios

One-command failure simulation for live demos: `./scripts/demo.sh <scenario> [--auto 120]`.

## Safety model

- **Everything is namespace-scoped.** All scenarios run in `demo-lab` (plus
  `robusta-test-demo` for the auto-fix demo). Real application namespaces are
  never referenced by the script — nothing to remember, nothing to slip.
- **Recovery is a re-apply, not an undo-list.** `recover` deletes anything
  labeled `demo-failure` and re-applies the baseline manifests; Kubernetes'
  declarative model guarantees the end state equals the original state.
- **Ctrl-C recovers.** Every scenario traps INT/TERM; `--auto N` recovers by
  itself after N seconds. A forgotten demo can always be nuked with
  `./scripts/demo.sh down` (deletes the namespaces entirely).
- Nothing in the script has cluster-wide scope; no CRDs, no node changes,
  no persistent volumes.

## Prerequisites

1. IncidentPilot backend services installed (see README) with the webhook sink pointed
   at the console.
2. Console running: `./run.sh` → http://localhost:8010.
3. Slack sink configured (already: `#all-autosre`).

## Baseline

`./scripts/demo.sh up` creates namespace `demo-lab` with a small fake shop:

| Deployment | Image | Replicas | Purpose |
|---|---|---|---|
| `webshop` | nginx:1.27-alpine | 2 | the "product" being demoed |
| `cart` | busybox (sleep) | 1 | second service for image-pull demo |

Overview page shows a new **demo-lab** card, green `Healthy`, `3/3 pods`.

## Scenarios

### 1. `crashloop` — CrashLoopBackOff (the headline demo)

- **Simulates:** an app that dies on startup because a dependency (redis) is
  unreachable. The container logs a realistic sequence — `[INFO] starting`,
  `[ERROR] redis connection refused`, `[FATAL] session store unavailable` —
  then exits 1. Kubernetes restarts it with exponential backoff.
- **Resource:** `deploy/webshop` (image+command patched).
- **App detects:** the alert pipeline kubewatch fires "Crashing pod" within ~2 restarts;
  Prometheus `KubePodCrashLooping` follows.
- **UI:** demo-lab card flips Healthy → **Critical** (red pulse, sorts first);
  incident row appears; namespace page shows the 💡 crash explanation; View
  Logs streams the red FATAL lines live (use "previous run" for the dead
  container).
- **Slack:** "Crashing pod webshop-… in namespace demo-lab" with logs attached.
- **AI:** Investigate → the AI engine reads the logs and reports the redis dependency
  as root cause with suggested fixes; Logs → AI Summary produces the sectioned
  plain-English explanation.
- **Recovery:** `recover` re-applies the original nginx spec → pods start
  clean → card returns to green; Prometheus alert resolves (Slack resolved
  notice follows on the next Alertmanager cycle).

### 2. `imagepull` — ImagePullBackOff

- **Simulates:** a deploy with a typo'd image tag (`busybox:this-tag-does-not-exist-v99`).
- **Resource:** `deploy/cart`.
- **Detects:** "Failed to pull at least one image" finding; pod stuck Pending/ErrImagePull.
- **UI:** demo-lab **Critical** with `ImagePullBackOff` footer; pod row shows
  💡 "Kubernetes can't download the container image…".
- **AI:** explains registry/tag causes; recommends fixing the image reference
  (correct: restart does NOT help here — good talking point).
- **Recovery:** baseline re-apply restores the valid tag.

### 3. `scalezero` — deployment scaled to zero

- **Simulates:** accidental scale-down / outage without any crash.
- **Resource:** `deploy/webshop` replicas 2→0.
- **Detects:** Prometheus `KubeDeploymentReplicasMismatch` /
  "Deployment has not matched the expected number of replicas" (takes a few
  minutes — rule has a `for:` window).
- **UI:** pods disappear from namespace page; incident appears after the alert
  window.
- **Recovery:** scale back to 2 via baseline.

### 4. `oom` — OOMKilled

- **Simulates:** memory leak: container allocates unbounded (`tail /dev/zero`)
  against a 32Mi limit; kernel kills it repeatedly.
- **Resource:** new `deploy/memhog` (labeled `demo-failure`).
- **Detects:** crash-loop finding with reason OOMKilled.
- **UI:** Critical card; pod row restarts climbing; 💡 "used more memory than
  its limit — raise the limit or fix a leak".
- **AI:** identifies OOMKilled from pod state, recommends limit/leak fix.
- **Recovery:** memhog deleted by label.

### 5. `cpu` — CPU saturation/throttling

- **Simulates:** four spin-loops against a 100m CPU limit → ~97% throttling.
- **Resource:** new `deploy/cpuburn` (labeled `demo-failure`).
- **Detects:** `CPUThrottlingHigh` Prometheus alert (needs ~15 min window —
  start this one early in a demo, or present it via Grafana/`kubectl top`).
- **Recovery:** cpuburn deleted by label. Node safety: the pod's limit caps
  it at 0.1 core, so the demo can't starve the node.

### 6. `chaos` — multiple simultaneous failures

- **Simulates:** bad day: crashloop + imagepull + OOM at once.
- **UI:** demo-lab card shows 3+ incidents in 24h, multiple error reasons;
  incident list interleaves three failure types; chat question "what is wrong
  in demo-lab?" gives the AI engine a genuinely multi-cause investigation.
- **Recovery:** single `recover` fixes all three.

### 7. `autofix` — existing automated remediation, live

- **Simulates:** crash-looping pod in `robusta-test-demo` — a namespace
  matching the `robusta-test` prefix of the pre-existing pipeline playbook
  `on_pod_crash_loop → delete_pod`.
- **What happens without any human:** pod crashes → the alert pipeline detects → playbook
  deletes the pod → Deployment recreates it → repeat. Slack shows both the
  crash finding and the playbook action.
- **Talking point:** this is the "autopilot" primitive — same mechanism a
  production auto-remediation policy would use, deliberately fenced to
  test namespaces.
- **Recovery:** `recover` deletes the whole `robusta-test-demo` namespace.

## Observability checklist (what to point at during any scenario)

| Signal | Where | Latency |
|---|---|---|
| Card Healthy → Critical | Overview (auto-refresh 10s) | seconds after pod state change |
| Incident row | /incidents (5s refresh) | seconds (kubewatch) to minutes (Prometheus `for:` windows) |
| Live logs | namespace → View Logs (5s refresh) | real time |
| AI root cause | incident → Investigate | ~1–2 min (LLM tool loop) |
| AI log summary | View Logs → AI Summary | ~10–30 s |
| Slack alert | #all-autosre | seconds |
| Slack resolved notice | #all-autosre | next Alertmanager resolve cycle |
| Grafana | robusta-grafana (port-forward) | Prometheus scrape interval |
| Auto-remediation | `autofix` scenario only | ~30 s per crash cycle |

## Suggested live-demo script (10 min)

1. `./scripts/demo.sh up` → show green demo-lab card. (1 min)
2. `./scripts/demo.sh crashloop --auto 300` → watch card turn red, open
   incident, show Slack ping. (2 min)
3. View Logs → red FATAL lines → AI Summary tab. (2 min)
4. Investigate with the AI engine → walk through the root cause. (2 min)
5. Ask the AI engine chat: "what is broken in demo-lab and what should I do?" (1 min)
6. Let `--auto` recover on stage → card returns green, Slack resolves. (1 min)
7. `./scripts/demo.sh autofix` → automation fixing a pod with zero clicks. (1 min)
8. `./scripts/demo.sh down` → cluster exactly as before.
