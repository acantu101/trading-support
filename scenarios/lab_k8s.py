#!/usr/bin/env python3
"""
Support Engineer Challenge Lab — Kubernetes, ArgoCD & Argo Workflows
====================================================================
Creates realistic K8s manifests, pod logs, ArgoCD configs, Argo Workflow
YAMLs, and diagnostic scripts for all K8s scenarios (K8-01 to K8-09).
No real cluster needed — all manifests and logs are file-based.

Run with: python3 lab_k8s.py [--scenario N] [--teardown]

SCENARIOS:
  1   K8-01  CrashLoopBackOff — pod logs + missing secret
  2   K8-02  Rolling deployment rollback — manifests + history
  3   K8-03  Pod stuck Pending / ImagePullBackOff
  4   K8-04  ArgoCD rollback + disable auto-sync
  5   K8-05  Validate a release before syncing to prod
  6   K8-06  Behavioral — K8s story + stats
  7   K8-07  Argo Workflows — OOMKilled step diagnosis
  8   K8-08  Argo Workflows — retry failed step (not full resubmit)
  9   K8-09  Argo Workflows — CronWorkflow missed schedule
  99         ALL scenarios
"""

import os, shutil, argparse
from pathlib import Path
from common import (
    GREEN, YELLOW, RED, CYAN, BOLD, RESET, SEP,
    ok, warn, err, info, header, lab_footer,
    create_dirs as _create_dirs, remove_lab_dir,
)

LAB_ROOT = Path("/tmp/lab_k8s")
DIRS = {
    "manifests": LAB_ROOT / "manifests",
    "logs":      LAB_ROOT / "logs",
    "scripts":   LAB_ROOT / "scripts",
    "argocd":    LAB_ROOT / "argocd",
    "argo":      LAB_ROOT / "argo",
}

def create_dirs(): _create_dirs(DIRS)

def write(path: Path, text: str) -> Path:
    path.write_text(text)
    ok(str(path).replace(str(LAB_ROOT) + "/", ""))
    return path


# ══════════════════════════════════════════════
#  SCENARIO 1 — CrashLoopBackOff
# ══════════════════════════════════════════════

def launch_scenario_1():
    header("Scenario K8-01 — CrashLoopBackOff")
    print("  risk-engine pod is in CrashLoopBackOff after a deploy.\n")

    write(DIRS["logs"] / "pod-describe.txt", """\
Name:         risk-engine-7d9b8c-xk2p9
Namespace:    trading
Node:         k8s-worker-2/10.0.1.52
Status:       Running

Containers:
  risk-engine:
    Image:        trading-registry.internal/risk-engine:2.5.1
    State:        Waiting
      Reason:     CrashLoopBackOff
    Last State:   Terminated
      Reason:     Error
      Exit Code:  1
    Restart Count: 5
    Environment:
      DB_HOST:      db-primary.trading.internal
      DB_PASSWORD:  <set to key 'password' in secret 'risk-engine-db-secret'>  Optional: false

Events:
  Warning  BackOff  5m  kubelet  Back-off restarting failed container
  Warning  BackOff  3m  kubelet  Back-off restarting failed container
  Warning  BackOff  1m  kubelet  Back-off restarting failed container
""")

    write(DIRS["logs"] / "pod-logs-previous.txt", """\
2024-01-15T09:35:42Z INFO  Risk Engine v2.5.1 starting...
2024-01-15T09:35:42Z INFO  Loading config from /etc/risk-engine/config.yaml
2024-01-15T09:35:42Z INFO  Connecting to PostgreSQL db-primary.trading.internal:5432
2024-01-15T09:35:42Z ERROR Failed to retrieve secret 'risk-engine-db-secret': secret not found in namespace 'trading'
2024-01-15T09:35:42Z ERROR Cannot load DB_PASSWORD — refusing to start without credentials
2024-01-15T09:35:43Z FATAL Startup failed. Exit code 1.
""")

    write(DIRS["scripts"] / "k8s01_fix_crashloop.sh", f"""\
#!/bin/bash
# K8-01: CrashLoopBackOff Diagnosis & Fix

echo "=== Step 1: Check pod status ==="
# kubectl get pods -n trading
# Look for: STATUS=CrashLoopBackOff, RESTARTS > 0

echo "=== Step 2: Read crash logs (--previous is critical) ==="
cat {DIRS["logs"]}/pod-logs-previous.txt
# On real cluster:
# kubectl logs -n trading <pod-name> --previous

echo "=== Step 3: Describe pod ==="
cat {DIRS["logs"]}/pod-describe.txt
# kubectl describe pod -n trading <pod-name>
# Check: State, Exit Code, Events, Environment vars

echo ""
echo "=== ROOT CAUSE: Missing Kubernetes Secret ==="
echo "  Error: 'risk-engine-db-secret' not found in namespace trading"
echo "  Pod can't read DB_PASSWORD → refuses to start"

echo ""
echo "=== FIX ==="
echo "  # Create the missing secret:"
echo "  kubectl create secret generic risk-engine-db-secret \\"
echo "    --from-literal=password='<db_password>' \\"
echo "    -n trading"
echo ""
echo "  # Verify it exists:"
echo "  kubectl get secret -n trading | grep risk-engine"
echo ""
echo "  # Delete pod so deployment recreates it:"
echo "  kubectl delete pod -n trading <pod-name>"
echo "  kubectl get pods -n trading -w"

echo ""
echo "=== Exit code cheat sheet ==="
echo "  Exit 1   = application error (check logs)"
echo "  Exit 137 = OOMKilled (increase memory limit)"
echo "  Exit 139 = SIGSEGV / segfault (app bug)"
""")

    print(f"""
{BOLD}── Artifacts ────────────────────────────────────────────{RESET}
{CYAN}  cat {DIRS["logs"]}/pod-logs-previous.txt    # crash reason
  cat {DIRS["logs"]}/pod-describe.txt          # full pod state
  cat {DIRS["scripts"]}/k8s01_fix_crashloop.sh # fix playbook{RESET}

{BOLD}── Root cause ───────────────────────────────────────────{RESET}
  Missing Secret 'risk-engine-db-secret'.
  Always read --previous logs first — current container may not
  have output yet when CrashLoopBackOff is active.

{BOLD}── Triage checklist ─────────────────────────────────────{RESET}
  1. kubectl logs --previous          (crash message)
  2. kubectl describe pod             (events + env vars)
  3. Exit 1 = app error   137 = OOM   139 = SIGSEGV
  4. Check: missing Secret, missing ConfigMap, wrong image tag
""")


# ══════════════════════════════════════════════
#  SCENARIO 2 — Rolling Deployment Rollback
# ══════════════════════════════════════════════

def launch_scenario_2():
    header("Scenario K8-02 — Rolling Deployment Rollback")
    print("  Bad deploy causing errors. Roll back to last good version.\n")

    write(DIRS["manifests"] / "market-data-processor.yaml", """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: market-data-processor
  namespace: trading
  annotations:
    deployment.kubernetes.io/revision: "4"
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 1
      maxSurge: 1
  template:
    spec:
      containers:
      - name: market-data-processor
        image: trading-registry.internal/market-data-processor:2.6.0-BAD
        resources:
          requests:
            cpu: 200m
            memory: 256Mi
          limits:
            cpu: 1000m
            memory: 512Mi
        livenessProbe:
          httpGet:
            path: /healthz
            port: 8080
          initialDelaySeconds: 30
""")

    write(DIRS["logs"] / "rollout-history.txt", """\
# kubectl rollout history deployment/market-data-processor -n trading
REVISION  CHANGE-CAUSE
1         Deploy v2.4.0 — initial release
2         Deploy v2.5.0 — add latency metrics
3         Deploy v2.5.1 — hotfix AAPL price feed   ← LAST KNOWN GOOD
4         Deploy v2.6.0 — new routing algorithm     ← CURRENT (BAD)
""")

    write(DIRS["scripts"] / "k8s02_rollback.sh", f"""\
#!/bin/bash
# K8-02: Rolling Deployment Rollback

NAMESPACE="trading"
DEPLOYMENT="market-data-processor"

echo "=== Current rollout history ==="
cat {DIRS["logs"]}/rollout-history.txt

echo ""
echo "=== Option A: Roll back one step (to revision 3) ==="
echo "  kubectl rollout undo deployment/$DEPLOYMENT -n $NAMESPACE"

echo ""
echo "=== Option B: Roll back to a specific revision ==="
echo "  kubectl rollout undo deployment/$DEPLOYMENT -n $NAMESPACE --to-revision=3"

echo ""
echo "=== Watch the rollback ==="
echo "  kubectl rollout status deployment/$DEPLOYMENT -n $NAMESPACE --watch"
echo "  # Wait for: 'successfully rolled out'"

echo ""
echo "=== Verify ==="
echo "  kubectl get pods -n $NAMESPACE"
echo "  # All pods: Running, RESTARTS=0"
echo "  kubectl describe deployment/$DEPLOYMENT -n $NAMESPACE | grep Image:"
echo "  # Should show :2.5.1 not :2.6.0-BAD"

echo ""
echo "⚠  CRITICAL FOR ARGOCD CLUSTERS:"
echo "  kubectl rollout undo is OVERWRITTEN by ArgoCD on next sync."
echo "  In ArgoCD clusters → always use 'argocd app rollback' (see K8-04)."
""")

    print(f"""
{BOLD}── Artifacts ────────────────────────────────────────────{RESET}
{CYAN}  cat {DIRS["logs"]}/rollout-history.txt           # revision history
  cat {DIRS["manifests"]}/market-data-processor.yaml # bad deployment
  cat {DIRS["scripts"]}/k8s02_rollback.sh           # rollback steps{RESET}

{BOLD}── Key commands (real cluster) ─────────────────────────{RESET}
{CYAN}  kubectl rollout history deployment/market-data-processor -n trading
  kubectl rollout undo deployment/market-data-processor -n trading
  kubectl rollout undo deployment/market-data-processor -n trading --to-revision=3
  kubectl rollout status deployment/market-data-processor -n trading --watch{RESET}
""")


# ══════════════════════════════════════════════
#  SCENARIO 3 — Pod Stuck Pending
# ══════════════════════════════════════════════

def launch_scenario_3():
    header("Scenario K8-03 — Pod Stuck Pending / ImagePullBackOff")
    print("  New service stuck. Never reaches Running. Three failure modes.\n")

    write(DIRS["logs"] / "pending-no-resources.txt", """\
# kubectl describe pod new-service-abc -n trading — Events section:

Events:
  Warning  FailedScheduling  5m  default-scheduler
    0/3 nodes are available: 1 Insufficient cpu, 2 Insufficient memory.
    preemption: 0/3 nodes are available: 3 No preemption victims found.

ROOT CAUSE: Nodes exhausted. Pod resource requests exceed available capacity.
FIX:
  Option 1: Reduce resource requests in deployment spec
  Option 2: Scale up the node pool (add nodes)
  Option 3: kubectl top nodes  →  kubectl describe nodes | grep -A5 "Allocated"
""")

    write(DIRS["logs"] / "pending-imagepull.txt", """\
# kubectl describe pod new-service-def -n trading

State: Waiting
  Reason: ImagePullBackOff

Events:
  Normal   Pulling  3m  kubelet  Pulling image "trading-registry.internal/new-service:1.2.3"
  Warning  Failed   3m  kubelet  Failed to pull image: 401 Unauthorized
  Warning  BackOff  2m  kubelet  Back-off pulling image

ROOT CAUSE: Registry authentication failure.
FIX:
  kubectl create secret docker-registry regcred \\
    --docker-server=trading-registry.internal \\
    --docker-username=svc-k8s \\
    --docker-password=<token> \\
    -n trading
  Then add to deployment: imagePullSecrets: [{name: regcred}]
""")

    write(DIRS["logs"] / "pending-missing-configmap.txt", """\
# kubectl describe pod new-service-ghi -n trading

State: Waiting
  Reason: CreateContainerConfigError

Events:
  Warning  Failed  30s  kubelet  Error: configmap "new-service-config" not found

ROOT CAUSE: Deployment references a ConfigMap that was never created.
FIX:
  kubectl get configmap -n trading               # check what exists
  kubectl apply -f new-service-config.yaml       # create the missing CM
  kubectl delete pod <pod-name> -n trading       # pod recreates using CM
""")

    write(DIRS["scripts"] / "k8s03_triage.sh", f"""\
#!/bin/bash
# K8-03: Pending / ImagePull triage

echo "The Events section of 'kubectl describe pod' tells you everything."
echo "Read it BEFORE touching anything else."
echo ""

echo "=== Failure Mode 1: Insufficient Resources ==="
cat {DIRS["logs"]}/pending-no-resources.txt

echo ""
echo "=== Failure Mode 2: ImagePullBackOff (auth) ==="
cat {DIRS["logs"]}/pending-imagepull.txt

echo ""
echo "=== Failure Mode 3: Missing ConfigMap ==="
cat {DIRS["logs"]}/pending-missing-configmap.txt

echo ""
echo "=== Triage commands ==="
echo "  kubectl get pods -n trading                              # see status"
echo "  kubectl describe pod <name> -n trading                  # read Events"
echo "  kubectl top nodes                                        # resource headroom"
echo "  kubectl get pod <name> -n trading -o jsonpath='...image' # image name"
echo "  kubectl get configmap,secret -n trading                 # check dependencies"
""")

    print(f"""
{BOLD}── Three Pending failure modes ─────────────────────────{RESET}
{CYAN}  cat {DIRS["logs"]}/pending-no-resources.txt    # CPU/memory exhausted
  cat {DIRS["logs"]}/pending-imagepull.txt        # registry 401
  cat {DIRS["logs"]}/pending-missing-configmap.txt # missing ConfigMap
  cat {DIRS["scripts"]}/k8s03_triage.sh          # full triage guide{RESET}

{BOLD}── Pattern ──────────────────────────────────────────────{RESET}
  State: Pending                → scheduling (resources/taints/affinity)
  State: ImagePullBackOff       → image not found or registry auth
  State: CreateContainerConfig  → missing ConfigMap or Secret
  State: CrashLoopBackOff       → starts but exits immediately
""")


# ══════════════════════════════════════════════
#  SCENARIO 4 — ArgoCD Rollback
# ══════════════════════════════════════════════

def launch_scenario_4():
    header("Scenario K8-04 — ArgoCD Rollback + Sync Management")
    print("  Bad release via ArgoCD. App is Degraded. Roll back and\n"
          "  prevent ArgoCD from re-syncing back to the bad commit.\n")

    write(DIRS["argocd"] / "oms-engine-app.yaml", """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: oms-engine
  namespace: argocd
spec:
  project: trading
  source:
    repoURL: https://github.com/trading-firm/k8s-manifests
    targetRevision: HEAD
    path: apps/oms-engine
  destination:
    server: https://kubernetes.default.svc
    namespace: trading
  syncPolicy:
    automated:
      prune: true
      selfHeal: true    # ← ArgoCD will fight any kubectl rollback
""")

    write(DIRS["argocd"] / "oms-engine-history.txt", """\
# argocd app history oms-engine
ID   DATE                           REVISION
0    2024-01-10 09:00:12 +0000 UTC  a3f2c91  v2.4.0 — known good
1    2024-01-12 14:30:00 +0000 UTC  b7e4d82  v2.4.1 — patch
2    2024-01-15 09:15:00 +0000 UTC  c9a1e55  v2.5.0 — BAD DEPLOY ← current
""")

    write(DIRS["scripts"] / "k8s04_argocd_rollback.sh", f"""\
#!/bin/bash
# K8-04: ArgoCD Rollback + Sync Management
# ==========================================
# DO NOT use 'kubectl rollout undo' in ArgoCD-managed clusters.
# ArgoCD's selfHeal will re-sync back to bad Git HEAD in seconds.
# ALWAYS use ArgoCD to roll back.

APP="oms-engine"

echo "=== Step 1: Check app status ==="
echo "  argocd app get $APP"
echo "  # Look for: Health=Degraded, Sync=Synced (synced to bad commit)"
echo ""
echo "  argocd app list   # see all managed apps"

echo ""
echo "=== View revision history ==="
cat {DIRS["argocd"]}/oms-engine-history.txt

echo ""
echo "=== CRITICAL Step 2: Disable auto-sync FIRST ==="
echo "  argocd app set $APP --sync-policy none"
echo ""
echo "  Without this: ArgoCD detects drift → re-syncs → undoes your rollback."
echo "  Verify disabled:"
echo "  argocd app get $APP | grep 'Sync Policy'"

echo ""
echo "=== Step 3: Roll back ==="
echo "  # Roll back one step (to revision 1 = b7e4d82):"
echo "  argocd app rollback $APP"
echo ""
echo "  # Roll back to specific known-good revision:"
echo "  argocd app rollback $APP 0   # → a3f2c91 (v2.4.0)"

echo ""
echo "=== Step 4: Watch and verify ==="
echo "  argocd app wait $APP --health"
echo "  kubectl get pods -n trading"
echo "  kubectl describe deployment oms-engine -n trading | grep Image:"
echo "  # Should show old tag, not v2.5.0"

echo ""
echo "=== Step 5: Re-enable auto-sync after devs push a fix ==="
echo "  argocd app set $APP --sync-policy automated --auto-prune --self-heal"
echo "  argocd app sync $APP"
""")

    print(f"""
{BOLD}── Artifacts ────────────────────────────────────────────{RESET}
{CYAN}  cat {DIRS["argocd"]}/oms-engine-app.yaml       # app definition
  cat {DIRS["argocd"]}/oms-engine-history.txt    # revision history
  cat {DIRS["scripts"]}/k8s04_argocd_rollback.sh # rollback playbook{RESET}

{BOLD}── The most important rule ──────────────────────────────{RESET}
  DISABLE AUTO-SYNC BEFORE ROLLBACK.
  selfHeal=true means ArgoCD fights kubectl rollout undo immediately.
  argocd app rollback is git-aware and durable.

{BOLD}── UI approach (same steps) ─────────────────────────────{RESET}
  App Details → Summary → Sync Policy → Disable Auto-Sync
  History and Rollback → pick last good revision → Rollback
  Monitor pods → once healthy → re-enable Auto-Sync
""")


# ══════════════════════════════════════════════
#  SCENARIO 5 — Validate Before Prod Sync
# ══════════════════════════════════════════════

def launch_scenario_5():
    header("Scenario K8-05 — Validate a Release Before Syncing to Prod")
    print("  Developer asks you to release a new version. What do you check?\n")

    write(DIRS["argocd"] / "risk-engine-diff.txt", """\
# argocd app diff risk-engine
# Shows what WILL change if you sync now

===== Deployment/risk-engine (trading) =====
  spec.template.spec.containers[0]:
-   image: trading-registry.internal/risk-engine:2.5.1
+   image: trading-registry.internal/risk-engine:2.6.0
    resources.requests:
-     cpu: 500m
+     cpu: 750m       ← 50% increase — verify cluster headroom
-     memory: 512Mi
+     memory: 768Mi   ← 50% increase

# OBSERVATIONS BEFORE SYNCING:
# 1. Image 2.5.1 → 2.6.0 — matches the approved PR ticket
# 2. CPU +50%, memory +50% — run 'kubectl top nodes' to verify headroom
# 3. No config or secret changes — good, nothing else unexpected
""")

    write(DIRS["scripts"] / "k8s05_release_checklist.sh", f"""\
#!/bin/bash
# K8-05: Release Validation Checklist

APP="risk-engine"
NAMESPACE="trading"

echo "=== Pre-Release Checks ==="

echo "1. Review the diff"
cat {DIRS["argocd"]}/risk-engine-diff.txt
echo "   argocd app diff $APP   (on real cluster)"

echo ""
echo "2. Confirm image tag matches approved PR"
echo "   argocd app get $APP | grep 'Target Revision'"

echo ""
echo "3. Check cluster resource headroom (resource requests are increasing)"
echo "   kubectl top nodes"
echo "   kubectl describe nodes | grep -A5 'Allocated resources'"

echo ""
echo "4. Dry-run sync to validate manifests"
echo "   argocd app sync $APP --dry-run"

echo ""
echo "=== Sync & Monitor ==="
echo "5. Sync to prod"
echo "   argocd app sync $APP"

echo ""
echo "6. Watch progress"
echo "   argocd app wait $APP --health --timeout 120"
echo "   kubectl rollout status deployment/$APP -n $NAMESPACE"

echo ""
cat << 'EOF'
=== GO / NO-GO (2-minute window) ===

GO ✅ — all of these must be true:
  - All pods Running, RESTARTS=0 after 2 minutes
  - ArgoCD: Health=Healthy, Sync=Synced
  - kubectl top pods: CPU/memory within expected range
  - No error logs at startup
  - Liveness/readiness probes passing

NO-GO 🚫 — rollback immediately if any of these:
  - Any pod in CrashLoopBackOff
  - OOMKilled in Events (kubectl describe pod)
  - ArgoCD Health=Degraded
  - Trader-facing alerts triggered within 2 minutes
  - Latency spike in monitoring dashboard
EOF
""")

    print(f"""
{BOLD}── Artifacts ────────────────────────────────────────────{RESET}
{CYAN}  cat {DIRS["argocd"]}/risk-engine-diff.txt         # what will change
  cat {DIRS["scripts"]}/k8s05_release_checklist.sh   # full checklist{RESET}

{BOLD}── Key interview phrases ────────────────────────────────{RESET}
  "I always read the diff — not just the image tag.
   I caught an accidental config change this way once."

  "I use --dry-run to validate manifests before every prod sync."

  "My go/no-go window is 2 minutes. A bad deploy at 9:35
   is a live P&L incident."
""")


# ══════════════════════════════════════════════
#  SCENARIO 6 — Behavioral Story
# ══════════════════════════════════════════════

def launch_scenario_6():
    header("Scenario K8-06 — Tell Your K8s Story (Behavioral)")
    print("  Prepare your answer for 'Walk me through your K8s experience.'\n")

    write(DIRS["scripts"] / "k8s06_story_notes.md", """\
# K8-06: K8s Behavioral Answer Notes
======================================

## The Question
"Walk me through how you use Kubernetes and ArgoCD in your current role.
What's the most impactful deployment issue you've handled?"

## Model Answer

"At Granite Point I manage Kubernetes deployments for 60+ production
trading applications using ArgoCD. These include the OMS, risk engine,
market data processors, and FIX session handlers — services where
a bad deploy during market hours directly costs money.

My release process:
  1. Review the ArgoCD diff — confirm only expected changes are going out
  2. Validate image tag matches the approved commit / PR
  3. --dry-run sync to catch manifest errors before they hit prod
  4. Sync and monitor: argocd app wait, kubectl rollout status
  5. 2-minute go/no-go window — CrashLoopBackOff or Degraded → rollback

The most important lesson I learned: kubectl rollout undo doesn't work
in ArgoCD clusters. ArgoCD's selfHeal re-syncs to Git HEAD in seconds,
undoing your rollback. The correct procedure is:
  1. Disable auto-sync FIRST (argocd app set --sync-policy none)
  2. Use argocd app rollback, not kubectl
  3. Re-enable auto-sync only after devs push a clean fix to Git.

Over the past year that disciplined process reduced deployment-related
incidents by about 25%. We hold 99.4% uptime across 60+ apps during
a 27.5-hour/week zero-tolerance market window."

## Numbers to Say Out Loud
  → 60+ production trading applications
  → 25% reduction in deployment incidents
  → 99.4% uptime
  → 2-minute go/no-go window

## Traps to Avoid
  ✗ "I just click sync in the UI" (too passive — no process)
  ✗ "kubectl rollout undo" in ArgoCD context (shows you don't know ArgoCD)
  ✓ "I review the diff before every sync"
  ✓ "I disable auto-sync before rolling back"
  ✓ Connect every decision to trading P&L impact
""")

    print(f"""
{BOLD}── Study the model answer ───────────────────────────────{RESET}
{CYAN}  cat {DIRS["scripts"]}/k8s06_story_notes.md{RESET}

{BOLD}── Numbers to memorise ──────────────────────────────────{RESET}
  60+ production apps  |  25% fewer incidents  |  99.4% uptime
  2-minute go/no-go   |  27.5h/week market window

{BOLD}── Practice saying out loud ─────────────────────────────{RESET}
  "Disable auto-sync FIRST — otherwise ArgoCD fights your rollback."
  "I treat every market-hours release as high-stakes, even small changes."
  "The diff tells you more than the image tag alone."
""")


# ══════════════════════════════════════════════
#  SCENARIO 7 — Argo Workflows: OOMKilled Step
# ══════════════════════════════════════════════

def launch_scenario_7():
    header("Scenario K8-07 — Argo Workflows: OOMKilled Step")
    print(f"""
{YELLOW}  ArgoCD vs Argo Workflows — two separate products, know the difference{RESET}
  ArgoCD         = GitOps CD — keeps K8s apps in sync with a Git repo
  Argo Workflows = Job orchestration — runs DAG pipeline jobs as K8s pods
                   (Kubernetes-native alternative to Airflow, defined in YAML)

  This stack uses BOTH:
    ArgoCD         → deploys the applications (OMS, feed handler, risk engine)
    Argo Workflows → runs the data jobs (tick storage, EOD reports, replay)
""")
    print("  The 04:00 UTC daily tick-storage workflow did not complete.")
    print("  Alert fired at 06:17 UTC: workflow running >2h (expected ~15 min).")
    print("  The write-hdf5 step failed with exit code 137. Investigate.\n")

    write(DIRS["argo"] / "tick-storage-workflow.yaml", """\
# Argo Workflow: daily tick storage job
# Triggered by CronWorkflow at 04:00 UTC Mon-Fri
# Pipeline: fetch ticks from Kafka → validate → write to HDF5
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  name: tick-storage-20260504-x7k2p
  namespace: data-pipelines
  labels:
    app: tick-storage
    date: "2026-05-04"
spec:
  entrypoint: tick-storage-dag
  serviceAccountName: argo-workflow-sa
  templates:

  - name: tick-storage-dag
    dag:
      tasks:
      - name: fetch-ticks
        template: fetch-ticks-tmpl
        arguments:
          parameters:
          - name: date
            value: "2026-05-04"
      - name: validate
        template: validate-tmpl
        dependencies: [fetch-ticks]
      - name: write-hdf5
        template: write-hdf5-tmpl
        dependencies: [validate]
        arguments:
          parameters:
          - name: date
            value: "2026-05-04"

  - name: fetch-ticks-tmpl
    container:
      image: data-pipeline:v2.1.4
      command: [python3, fetch_ticks.py]
      args: ["--date", "{{inputs.parameters.date}}", "--symbols", "ALL"]
      resources:
        requests:
          memory: "512Mi"
          cpu: "500m"
        limits:
          memory: "1Gi"
          cpu: "1000m"

  - name: validate-tmpl
    container:
      image: data-pipeline:v2.1.4
      command: [python3, validate_ticks.py]
      resources:
        requests:
          memory: "256Mi"
          cpu: "250m"
        limits:
          memory: "512Mi"
          cpu: "500m"

  - name: write-hdf5-tmpl
    container:
      image: data-pipeline:v2.1.4
      command: [python3, write_hdf5.py]
      args: ["--date", "{{inputs.parameters.date}}", "--symbols", "ALL", "--compress", "lz4"]
      resources:
        requests:
          memory: "512Mi"
          cpu: "500m"
        limits:
          memory: "1Gi"    # BUG: full day all symbols needs ~3.5Gi — causes OOMKilled
          cpu: "2000m"
""")

    write(DIRS["logs"] / "argo-get-tick-storage.txt", """\
# argo get tick-storage-20260504-x7k2p -n data-pipelines

Name:                tick-storage-20260504-x7k2p
Namespace:           data-pipelines
ServiceAccount:      argo-workflow-sa
Status:              Failed
Message:             child 'write-hdf5' failed
Created:             Mon May 04 04:00:12 +0000 (2h 22m ago)
Started:             Mon May 04 04:00:14 +0000 (2h 22m ago)
Finished:            Mon May 04 06:17:43 +0000
Duration:            2 hours 17 minutes

STEP                                    PODNAME                                              DURATION  MESSAGE
 ✖ tick-storage-20260504-x7k2p          (tick-storage-dag)                                             child 'write-hdf5' failed
 ├─✔ fetch-ticks                        tick-storage-20260504-x7k2p-fetch-ticks-1298457      3m 12s
 ├─✔ validate                           tick-storage-20260504-x7k2p-validate-3849201          1m 04s
 └─✖ write-hdf5                         tick-storage-20260504-x7k2p-write-hdf5-9012345        2h 13m    Error (exit code 137)
""")

    write(DIRS["logs"] / "write-hdf5-pod.log", """\
# argo logs tick-storage-20260504-x7k2p write-hdf5 -n data-pipelines

2026-05-04T04:04:22Z INFO  write_hdf5.py starting  date=2026-05-04 symbols=ALL compress=lz4
2026-05-04T04:04:23Z INFO  Opened HDF5 file: /data/ticks/2026-05-04.h5
2026-05-04T04:04:23Z INFO  Loading symbol reference: 847 symbols loaded
2026-05-04T04:04:30Z INFO  [AAPL]  Writing 2,847,391 ticks ... done (12.3s)  mem=612Mi
2026-05-04T04:04:43Z INFO  [MSFT]  Writing 1,923,847 ticks ... done (8.7s)   mem=698Mi
2026-05-04T04:05:01Z INFO  [GOOGL] Writing 1,102,394 ticks ... done (5.2s)   mem=741Mi
2026-05-04T04:05:16Z INFO  [TSLA]  Writing 3,481,203 ticks ... done (15.8s)  mem=819Mi
2026-05-04T04:05:34Z INFO  [AMZN]  Writing 1,847,029 ticks ... done (7.4s)   mem=863Mi
2026-05-04T04:05:43Z INFO  [NVDA]  Writing 4,103,847 ticks ... done (22.1s)  mem=921Mi
2026-05-04T06:01:14Z INFO  [SPY]   Writing 8,293,847 ticks ... mem=975Mi/1024Mi WARNING: approaching limit
2026-05-04T06:11:44Z INFO  [QQQ]   Writing 6,102,394 ticks ... mem=1001Mi/1024Mi WARNING: near limit
2026-05-04T06:17:39Z INFO  [IWM]   Writing 5,847,201 ticks ... mem=1019Mi/1024Mi
2026-05-04T06:17:42Z WARN  GC overhead limit exceeded — heap exhausted
Killed

# exit code 137 = 128 + 9 (SIGKILL from kernel OOM killer)

# kubectl describe pod tick-storage-20260504-x7k2p-write-hdf5-9012345 -n data-pipelines
# Containers:
#   main:
#     State:      Terminated
#       Reason:   OOMKilled
#       Exit Code: 137
#     Limits:
#       cpu:      2
#       memory:   1Gi          ← too low for full-day all-symbol write
#     Requests:
#       cpu:      500m
#       memory:   512Mi
""")

    write(DIRS["scripts"] / "k8s07_argo_debug.sh", f"""\
#!/usr/bin/env bash
# K8-07: Debug Argo Workflow — OOMKilled step
# ============================================
WF="tick-storage-20260504-x7k2p"
NS="data-pipelines"

echo "STEP 1: Get workflow overview — which step failed?"
echo "  $ argo get $WF -n $NS"
cat {DIRS["logs"]}/argo-get-tick-storage.txt
echo ""

echo "STEP 2: Read logs from the failed step"
echo "  $ argo logs $WF write-hdf5 -n $NS"
echo "  Look for: 'Killed' and 'exit code 137'"
cat {DIRS["logs"]}/write-hdf5-pod.log
echo ""

echo "STEP 3: Confirm OOMKilled at the pod level"
echo "  $ kubectl describe pod tick-storage-20260504-x7k2p-write-hdf5-9012345 -n $NS"
echo "  Look for: Reason: OOMKilled  and  Limits: memory: 1Gi"
echo ""

echo "STEP 4: Fix — increase memory limit in the workflow YAML"
echo "  File: {DIRS['argo']}/tick-storage-workflow.yaml"
echo "  write-hdf5-tmpl  limits.memory: 1Gi  →  4Gi"
echo "  write-hdf5-tmpl  requests.memory: 512Mi  →  2Gi"
echo ""

echo "STEP 5: Retry from the failed step only (reuse fetch + validate output)"
echo "  $ argo retry $WF -n $NS --restart-successful"
echo "  NOT: argo resubmit  (that re-runs everything from scratch)"
echo ""

echo "EXIT CODE CHEAT SHEET:"
echo "  137 = OOMKilled  (128 + SIGKILL)"
echo "  1   = Application error"
echo "  126 = Permission denied (cannot execute)"
echo "  127 = Command not found"
echo ""

echo "ARGO COMMANDS CHEAT SHEET:"
echo "  argo list -n <ns>                          list all workflows"
echo "  argo get <wf> -n <ns>                      workflow + step status"
echo "  argo logs <wf> <step> -n <ns>              step pod output"
echo "  argo retry <wf> -n <ns> --restart-successful  resume from failure"
echo "  argo resubmit <wf> -n <ns>                 start over (new workflow)"
echo "  argo delete <wf> -n <ns>                   clean up"
""")

    print(f"""
{BOLD}── Read the workflow definition ─────────────────────────{RESET}
{CYAN}  cat {DIRS["argo"]}/tick-storage-workflow.yaml{RESET}

{BOLD}── See which step failed (argo get output) ──────────────{RESET}
{CYAN}  cat {DIRS["logs"]}/argo-get-tick-storage.txt{RESET}

{BOLD}── Read pod logs — find 'Killed' and exit code 137 ──────{RESET}
{CYAN}  cat {DIRS["logs"]}/write-hdf5-pod.log{RESET}

{BOLD}── Run the full debug guide ──────────────────────────────{RESET}
{CYAN}  bash {DIRS["scripts"]}/k8s07_argo_debug.sh{RESET}

{BOLD}── Key Interview Answer ──────────────────────────────────{RESET}
  "exit 137 = OOMKilled. I'd confirm with kubectl describe pod (Reason: OOMKilled),
  increase the memory limit in the workflow YAML, then argo retry --restart-successful
  to resume from the failed step without re-running the completed ones."
""")


# ══════════════════════════════════════════════
#  SCENARIO 8 — Argo Workflows: Retry Failed Step
# ══════════════════════════════════════════════

def launch_scenario_8():
    header("Scenario K8-08 — Argo Workflows: Retry vs Resubmit")
    print("  A 4-step replay pipeline failed at step 3. The first two steps took")
    print("  45 minutes and pulled 10GB from S3. You don't want to re-run them.")
    print("  Know the difference: argo retry vs argo resubmit.\n")

    write(DIRS["argo"] / "replay-pipeline-workflow.yaml", """\
# Argo Workflow: historical tick replay pipeline
# Triggered on-demand by the data replay website
# Steps: extract → transform → write-hdf5 → update-index
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  name: replay-pipeline-20260503-m4n8q
  namespace: data-pipelines
  labels:
    app: replay-pipeline
    requested-by: research-team
    date-range: "2024-01-01_2024-12-31"
spec:
  entrypoint: replay-dag
  serviceAccountName: argo-workflow-sa
  templates:

  - name: replay-dag
    dag:
      tasks:
      - name: extract-from-glacier
        template: extract-tmpl
      - name: transform-normalize
        template: transform-tmpl
        dependencies: [extract-from-glacier]
      - name: write-hdf5
        template: write-hdf5-tmpl
        dependencies: [transform-normalize]
      - name: update-replay-index
        template: update-index-tmpl
        dependencies: [write-hdf5]

  - name: extract-tmpl
    container:
      image: data-pipeline:v2.1.4
      command: [python3, extract_glacier.py]
      args: ["--date-range", "2024-01-01:2024-12-31", "--symbols", "AAPL,MSFT,TSLA"]
      # Restores ~10GB from S3 Glacier — takes ~40 minutes

  - name: transform-tmpl
    container:
      image: data-pipeline:v2.1.4
      command: [python3, transform_ticks.py]
      # Normalizes timestamps, fills splits/dividends, validates

  - name: write-hdf5-tmpl
    container:
      image: data-pipeline:v2.1.4
      command: [python3, write_hdf5_replay.py]
      args: ["--output", "/data/replay/AAPL_MSFT_TSLA_2024.h5"]
      resources:
        limits:
          memory: "2Gi"

  - name: update-index-tmpl
    container:
      image: data-pipeline:v2.1.4
      command: [python3, update_replay_index.py]
""")

    write(DIRS["logs"] / "replay-pipeline-get.txt", """\
# argo get replay-pipeline-20260503-m4n8q -n data-pipelines

Name:                replay-pipeline-20260503-m4n8q
Namespace:           data-pipelines
Status:              Failed
Message:             child 'write-hdf5' failed
Duration:            52 minutes 18 seconds

STEP                                         PODNAME                                           DURATION  MESSAGE
 ✖ replay-pipeline-20260503-m4n8q            (replay-dag)
 ├─✔ extract-from-glacier                   replay-pipeline-...-extract-1092837               41m 03s
 ├─✔ transform-normalize                    replay-pipeline-...-transform-4729103              9m 44s
 ├─✖ write-hdf5                             replay-pipeline-...-write-hdf5-8837291             1m 31s   Error (exit code 1)
 └─○ update-replay-index                    (skipped — dependency failed)
""")

    write(DIRS["logs"] / "write-hdf5-replay-error.log", """\
# argo logs replay-pipeline-20260503-m4n8q write-hdf5 -n data-pipelines

2026-05-03T14:12:01Z INFO  write_hdf5_replay.py starting
2026-05-03T14:12:02Z INFO  Output: /data/replay/AAPL_MSFT_TSLA_2024.h5
2026-05-03T14:12:02Z ERROR OSError: [Errno 28] No space left on device: '/data/replay/AAPL_MSFT_TSLA_2024.h5'
2026-05-03T14:12:02Z FATAL Unhandled exception — aborting
Traceback (most recent call last):
  File "write_hdf5_replay.py", line 47, in write_dataset
    dset = grp.create_dataset(symbol, data=arr, compression='lz4')
  File "h5py/_objects.pyx", line 54, in h5py._objects.with_phil.wrapper
OSError: Unable to create dataset (no space left on device)
""")

    write(DIRS["scripts"] / "k8s08_retry_vs_resubmit.sh", f"""\
#!/usr/bin/env bash
# K8-08: argo retry vs argo resubmit — know the difference
# =========================================================
WF="replay-pipeline-20260503-m4n8q"
NS="data-pipelines"

echo "=== Situation ==="
echo "  extract-from-glacier: PASSED (41min, pulled 10GB from Glacier)"
echo "  transform-normalize:  PASSED (9min)"
echo "  write-hdf5:           FAILED (disk full — exit code 1)"
echo "  update-replay-index:  SKIPPED (dependency failed)"
echo ""
cat {DIRS["logs"]}/replay-pipeline-get.txt

echo ""
echo "=== Root cause: disk full ==="
cat {DIRS["logs"]}/write-hdf5-replay-error.log

echo ""
echo "=== Fix: free up disk space ==="
echo "  $ kubectl exec -it <any-pod> -n data-pipelines -- df -h /data/replay"
echo "  $ kubectl exec -it <any-pod> -n data-pipelines -- du -sh /data/replay/* | sort -rh | head"
echo "  Delete old replay files, or expand the PVC"
echo ""

echo "=== Option A: argo retry (CORRECT for this situation) ==="
echo "  $ argo retry $WF -n $NS --restart-successful"
echo ""
echo "  --restart-successful: reuses output from passed steps"
echo "  Result: skips 50min of extract+transform, goes straight to write-hdf5"
echo "  Use when: fix is external (disk freed) and prior steps are idempotent"
echo ""

echo "=== Option B: argo resubmit (WRONG here — wastes 50 minutes) ==="
echo "  $ argo resubmit $WF -n $NS"
echo ""
echo "  resubmit: creates a brand new workflow, starts from scratch"
echo "  Re-pulls 10GB from Glacier, re-transforms everything"
echo "  Use when: prior step output is corrupted or non-deterministic"
echo ""

echo "=== Decision tree ==="
echo "  Prior steps still valid? → argo retry --restart-successful"
echo "  Prior output corrupted?  → argo resubmit"
echo "  Need different params?   → argo submit with new args"
""")

    print(f"""
{BOLD}── Read the 4-step workflow ─────────────────────────────{RESET}
{CYAN}  cat {DIRS["argo"]}/replay-pipeline-workflow.yaml{RESET}

{BOLD}── See which step failed ────────────────────────────────{RESET}
{CYAN}  cat {DIRS["logs"]}/replay-pipeline-get.txt{RESET}

{BOLD}── Read the error log ───────────────────────────────────{RESET}
{CYAN}  cat {DIRS["logs"]}/write-hdf5-replay-error.log{RESET}

{BOLD}── Study retry vs resubmit ──────────────────────────────{RESET}
{CYAN}  bash {DIRS["scripts"]}/k8s08_retry_vs_resubmit.sh{RESET}

{BOLD}── Key Interview Points ─────────────────────────────────{RESET}
  argo retry --restart-successful  = resume from failed step (reuse prior output)
  argo resubmit                    = brand new workflow, starts from scratch
  Always check: is the error transient (disk/OOM/network) or logic (code bug)?
  Transient → fix externally, then retry.  Logic bug → fix code, then resubmit.
""")


# ══════════════════════════════════════════════
#  SCENARIO 9 — Argo Workflows: CronWorkflow Missed Schedule
# ══════════════════════════════════════════════

def launch_scenario_9():
    header("Scenario K8-09 — Argo Workflows: CronWorkflow Missed Schedule")
    print("  A researcher reports: 'Yesterday's data is missing from HDF5.'")
    print("  The EOD tick ingestion CronWorkflow was supposed to run at 16:15 EST.")
    print("  argo list shows no run yesterday. Diagnose and manually re-trigger.\n")

    write(DIRS["argo"] / "tick-ingestion-cron.yaml", """\
# CronWorkflow: daily tick ingestion job
# Runs Mon-Fri at 16:15 EST (21:15 UTC)
apiVersion: argoproj.io/v1alpha1
kind: CronWorkflow
metadata:
  name: tick-ingestion-cron
  namespace: data-pipelines
spec:
  schedule: "15 21 * * 1-5"       # 21:15 UTC = 16:15 EST  (no DST adjustment!)
  timezone: "America/Chicago"      # ← if you set timezone here, schedule is in local time
  concurrencyPolicy: Forbid        # ← if previous run is still going, skip this one
  startingDeadlineSeconds: 300     # must start within 5min of schedule or skip
  suspend: false                   # ← set to true to pause the cron
  workflowSpec:
    entrypoint: tick-ingestion-dag
    serviceAccountName: argo-workflow-sa
    templates:
    - name: tick-ingestion-dag
      dag:
        tasks:
        - name: fetch-ticks
          template: fetch-tmpl
        - name: write-hdf5
          template: write-tmpl
          dependencies: [fetch-ticks]
        - name: update-index
          template: index-tmpl
          dependencies: [write-hdf5]
    - name: fetch-tmpl
      container:
        image: data-pipeline:v2.1.4
        command: [python3, fetch_ticks.py, "--date", "{{workflow.scheduledTime}}"]
    - name: write-tmpl
      container:
        image: data-pipeline:v2.1.4
        command: [python3, write_hdf5.py]
        resources:
          limits:
            memory: "4Gi"
    - name: index-tmpl
      container:
        image: data-pipeline:v2.1.4
        command: [python3, update_replay_index.py]
""")

    write(DIRS["logs"] / "argo-list-cron-workflows.txt", """\
# argo list -n data-pipelines --prefix tick-ingestion

NAME                              STATUS     AGE    DURATION  MESSAGE
tick-ingestion-cron-1746486900    Succeeded  3d     14m 22s
tick-ingestion-cron-1746573300    Succeeded  2d     15m 01s
tick-ingestion-cron-1746659700    Failed     1d     2m 14s    child 'write-hdf5' failed
tick-ingestion-cron-1746746100    -          -      -         (no run — missed schedule)

# Today is Mon 2026-05-04. Yesterday (Fri 2026-05-01) should have run.
# Row 3: ran but FAILED (write-hdf5 failed after 2min)
# Row 4: never triggered

# argo cron get tick-ingestion-cron -n data-pipelines
Name:                    tick-ingestion-cron
Namespace:               data-pipelines
Created:                 2026-01-10 09:00:00 +0000
Schedule:                15 21 * * 1-5
Timezone:                America/Chicago
ConcurrencyPolicy:       Forbid
Suspend:                 false
StartingDeadlineSeconds: 300
LastScheduledTime:       2026-05-01 21:15:00 +0000
Active Workflows:        tick-ingestion-cron-1746659700   ← previous run is still in Failed state
                                                            Forbid policy sees it as 'active' — skipped!
""")

    write(DIRS["logs"] / "argo-cron-missed-analysis.txt", """\
# WHY DID THE CRONWORKFLOW MISS ITS SCHEDULE?
# ============================================

Root cause: concurrencyPolicy=Forbid + previous workflow stuck in Failed state

Timeline:
  2026-04-30 21:15 UTC  → tick-ingestion-cron-1746659700 triggered
  2026-04-30 21:17 UTC  → write-hdf5 step failed (exit code 137, OOMKilled)
  2026-04-30 21:17 UTC  → workflow status: Failed  (but Argo counts it as 'active')

  2026-05-01 21:15 UTC  → CronWorkflow fires next schedule
  2026-05-01 21:15 UTC  → concurrencyPolicy=Forbid: "previous workflow still active?" YES
  2026-05-01 21:15 UTC  → SKIP — no workflow triggered
  2026-05-01 21:15 UTC  → startingDeadlineSeconds=300 expires — permanently missed

Result: no tick data ingested for 2026-05-01

# COMMON CAUSES OF MISSED CRONWORKFLOW SCHEDULES:
#
# 1. concurrencyPolicy=Forbid + previous run still running/failed
#    Fix: argo delete <old-wf> OR change policy to Replace
#
# 2. suspend: true
#    Fix: argo cron resume tick-ingestion-cron -n data-pipelines
#
# 3. Timezone mismatch (schedule was in UTC, expected EST)
#    Fix: set timezone field in CronWorkflow spec
#
# 4. startingDeadlineSeconds too short
#    Fix: increase to 3600 (1 hour) for tolerance
#
# 5. No worker nodes available at schedule time
#    Fix: check kubectl get nodes / cluster autoscaler logs
""")

    write(DIRS["scripts"] / "k8s09_cron_debug.sh", f"""\
#!/usr/bin/env bash
# K8-09: CronWorkflow missed schedule — debug + manual re-trigger
# ================================================================
CRON="tick-ingestion-cron"
NS="data-pipelines"
MISSED_DATE="2026-05-01"

echo "=== STEP 1: List recent workflow runs ==="
echo "  $ argo list -n $NS --prefix tick-ingestion"
cat {DIRS["logs"]}/argo-list-cron-workflows.txt
echo ""

echo "=== STEP 2: Get CronWorkflow status ==="
echo "  $ argo cron get $CRON -n $NS"
echo "  Key fields to check:"
echo "    Suspend: false/true"
echo "    LastScheduledTime"
echo "    Active Workflows (Forbid policy treats stuck workflows as active)"
echo ""
cat {DIRS["logs"]}/argo-list-cron-workflows.txt
echo ""

echo "=== STEP 3: Read root cause analysis ==="
cat {DIRS["logs"]}/argo-cron-missed-analysis.txt
echo ""

echo "=== STEP 4: Fix — delete the stuck workflow that blocked the cron ==="
echo "  $ argo delete tick-ingestion-cron-1746659700 -n $NS"
echo ""

echo "=== STEP 5: Manually re-trigger for the missed date ==="
echo "  $ argo submit -n $NS --from=cronwf/$CRON"
echo "    --parameter date=$MISSED_DATE"
echo "    --labels 'triggered-by=manual-backfill,reason=missed-schedule'"
echo ""
echo "  Note: workflow.scheduledTime won't be set for manual runs."
echo "  Pass the date explicitly as a parameter."
echo ""

echo "=== STEP 6: Watch the re-triggered run ==="
echo "  $ argo watch @latest -n $NS"
echo ""

echo "=== PREVENTION: fix the CronWorkflow config ==="
echo "  Change: concurrencyPolicy: Forbid"
echo "  To:     concurrencyPolicy: Replace   (cancels previous, starts fresh)"
echo "  Or increase startingDeadlineSeconds to 3600"
echo ""
echo "  Add alert: if no workflow starts within 30min of schedule → page on-call"
""")

    print(f"""
{BOLD}── Read the CronWorkflow YAML ───────────────────────────{RESET}
{CYAN}  cat {DIRS["argo"]}/tick-ingestion-cron.yaml{RESET}

{BOLD}── See the missed run in argo list ──────────────────────{RESET}
{CYAN}  cat {DIRS["logs"]}/argo-list-cron-workflows.txt{RESET}

{BOLD}── Understand why it was missed ─────────────────────────{RESET}
{CYAN}  cat {DIRS["logs"]}/argo-cron-missed-analysis.txt{RESET}

{BOLD}── Full debug + re-trigger guide ────────────────────────{RESET}
{CYAN}  bash {DIRS["scripts"]}/k8s09_cron_debug.sh{RESET}

{BOLD}── Key Interview Points ─────────────────────────────────{RESET}
  CronWorkflow concurrencyPolicy options:
    Forbid  → skip new run if previous still active (can miss schedules!)
    Allow   → run multiple concurrently (risk: resource contention)
    Replace → cancel the old one and start fresh (usually safest for pipelines)

  Manual re-trigger:
    argo submit --from=cronwf/<name>  --parameter date=<date>

  Always check: suspend, lastScheduledTime, active workflows under Forbid policy
""")


# ══════════════════════════════════════════════
#  TEARDOWN / MAIN
# ══════════════════════════════════════════════

def teardown():
    header("Tearing Down K8s Lab")
    remove_lab_dir(LAB_ROOT)

def show_status():
    header("K8s Lab Status")
    for d in DIRS.values():
        if d.exists():
            ok(f"{d.name}/: {len(list(d.glob('*')))} files")
        else:
            warn(f"{d.name}/ not found")

def launch_scenario_99():
    for fn in [launch_scenario_1, launch_scenario_2, launch_scenario_3,
               launch_scenario_4, launch_scenario_5, launch_scenario_6,
               launch_scenario_7, launch_scenario_8, launch_scenario_9]:
        fn()

SCENARIO_MAP = {
    1:  (launch_scenario_1,  "K8-01  CrashLoopBackOff diagnosis"),
    2:  (launch_scenario_2,  "K8-02  Rolling deployment rollback"),
    3:  (launch_scenario_3,  "K8-03  Pod stuck Pending / ImagePull"),
    4:  (launch_scenario_4,  "K8-04  ArgoCD rollback + disable auto-sync"),
    5:  (launch_scenario_5,  "K8-05  Validate release before prod sync"),
    6:  (launch_scenario_6,  "K8-06  Behavioral — K8s story"),
    7:  (launch_scenario_7,  "K8-07  Argo Workflows — OOMKilled step"),
    8:  (launch_scenario_8,  "K8-08  Argo Workflows — retry vs resubmit"),
    9:  (launch_scenario_9,  "K8-09  Argo Workflows — CronWorkflow missed schedule"),
    99: (launch_scenario_99, "      ALL scenarios"),
}

def main():
    parser = argparse.ArgumentParser(
        description="Kubernetes & ArgoCD Challenge Lab",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(f"  {k:<4} {v}" for k, (_, v) in SCENARIO_MAP.items()))
    parser.add_argument("--scenario", "-s", type=int, choices=list(SCENARIO_MAP.keys()))
    parser.add_argument("--teardown", "-t", action="store_true")
    parser.add_argument("--status",         action="store_true")
    args = parser.parse_args()

    if args.teardown: teardown(); return
    if args.status:   show_status(); return

    create_dirs()

    if args.scenario:
        if args.scenario == 99:
            for fn, _ in list(SCENARIO_MAP.values())[:-1]:
                fn()
        else:
            fn, _ = SCENARIO_MAP[args.scenario]; fn()
    else:
        header("Kubernetes & ArgoCD Challenge Lab")
        for num, (_, desc) in SCENARIO_MAP.items():
            print(f"    {num:<4} {desc}")
        choice = input("\n  Enter scenario number (or q): ").strip()
        if choice.lower() == "q": return
        try:
            if int(choice) == 99:
                for fn, _ in list(SCENARIO_MAP.values())[:-1]: fn()
            else:
                fn, _ = SCENARIO_MAP[int(choice)]; fn()
        except (KeyError, ValueError):
            print(f"{RED}  Invalid choice{RESET}")

    print(f"\n{BOLD}{SEP}{RESET}\n{GREEN}{BOLD}  Lab is live.{RESET}\n"
          f"{CYAN}    python3 lab_k8s.py --status\n"
          f"    python3 lab_k8s.py --teardown{RESET}\n{BOLD}{SEP}{RESET}\n")

if __name__ == "__main__":
    main()
