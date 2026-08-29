#!/usr/bin/env bash
# IncidentPilot demo lab — safe, repeatable failure simulation.
#
# Everything happens inside the throwaway namespace "demo-lab" (plus
# "robusta-test-demo" for the auto-remediation scenario). Your real
# application namespaces are never touched — safety by construction.
#
# Usage:
#   ./scripts/demo.sh up                    # create demo namespace + healthy baseline app
#   ./scripts/demo.sh crashloop             # scenario: CrashLoopBackOff
#   ./scripts/demo.sh imagepull             # scenario: ImagePullBackOff
#   ./scripts/demo.sh scalezero             # scenario: deployment scaled to 0
#   ./scripts/demo.sh oom                   # scenario: OOMKilled (memory limit hit)
#   ./scripts/demo.sh cpu                   # scenario: CPU burn against a tiny limit
#   ./scripts/demo.sh chaos                 # multiple simultaneous failures
#   ./scripts/demo.sh autofix               # crashloop in robusta-test-demo -> Robusta auto-deletes it
#   ./scripts/demo.sh <scenario> --auto 120 # auto-recover after 120s (Ctrl-C also recovers)
#   ./scripts/demo.sh recover               # restore healthy baseline (undo all scenarios)
#   ./scripts/demo.sh status                # show demo-lab health
#   ./scripts/demo.sh down                  # delete the demo namespaces entirely
set -euo pipefail

NS=demo-lab
AUTONS=robusta-test-demo   # matches Robusta's namespace_prefix "robusta-test" playbook

baseline() {
  kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl apply -n "$NS" -f - <<'YAML' >/dev/null
apiVersion: apps/v1
kind: Deployment
metadata:
  name: webshop
  labels: {app: webshop, demo: incidentpilot}
spec:
  replicas: 2
  selector: {matchLabels: {app: webshop}}
  template:
    metadata: {labels: {app: webshop}}
    spec:
      containers:
      - name: web
        image: nginx:1.27-alpine
        ports: [{containerPort: 80}]
        resources:
          requests: {cpu: 10m, memory: 32Mi}
          limits: {memory: 64Mi}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cart
  labels: {app: cart, demo: incidentpilot}
spec:
  replicas: 1
  selector: {matchLabels: {app: cart}}
  template:
    metadata: {labels: {app: cart}}
    spec:
      containers:
      - name: cart
        image: busybox:1.36
        command: ["sh", "-c", "echo cart service ready; sleep infinity"]
        resources:
          requests: {cpu: 10m, memory: 16Mi}
          limits: {memory: 32Mi}
YAML
}

recover() {
  echo "==> Recovering: restoring healthy baseline in $NS"
  # delete scenario-only workloads (labeled demo-failure), keep baseline
  kubectl delete deploy,pod -n "$NS" -l demo-failure --ignore-not-found --wait=false 2>/dev/null || true
  kubectl delete namespace "$AUTONS" --ignore-not-found --wait=false 2>/dev/null || true
  # delete baseline deployments before re-apply: `kubectl apply` cannot remove
  # fields added by `kubectl patch` (three-way merge only prunes fields that
  # were in last-applied-configuration), so a patched-in crash command would
  # survive a plain re-apply. Delete + recreate guarantees the exact spec.
  kubectl delete deploy webshop cart -n "$NS" --ignore-not-found >/dev/null 2>&1 || true
  baseline
  kubectl scale deploy/webshop -n "$NS" --replicas=2 >/dev/null 2>&1 || true
  kubectl scale deploy/cart -n "$NS" --replicas=1 >/dev/null 2>&1 || true
  echo "==> Baseline restored. Pods recreate in a few seconds; alerts resolve on the next Prometheus cycle."
}

# parse "--auto N" from the command line ($2 $3)
AUTO=0
if [ "${2:-}" = "--auto" ]; then AUTO="${3:-120}"; fi

# Wait AUTO seconds then recover. Ctrl-C also recovers.
auto_watch() {
  local secs="${1:-0}"
  trap 'echo; recover; exit 0' INT TERM
  if [ "$secs" -gt 0 ]; then
    echo "==> Auto-recovery in ${secs}s (Ctrl-C to recover immediately)"
    sleep "$secs"
    recover
  else
    echo "==> Failure is live. Run './scripts/demo.sh recover' when done (or re-run with --auto 120)."
  fi
}

case "${1:-help}" in
  up)
    baseline
    echo "==> demo-lab ready: webshop (2 replicas) + cart (1 replica), all healthy."
    ;;

  crashloop)
    baseline
    echo "==> Breaking webshop: container now exits with a realistic DB error"
    kubectl patch deploy webshop -n "$NS" --type json -p '[
      {"op":"replace","path":"/spec/template/spec/containers/0/image","value":"busybox:1.36"},
      {"op":"add","path":"/spec/template/spec/containers/0/command","value":
        ["sh","-c","echo \"[INFO] webshop v3.1 starting\"; echo \"[INFO] connecting to redis cache-master:6379\"; sleep 1; echo \"[ERROR] redis connection refused - is cache-master running?\"; echo \"[FATAL] session store unavailable, aborting startup\"; exit 1"]}
    ]' >/dev/null
    auto_watch "$AUTO"
    ;;

  imagepull)
    baseline
    echo "==> Breaking cart: image tag that does not exist"
    kubectl set image deploy/cart -n "$NS" cart=busybox:this-tag-does-not-exist-v99 >/dev/null
    auto_watch "$AUTO"
    ;;

  scalezero)
    baseline
    echo "==> Scaling webshop to 0 replicas (service outage)"
    kubectl scale deploy/webshop -n "$NS" --replicas=0 >/dev/null
    auto_watch "$AUTO"
    ;;

  oom)
    baseline
    echo "==> Deploying memory hog (32Mi limit, allocates until OOMKilled)"
    kubectl apply -n "$NS" -f - <<'YAML' >/dev/null
apiVersion: apps/v1
kind: Deployment
metadata:
  name: memhog
  labels: {demo-failure: "true"}
spec:
  replicas: 1
  selector: {matchLabels: {app: memhog}}
  template:
    metadata: {labels: {app: memhog, demo-failure: "true"}}
    spec:
      containers:
      - name: hog
        image: busybox:1.36
        command: ["sh", "-c", "echo allocating memory...; tail /dev/zero"]
        resources:
          requests: {memory: 16Mi}
          limits: {memory: 32Mi}
YAML
    auto_watch "$AUTO"
    ;;

  cpu)
    baseline
    echo "==> Deploying CPU burner (4 spin loops against a 100m limit -> heavy throttling)"
    kubectl apply -n "$NS" -f - <<'YAML' >/dev/null
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cpuburn
  labels: {demo-failure: "true"}
spec:
  replicas: 1
  selector: {matchLabels: {app: cpuburn}}
  template:
    metadata: {labels: {app: cpuburn, demo-failure: "true"}}
    spec:
      containers:
      - name: burn
        image: busybox:1.36
        command: ["sh", "-c", "for i in 1 2 3 4; do (while :; do :; done) & done; wait"]
        resources:
          requests: {cpu: 50m}
          limits: {cpu: 100m}
YAML
    auto_watch "$AUTO"
    ;;

  chaos)
    baseline
    echo "==> Multiple simultaneous failures: crashloop + imagepull + oom"
    "$0" crashloop >/dev/null 2>&1 || true
    kubectl set image deploy/cart -n "$NS" cart=busybox:this-tag-does-not-exist-v99 >/dev/null
    "$0" oom >/dev/null 2>&1 || true
    echo "==> demo-lab is now on fire (3 distinct failures)."
    auto_watch "$AUTO"
    ;;

  autofix)
    echo "==> Auto-remediation demo: crashing pod in $AUTONS (Robusta playbook deletes it automatically)"
    kubectl create namespace "$AUTONS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    kubectl apply -n "$AUTONS" -f - <<'YAML' >/dev/null
apiVersion: apps/v1
kind: Deployment
metadata:
  name: flaky
  labels: {demo-failure: "true"}
spec:
  replicas: 1
  selector: {matchLabels: {app: flaky}}
  template:
    metadata: {labels: {app: flaky, demo-failure: "true"}}
    spec:
      containers:
      - name: flaky
        image: busybox:1.36
        command: ["sh", "-c", "echo '[ERROR] license check failed, exiting'; exit 1"]
YAML
    echo "==> Watch: pod crashes -> Robusta's on_pod_crash_loop playbook auto-deletes it -> Deployment recreates it."
    auto_watch "$AUTO"
    ;;

  recover) recover ;;

  status)
    kubectl get pods -n "$NS" 2>/dev/null || echo "demo-lab does not exist — run './scripts/demo.sh up'"
    kubectl get pods -n "$AUTONS" 2>/dev/null || true
    ;;

  down)
    echo "==> Deleting demo namespaces entirely"
    kubectl delete namespace "$NS" "$AUTONS" --ignore-not-found --wait=false
    ;;

  *)
    grep '^#   ' "$0" | sed 's/^#   //'
    ;;
esac
