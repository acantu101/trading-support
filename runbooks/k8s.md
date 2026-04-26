# Runbook: Kubernetes & ArgoCD

## Overview

This runbook covers Kubernetes pod debugging, deployment issues, configuration problems, and ArgoCD sync failures in a trading environment. All scenarios map to `lab_k8s.py` (K8-01 through K8-06).

---

## Diagnostic First Steps

```bash
# Always start here — get the full picture
kubectl get pods -n <namespace>            # pod statuses
kubectl get events -n <namespace> --sort-by='.lastTimestamp' | tail -20
kubectl describe pod <pod-name> -n <namespace>   # events + resource states
kubectl logs <pod-name> -n <namespace>           # stdout/stderr
kubectl logs <pod-name> -n <namespace> --previous  # logs from crashed container
```

---

## K8-01 — CrashLoopBackOff

### Symptoms
- Pod status shows `CrashLoopBackOff`
- `RESTARTS` counter incrementing in `kubectl get pods`

### Diagnosis

```bash
# 1. Get the exit reason
kubectl describe pod <pod> -n <ns>
# Look for: "Last State: Terminated" → reason, exit code, message

# 2. Read the crash logs (from the terminated container)
kubectl logs <pod> -n <ns> --previous
kubectl logs <pod> -n <ns> -c <container>   # if multi-container pod

# 3. Check events for clues
kubectl get events -n <ns> --field-selector involvedObject.name=<pod>

# 4. Check resource limits — OOMKilled?
kubectl describe pod <pod> -n <ns> | grep -A5 "Limits\|Requests\|OOM"
```

### Common Exit Codes

| Exit Code | Meaning |
|-----------|---------|
| 0 | Clean exit (unexpected if container should run forever) |
| 1 | Application error — check logs |
| 137 | SIGKILL — likely OOMKilled; check `kubectl describe` for `OOMKilled: true` |
| 139 | Segmentation fault |
| 143 | SIGTERM — container was asked to stop (may be liveness probe failure) |

### Resolution

```bash
# OOMKilled → increase memory limit
kubectl edit deployment <name> -n <ns>
# Or patch directly:
kubectl patch deployment <name> -n <ns> --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"512Mi"}]'

# Application crash → fix the config/code issue, then roll out:
kubectl rollout restart deployment/<name> -n <ns>

# Confirm recovery
kubectl rollout status deployment/<name> -n <ns>
kubectl get pods -n <ns> -w
```

---

## K8-02 — Pod Stuck in Pending

### Symptoms
- Pod status `Pending` for more than 30 seconds
- Not progressing to `ContainerCreating` or `Running`

### Diagnosis

```bash
# Most important — read the events
kubectl describe pod <pod> -n <ns>
# Look for: "FailedScheduling" event with reason

# Check node resources
kubectl describe nodes | grep -A5 "Allocated resources"
kubectl top nodes    # requires metrics-server

# Check if any nodes match the pod's selector/toleration
kubectl get nodes --show-labels
kubectl describe pod <pod> -n <ns> | grep -A5 "Node-Selectors\|Tolerations"

# Check PersistentVolumeClaims (if pod needs storage)
kubectl get pvc -n <ns>
kubectl describe pvc <pvc-name> -n <ns>
```

### Common Causes

| Event Message | Cause | Fix |
|---------------|-------|-----|
| `Insufficient cpu` | No node has enough CPU | Scale up nodes or reduce CPU request |
| `Insufficient memory` | No node has enough memory | Scale up nodes or reduce memory request |
| `0/3 nodes are available: 3 node(s) had taint` | Node taint not matched | Add toleration to pod spec |
| `no matching node` | nodeSelector mismatch | Fix node label or selector |
| `PVC not bound` | Storage class unavailable | Check storage class and provisioner |

### Resolution

```bash
# Lower resource requests (if pod is over-requesting)
kubectl edit deployment <name> -n <ns>
# resources:
#   requests:
#     cpu: "100m"      ← lower this
#     memory: "128Mi"  ← lower this

# Add toleration for a tainted node
# spec:
#   tolerations:
#   - key: "trading"
#     operator: "Equal"
#     value: "true"
#     effect: "NoSchedule"

# Force schedule on a specific node (debugging only)
kubectl patch pod <pod> -n <ns> -p '{"spec":{"nodeName":"<node-name>"}}'
```

---

## K8-03 — Failed Rolling Deployment

### Symptoms
- `kubectl rollout status` shows `Waiting for deployment to finish`
- New pods in `CrashLoopBackOff` or `ImagePullBackOff`
- Old pods still running (rollout stalled)

### Diagnosis

```bash
# Check rollout status
kubectl rollout status deployment/<name> -n <ns>

# Check the new pod's problem
kubectl get pods -n <ns>          # look for new pods in bad state
kubectl logs <new-pod> -n <ns> --previous
kubectl describe pod <new-pod> -n <ns>

# View rollout history
kubectl rollout history deployment/<name> -n <ns>
```

### Resolution — Rollback

```bash
# Roll back to the previous version immediately
kubectl rollout undo deployment/<name> -n <ns>

# Roll back to a specific revision
kubectl rollout undo deployment/<name> --to-revision=2 -n <ns>

# Verify rollback succeeded
kubectl rollout status deployment/<name> -n <ns>
kubectl get pods -n <ns>

# View what changed between revisions
kubectl rollout history deployment/<name> -n <ns> --revision=2
kubectl rollout history deployment/<name> -n <ns> --revision=3
```

### ImagePullBackOff

```bash
# Diagnose
kubectl describe pod <pod> -n <ns> | grep -A5 "Failed\|Error"
# Look for: "Failed to pull image" + reason

# Common causes:
# - Image tag doesn't exist (typo or not pushed)
# - Private registry requires imagePullSecret
# - Rate limited by Docker Hub

# Fix imagePullSecret
kubectl create secret docker-registry regcred \
  --docker-server=<registry> \
  --docker-username=<user> \
  --docker-password=<pass>
kubectl patch serviceaccount default -n <ns> \
  -p '{"imagePullSecrets": [{"name": "regcred"}]}'
```

---

## K8-04 — ConfigMap and Secret Misconfiguration

### Symptoms
- App crashes on startup with `config file not found` or `key not found`
- Environment variable missing or wrong value

### Diagnosis

```bash
# List configmaps and secrets
kubectl get configmaps -n <ns>
kubectl get secrets -n <ns>

# Inspect a configmap
kubectl describe configmap <name> -n <ns>
kubectl get configmap <name> -n <ns> -o yaml

# Decode a secret (base64 encoded)
kubectl get secret <name> -n <ns> -o jsonpath='{.data.<key>}' | base64 -d

# Check what env vars the pod actually sees
kubectl exec -it <pod> -n <ns> -- env | sort
kubectl exec -it <pod> -n <ns> -- cat /etc/config/<filename>
```

### Common Issues

```yaml
# WRONG: key name mismatch
envFrom:
  - configMapRef:
      name: oms-config   # configmap exists but key is "DATABASE_HOST" not "DB_HOST"

# WRONG: secret not base64 encoded in the YAML
apiVersion: v1
kind: Secret
data:
  password: mysecret   # MUST be base64: echo -n 'mysecret' | base64

# RIGHT
data:
  password: bXlzZWNyZXQ=
```

### Resolution

```bash
# Update a configmap
kubectl edit configmap <name> -n <ns>

# Or replace entirely
kubectl create configmap <name> --from-file=config.yml \
  --dry-run=client -o yaml | kubectl apply -f -

# After changing configmap/secret, pods usually need a restart to pick up changes
kubectl rollout restart deployment/<name> -n <ns>
```

---

## K8-05 — Service Not Routing Traffic

### Symptoms
- Pods are Running but requests return connection refused or timeout
- `kubectl exec` into one pod and `curl` to service IP fails

### Diagnosis

```bash
# 1. Does the service exist and have the right port?
kubectl get service <name> -n <ns>
kubectl describe service <name> -n <ns>

# 2. Does the service selector match the pod labels?
kubectl describe service <name> -n <ns> | grep Selector
kubectl get pods -n <ns> --show-labels | grep <expected-label>

# 3. Are there endpoints? (If empty, selector is wrong)
kubectl get endpoints <service-name> -n <ns>
# Should show: <pod-ip>:<port>  — if empty, selector mismatch

# 4. Test from inside the cluster
kubectl run debug --rm -it --image=busybox -- sh
  wget -qO- http://<service-name>.<namespace>.svc.cluster.local:<port>/health
```

### Resolution

```bash
# Fix selector mismatch — update service selector to match pod labels
kubectl edit service <name> -n <ns>
# spec:
#   selector:
#     app: oms-server        ← must exactly match pod label

# Verify endpoints appear after fix
kubectl get endpoints <name> -n <ns>
# Should now show pod IPs

# If targetPort is wrong
kubectl patch service <name> -n <ns> --type=json \
  -p='[{"op":"replace","path":"/spec/ports/0/targetPort","value":8080}]'
```

---

## K8-06 — ArgoCD Sync Failure

### Symptoms
- ArgoCD app shows `OutOfSync` or `Degraded`
- Sync operation shows errors in the ArgoCD UI
- Drift detected between Git state and cluster state

### Diagnosis

```bash
# ArgoCD CLI
argocd app get <app-name>
argocd app diff <app-name>    # shows diff between Git and cluster
argocd app sync <app-name> --dry-run

# kubectl — check ArgoCD application resource
kubectl describe application <app-name> -n argocd

# View sync status
argocd app list   # STATUS and HEALTH columns

# What changed?
argocd app history <app-name>   # sync history with commit SHAs
```

### Common Sync Failure Causes

| Error | Cause | Fix |
|-------|-------|-----|
| `ComparisonError: failed to load` | YAML parse error in repo | Fix YAML syntax |
| `Resource already exists` | Resource created outside ArgoCD | Add to ArgoCD management or delete orphan |
| `PermissionDenied` | ArgoCD service account missing RBAC | Add ClusterRole/RoleBinding |
| `Hook failed` | Pre/post-sync hook job failed | Check hook job logs |
| `Health check failed` | Deployment not reaching healthy state | Debug underlying deployment |

### Resolution

```bash
# Hard refresh — re-fetch from Git ignoring cache
argocd app get <app-name> --hard-refresh

# Sync with force (overwrites manual cluster changes)
argocd app sync <app-name> --force

# Sync a specific resource only
argocd app sync <app-name> --resource apps:Deployment:<name>

# If an out-of-band resource is blocking sync, mark it as managed or delete it
kubectl delete <resource> <name> -n <ns>

# Refresh and re-sync after fix
argocd app sync <app-name>
argocd app wait <app-name> --health
```

### Drift Prevention
- Never apply changes to the cluster manually outside of Git (no `kubectl apply` in prod)
- Set ArgoCD to `auto-sync` + `self-heal` for non-production environments
- For production: manual sync gate with approval in the ArgoCD UI

---

## Essential kubectl Cheat Sheet

```bash
# Pod lifecycle
kubectl get pods -n <ns> -w                    # watch changes
kubectl describe pod <pod> -n <ns>             # events + state
kubectl logs <pod> -n <ns> --previous          # crashed container logs
kubectl exec -it <pod> -n <ns> -- bash         # shell into running pod
kubectl delete pod <pod> -n <ns>               # force pod reschedule

# Deployments
kubectl rollout status deployment/<name> -n <ns>
kubectl rollout undo deployment/<name> -n <ns>
kubectl rollout restart deployment/<name> -n <ns>
kubectl scale deployment <name> --replicas=3 -n <ns>

# Networking
kubectl get svc -n <ns>
kubectl get endpoints -n <ns>
kubectl port-forward pod/<pod> 8080:8080 -n <ns>  # debug locally

# Config
kubectl get configmap <name> -n <ns> -o yaml
kubectl get secret <name> -n <ns> -o jsonpath='{.data}' | base64 -d

# Resources
kubectl top pods -n <ns>
kubectl top nodes
kubectl describe node <node>
```

---

## Escalation Criteria

| Condition | Action |
|-----------|--------|
| All nodes NotReady | Infrastructure / control plane incident |
| etcd unhealthy | Escalate immediately — cluster state at risk |
| PersistentVolume lost or corrupted | Escalate — potential data loss |
| CrashLoopBackOff after rollback | Escalate to dev — code issue not deployment issue |
| ArgoCD controller unresponsive | Restart ArgoCD controller pod in argocd namespace |
