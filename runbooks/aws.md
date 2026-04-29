# Runbook: AWS & Cloud

## Overview

This runbook covers AWS operations for a trading system support engineer: CloudWatch log queries, S3 market data operations, IAM permission diagnosis, alarm triage, and EKS node health. All scenarios map to `lab_aws.py` (A-01 through A-05).

The lab uses mock data — no AWS credentials needed to practice. Production work requires appropriate IAM role access.

---

## A-01 — CloudWatch Log Insights

### Connect to logs

```bash
# AWS Console: CloudWatch → Log groups → /trading/order-router → Log Insights
# AWS CLI equivalents:

# List log groups for trading services
aws logs describe-log-groups --log-group-name-prefix /trading

# Tail a log stream live
aws logs tail /trading/order-router --follow

# Run a Log Insights query via CLI
aws logs start-query \
  --log-group-name /trading/order-router \
  --start-time $(date -d '1 hour ago' +%s) \
  --end-time $(date +%s) \
  --query-string 'fields @timestamp, @message | filter @message like /ERROR/ | sort @timestamp desc | limit 50'
```

### Common Log Insights queries

```sql
-- All errors in the last hour
fields @timestamp, @message
| filter @message like /ERROR/
| sort @timestamp desc
| limit 100

-- Count errors per service
fields @logStream, @message
| filter @message like /ERROR/
| stats count(*) as error_count by @logStream
| sort error_count desc

-- Find slow DB queries (>500ms)
fields @timestamp, @message
| filter @message like /exceeded SLA/ or @message like /ms)/
| sort @timestamp desc

-- Count by log level
fields @message
| parse @message "* [*] *" as timestamp, level, rest
| stats count(*) by level

-- Find messages in a specific time window
fields @timestamp, @message
| filter @timestamp >= 1745836200000 and @timestamp <= 1745839800000
| filter @message like /CRITICAL/

-- Correlation: find all events within 5 seconds of an error
fields @timestamp, @message
| filter @message like /NullPointerException/
```

### Log group structure for trading

| Log Group | Contents |
|---|---|
| `/trading/order-router` | FIX messages in/out, order routing decisions |
| `/trading/risk-engine` | Pre-trade risk check results, position updates |
| `/trading/market-data-feed` | Tick ingestion, feed gaps, reconnects |
| `/trading/position-service` | Position recalculations, reconciliation results |
| `/trading/fix-session` | Session lifecycle: logon, heartbeat, gaps, logout |

### Key CloudWatch log concepts

| Concept | Meaning |
|---|---|
| Log Group | One per service — `/trading/order-router` |
| Log Stream | One per pod instance inside the group |
| Log Insights | SQL-like query language — cross-group search |
| Metric Filter | Pattern match on logs → emit a CloudWatch metric |
| Retention | Logs expire after N days — check before searching old incidents |
| Subscription Filter | Stream logs to Kinesis/Lambda in real time |

---

## A-02 — S3 Market Data Operations

### Common S3 commands

```bash
# List tick files for a date
aws s3 ls s3://drw-market-data/ticks/2026/04/28/

# List recursively with sizes
aws s3 ls s3://drw-market-data/ticks/ --recursive --human-readable

# Download a tick file
aws s3 cp s3://drw-market-data/ticks/2026/04/28/AAPL_20260428.csv.gz .

# Sync a full day's data locally
aws s3 sync s3://drw-market-data/ticks/2026/04/28/ ./local_data/

# Check object metadata (size, last modified, ETag)
aws s3api head-object --bucket drw-market-data \
  --key ticks/2026/04/28/AAPL_20260428.csv.gz

# Generate a presigned URL (gives temp access without IAM credentials)
aws s3 presign s3://drw-market-data/ticks/2026/04/28/AAPL_20260428.csv.gz \
  --expires-in 3600

# Check bucket versioning
aws s3api get-bucket-versioning --bucket drw-market-data
```

### S3 bucket layout for trading

```
s3://drw-market-data/
  ticks/YYYY/MM/DD/SYMBOL_YYYYMMDD.csv.gz     ← raw tick data (compressed)
  processed/YYYY/MM/DD/trades_partitioned.parquet ← Athena-queryable
  reports/YYYY/MM/DD/regulatory_report.json   ← compliance outputs
  snapshots/YYYY/MM/DD/positions_eod.json     ← end-of-day positions
```

### S3 storage classes

| Class | Use case | Cost |
|---|---|---|
| Standard | Hot data — last 30 days of ticks | Highest |
| Standard-IA | Warm data — 30-90 days | Lower |
| Glacier | Cold archive — regulatory retention (7 years) | Lowest |
| Intelligent-Tiering | Auto-moves between tiers | Medium |

### Lifecycle rule example (cost control)

```json
{
  "Rules": [{
    "Filter": {"Prefix": "ticks/"},
    "Transitions": [
      {"Days": 30,  "StorageClass": "STANDARD_IA"},
      {"Days": 90,  "StorageClass": "GLACIER"}
    ],
    "Status": "Enabled"
  }]
}
```

---

## A-03 — IAM & Permissions

### Diagnosing AccessDenied

```
Error signature:
  AmazonS3Exception: Access Denied (Status Code: 403; Error Code: AccessDenied)
  Bucket: drw-market-data
  Key: ticks/2026/04/28/AAPL_20260428.csv.gz
```

**Triage steps:**

```bash
# 1. Find what IAM role the pod is using
kubectl describe pod <pod-name> -n trading | grep serviceAccountName

# 2. Find the IAM role attached to that service account (IRSA)
kubectl get serviceaccount <name> -n trading -o yaml | grep role-arn

# 3. List policies attached to the role
aws iam list-attached-role-policies --role-name market-data-feed-role

# 4. Read the policy document
aws iam get-policy --policy-arn arn:aws:iam::123456789012:policy/MarketDataReadPolicy
aws iam get-policy-version \
  --policy-arn arn:aws:iam::123456789012:policy/MarketDataReadPolicy \
  --version-id v1

# 5. Simulate the permission check
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::123456789012:role/market-data-feed-role \
  --action-names s3:GetObject \
  --resource-arns arn:aws:s3:::drw-market-data/ticks/2026/04/28/AAPL_20260428.csv.gz
```

### IAM policy structure

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3ReadMarketData",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::drw-market-data",
        "arn:aws:s3:::drw-market-data/*"
      ]
    }
  ]
}
```

**Common mistake:** granting `s3:GetObject` on `arn:aws:s3:::bucket/*` but forgetting `s3:ListBucket` on `arn:aws:s3:::bucket` (without `/*`). Both are needed.

### IAM concepts

| Concept | Meaning |
|---|---|
| Role | Identity attached to a service/pod (not a user) |
| Policy | JSON document: allowed actions on which resources |
| ARN | Amazon Resource Name — unique ID for any AWS resource |
| Effect | `Allow` or `Deny` — Deny always wins |
| IRSA | IAM Roles for Service Accounts — how K8s pods get AWS credentials |
| SCP | Service Control Policy — org-level guardrails, overrides role policies |

### Common AccessDenied scenarios

| Error | Likely cause |
|---|---|
| S3 403 on `GetObject` | Missing `s3:GetObject` on `bucket/*` |
| S3 403 on `ListBucket` | Missing `s3:ListBucket` on `bucket` (not `bucket/*`) |
| CloudWatch 403 on `PutLogEvents` | Missing `logs:PutLogEvents` or wrong log group ARN |
| Secrets Manager 403 | Missing `secretsmanager:GetSecretValue` or wrong secret ARN |
| EKS 401 Unauthorized | IRSA not set up — pod using node role instead of service account role |

---

## A-04 — CloudWatch Alarm Triage

### Read alarm state

```bash
# List all alarms currently in ALARM state
aws cloudwatch describe-alarms --state-value ALARM

# Describe a specific alarm
aws cloudwatch describe-alarms --alarm-names OrderRouter-P99Latency-High

# Get recent alarm history (state changes)
aws cloudwatch describe-alarm-history \
  --alarm-name OrderRouter-P99Latency-High \
  --history-item-type StateUpdate \
  --max-records 10

# Get the metric datapoints that triggered it
aws cloudwatch get-metric-statistics \
  --namespace Trading/OrderRouter \
  --metric-name P99Latency \
  --period 60 \
  --statistics Maximum \
  --start-time 2026-04-28T09:30:00Z \
  --end-time 2026-04-28T10:00:00Z
```

### Cascade analysis

When multiple alarms fire together, find the root cause by timestamp:

```
09:31:01  MarketDataFeed-S3AccessErrors      ← FIRST = root cause
09:31:03  RiskEngine-ConsumerLag-High        ← cascade: no market data
09:31:05  OrderRouter-P99Latency-High        ← cascade: risk checks slow
```

**Rule:** the alarm that fired first is almost always the root cause. Later alarms are symptoms of the first failure cascading upstream.

### Alarm anatomy

| Field | Meaning |
|---|---|
| Namespace | Logical grouping — `Trading/OrderRouter` |
| MetricName | The measurement — `P99Latency` |
| Threshold | Value that triggers the alarm |
| Period | How often the metric is evaluated (60s, 300s) |
| EvaluationPeriods | How many periods must breach before alarm fires |
| TreatMissingData | What to do if no data — `breaching` is safest for trading |
| Actions | SNS topic → PagerDuty / Slack / email |

### Standard trading system alarms

| Alarm | Metric | Threshold | Severity |
|---|---|---|---|
| FIX session down | `fix_session_connected` | < 1 | CRITICAL |
| Order router p99 | `P99Latency` | > 200ms | CRITICAL |
| Kafka consumer lag | `ConsumerLag` | > 500 msgs | WARNING |
| JVM heap high | `HeapUsedPercent` | > 85% | WARNING |
| Error rate | `ErrorRatePercent` | > 1% | WARNING |
| DB query slow | `DBQueryP99` | > 2000ms | WARNING |

---

## A-05 — EKS Node Health

### Node triage

```bash
# Check node status
kubectl get nodes -o wide

# Describe a NotReady node
kubectl describe node <node-name>

# Find pods on a specific node
kubectl get pods --all-namespaces \
  --field-selector spec.nodeName=<node-name>

# Check aws-node DaemonSet (CNI plugin)
kubectl get pods -n kube-system -o wide | grep aws-node
kubectl logs -n kube-system aws-node-<id>

# Check node conditions
kubectl get node <node-name> -o jsonpath='{.status.conditions[*].type}'
```

### NotReady causes and fixes

| Cause | Log/Event signal | Fix |
|---|---|---|
| CNI not initialized | `NetworkPluginNotReady: cni plugin not initialized` | Restart `aws-node` pod on that node |
| Disk pressure | `DiskPressure=True` | Clean up `/var/log` or expand EBS volume |
| Memory pressure | `MemoryPressure=True` | Evict low-priority pods, scale node group |
| Node not joining | `node not found` in kube-apiserver | Check EC2 instance IAM role has `AmazonEKSWorkerNodePolicy` |
| Kubelet stopped | No heartbeat for 5+ minutes | SSH to node, `systemctl restart kubelet` |

### Drain and terminate a bad node

```bash
# Step 1: cordon (stop new pods scheduling here)
kubectl cordon <node-name>

# Step 2: drain (evict existing pods gracefully)
kubectl drain <node-name> \
  --ignore-daemonsets \
  --delete-emptydir-data \
  --grace-period=60

# Step 3: terminate the EC2 instance
# EKS managed node group will replace it automatically
aws ec2 terminate-instances --instance-ids <instance-id>

# Find the instance ID from the node name
aws ec2 describe-instances \
  --filters "Name=private-dns-name,Values=<node-name>" \
  --query 'Reservations[*].Instances[*].InstanceId'
```

### EKS concepts

| Concept | Meaning |
|---|---|
| Node Group | Pool of EC2 instances — `trading-workers` (m5.2xlarge) |
| aws-node | DaemonSet that runs on every node, assigns pod IPs from VPC |
| CNI | Container Network Interface — `aws-node` is the CNI plugin |
| IRSA | IAM Roles for Service Accounts — pod → IAM role via service account |
| Managed nodes | AWS handles patching; you set the AMI version |
| Karpenter | Alternative to node groups — auto-provisions right-sized nodes on demand |

---

## Escalation Criteria

| Condition | Action |
|---|---|
| Multiple nodes NotReady simultaneously | Escalate — possible AZ outage or VPC issue |
| IAM change needed in production | Raise change request — IAM changes require approval |
| S3 bucket policy change | Escalate — bucket policy changes are org-wide |
| CloudWatch showing no data (not ALARM, not OK) | Check metric is being emitted — service may be down entirely |
| EKS control plane unreachable | Escalate AWS Support — managed control plane issue |
