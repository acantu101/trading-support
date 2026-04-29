# Runbook: Monitoring & Observability

## Overview

This runbook covers monitoring practices for a trading system support engineer: latency percentiles, CloudWatch dashboards, Prometheus/Grafana, alert triage, and trading system KPIs. All scenarios map to `lab_monitoring.py` (M-01 through M-05).

---

## M-01 — Latency Percentiles

### What percentiles mean

| Percentile | Meaning | Example |
|---|---|---|
| p50 (median) | Half of requests are faster than this | Typical request experience |
| p95 | 95% of requests are faster — 1 in 20 is slower | Early warning |
| p99 | 99% of requests are faster — 1 in 100 is slower | **SLA level for trading** |
| p99.9 | 999 in 1000 are faster | Catches GC pauses, lock timeouts |

**Why average is misleading for trading:**

```
Period: 900 requests at 50ms, 100 requests at 4800ms (GC pause)
Average: (900×50 + 100×4800) / 1000 = 525ms  ← looks bad but survivable
p99:     4800ms                               ← 100 orders/hour hitting 5s delays
```

The average hides the fact that 100 orders per 1000 are hitting severe delays. In a system processing 1000 orders/minute, that is 100 severely delayed orders per minute.

### Calculating percentiles in Python

```python
def percentile(data: list, p: float) -> float:
    sorted_data = sorted(data)
    idx = (p / 100) * (len(sorted_data) - 1)
    lower = int(idx)
    frac  = idx - lower
    if lower + 1 >= len(sorted_data):
        return sorted_data[-1]
    return sorted_data[lower] * (1 - frac) + sorted_data[lower + 1] * frac

latencies = [45, 52, 48, 4800, 51, 47, 4750, 50, 53, ...]
p99 = percentile(latencies, 99)
```

### Latency signatures and what they indicate

| Pattern | Meaning |
|---|---|
| p50 normal, p99 high | Outliers — GC pauses, lock timeouts, slow DB queries |
| All percentiles rising together | Overloaded service — too many requests |
| p99 spikes at regular intervals | Scheduled job contending with requests (GC, batch) |
| p99 fine, p99.9 extreme | Rare but severe outliers — worth investigating if trading |

### Trading system SLAs

| Service | p99 SLA | Critical threshold |
|---|---|---|
| Order routing (FIX in → exchange out) | 200ms | 500ms |
| Pre-trade risk check | 100ms | 200ms |
| Position calculation | 500ms | 2000ms |
| Market data tick-to-trade | 50ms | 100ms |

---

## M-02 — CloudWatch Metrics

### Get metric data

```bash
# Get p99 latency for the last hour (1-minute resolution)
aws cloudwatch get-metric-statistics \
  --namespace Trading/OrderRouter \
  --metric-name P99Latency \
  --period 60 \
  --statistics Maximum \
  --start-time $(date -u -d '1 hour ago' '+%Y-%m-%dT%H:%M:%SZ') \
  --end-time $(date -u '+%Y-%m-%dT%H:%M:%SZ') \
  --output table

# List all metrics in a namespace
aws cloudwatch list-metrics --namespace Trading/OrderRouter

# Get current alarm states
aws cloudwatch describe-alarms --state-value ALARM
```

### Reading a CloudWatch dashboard during an incident

**Step 1 — Look for which panels are red (threshold breached)**

**Step 2 — Find the earliest breach by timestamp** — that is the root cause direction

**Step 3 — Check the cascade:** one service's metrics degrade → upstream services degrade

```
Example cascade:
  09:45  JVM heap 92%       ← root cause
  09:46  Kafka lag 1800     ← GC pauses → consumer can't process
  09:47  Risk check p99 2s  ← lag means stale risk data
  09:48  Order router p99 5s ← waiting for risk checks
```

**Step 4 — Note traffic level (requests/sec):** a traffic spike is different from a degradation

### CloudWatch metric types for trading

| Metric | Namespace | What to watch |
|---|---|---|
| P99Latency | Trading/OrderRouter | > 200ms = SLA breach |
| ConsumerLag | Trading/Kafka | > 500 = falling behind |
| HeapUsedPercent | Trading/JVM | > 85% = GC pressure |
| ErrorRatePercent | Trading/OrderRouter | > 1% = investigate |
| FIXSessionConnected | Trading/FIX | 0 = trading halted |
| FeedGapCount | Trading/MarketData | > 0 = investigate |
| DBQueryP99 | Trading/Database | > 2000ms = slow query |

### CloudWatch alarm creation (reference)

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name OrderRouter-P99Latency-High \
  --namespace Trading/OrderRouter \
  --metric-name P99Latency \
  --statistic Maximum \
  --period 60 \
  --evaluation-periods 2 \
  --threshold 200 \
  --comparison-operator GreaterThanThreshold \
  --treat-missing-data breaching \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:trading-oncall \
  --alarm-description "Order router p99 exceeds SLA. Runbook: https://wiki/runbooks/order-router-latency"
```

---

## M-03 — Prometheus & Grafana

### Prometheus metric types

| Type | Behaviour | Use case | Example |
|---|---|---|---|
| Counter | Always increases, resets on restart | Totals: fills, errors, bytes | `trade_fills_total` |
| Gauge | Can go up or down | Current state: lag, heap, connections | `kafka_consumer_lag_records` |
| Histogram | Samples into buckets | Latency distribution, request sizes | `order_router_duration_seconds` |
| Summary | Pre-calculated quantiles | Similar to histogram, less flexible | Avoid — prefer histogram |

### Essential PromQL queries

```promql
# Current Kafka consumer lag (all partitions)
kafka_consumer_lag_records{group="risk-engine-group"}

# Total lag across all partitions
sum(kafka_consumer_lag_records{group="risk-engine-group"})

# Fill rate per second (5-minute window)
rate(trade_fills_total{status="filled"}[5m])

# Rejection rate as % of all orders
rate(trade_fills_total{status="rejected"}[5m])
/ rate(trade_fills_total[5m]) * 100

# p99 latency from histogram
histogram_quantile(0.99,
  rate(order_router_request_duration_seconds_bucket[5m])
)

# JVM heap used in GB
jvm_memory_used_bytes{area="heap"} / 1024 / 1024 / 1024

# FIX sessions that are down
fix_session_connected == 0

# Alert: any partition lag > 500
kafka_consumer_lag_records > 500

# Error rate over 1%
rate(http_requests_total{status=~"5.."}[5m])
/ rate(http_requests_total[5m]) * 100 > 1
```

### Prometheus text format

```
# HELP trade_fills_total Total trade fills processed
# TYPE trade_fills_total counter
trade_fills_total{service="order-router",symbol="AAPL",status="filled"} 4821
trade_fills_total{service="order-router",symbol="AAPL",status="rejected"}  42

# HELP kafka_consumer_lag_records Current consumer lag
# TYPE kafka_consumer_lag_records gauge
kafka_consumer_lag_records{group="risk-engine",topic="trade-executions",partition="0"} 5
kafka_consumer_lag_records{group="risk-engine",topic="trade-executions",partition="1"} 847
```

Labels (`{key="value"}`) differentiate instances of the same metric. A unique combination of metric name + labels = one time series.

### Grafana panel types

| Panel | Use case |
|---|---|
| Time Series | Latency, lag, rates over time — most common |
| Gauge | Current value with threshold colours (heap %, error rate) |
| Stat | Single big number (fills today, uptime, sessions up) |
| Table | Multi-column data (per-partition lag, per-symbol fill count) |
| Bar Chart | Comparison across services or symbols |
| Heatmap | Latency histogram over time |

---

## M-04 — Alert Triage Workflow

### The 8-step process

```
1. ACKNOWLEDGE (< 5 min)
   Acknowledge in PagerDuty — team knows it is being worked.
   Note exact time you started.

2. READ THE ALERT
   What metric? What threshold? What service? When did it fire?
   Note all related alarms also firing.

3. FIND THE ROOT CAUSE DIRECTION
   Sort firing alarms by timestamp — earliest = root cause direction.
   Ask: is this a cascade? (service A failure causing service B failure?)

4. CHECK LOGS
   Go to CloudWatch logs for the affected service.
   Filter the time window: 2 minutes before the alarm fired.
   Look for: exceptions, GC events, connection errors, timeout messages.

5. ASSESS BLAST RADIUS
   How many traders / orders are affected?
   Is live trading impacted or is this a back-office issue?
   Are orders being rejected or just slow?

6. MITIGATE
   Choose the least disruptive fix that restores service:
   - Restart the pod (fastest)
   - Roll back the deployment (if recent deploy caused it)
   - Scale up replicas (if overloaded)
   - Fix config/IAM (if access issue)

7. VERIFY RECOVERY
   Watch the dashboard — metrics should return to normal within 1-2 minutes.
   Confirm with trading desk that orders are flowing.
   Verify Kafka lag drains.

8. POST-INCIDENT COMMUNICATION
   Write a brief update: what fired, what you found, what you did, status.
   Open a ticket for permanent fix if needed.
```

### Communication template

```
[INC-XXXX UPDATE] Root cause identified as [what].
Mitigated at [time] by [action].
[Metric] recovering — monitoring.
Ticket [JIRA-XXX] opened for permanent fix.
```

### Common alert patterns and responses

| Alert | Check first | Common cause | Fix |
|---|---|---|---|
| Order router p99 high | Risk engine lag, JVM heap | GC pause cascading | Restart risk-engine pod |
| FIX session disconnected | FIX session logs for reject reason | Sequence gap, heartbeat timeout | Reconnect / restart FIX session |
| Kafka consumer lag high | Consumer pod CPU/heap, broker health | GC pause, broker rebalance | Restart consumer, check broker |
| JVM heap > 85% | GC logs for Full GC events | Memory leak, undersized heap | Restart pod, increase -Xmx |
| DB query slow | pg_stat_activity for blocked queries | Missing index, lock contention | Kill blocking query, add index |
| Feed gap count rising | Market data feed logs | Network packet loss, reconnect | Feed handler auto-recovers; if not, restart |
| S3 access denied | IAM policy for the service role | Missing policy statement | Add permission, redeploy pod |

---

## M-05 — Trading System KPIs & Alert Thresholds

### Full KPI reference

**Order Routing**

| Metric | Warn | Critical | Notes |
|---|---|---|---|
| Order router p99 latency | 100ms | 200ms | End-to-end: FIX in → exchange out |
| Order error rate | 0.5% | 2% | % of orders rejected or errored |
| FIX session connected | — | 0 | 0 = trading halted immediately |
| Orders per second | — | — | Alert on unusual drops or spikes |

**Market Data**

| Metric | Warn | Critical | Notes |
|---|---|---|---|
| Feed gap count | 1 | 5/min | Any gap = investigate |
| Book staleness | 5s | 30s | Seconds since last update per symbol |
| Tick rate | — | 0 | Drop to 0 = feed disconnected |

**Risk & Positions**

| Metric | Warn | Critical | Notes |
|---|---|---|---|
| Position calc lag | 100ms | 500ms | Staleness of position data |
| Risk check p99 | 50ms | 100ms | Pre-trade check latency |
| Margin utilization | 80% | 95% | % of margin limit used |

**Infrastructure**

| Metric | Warn | Critical | Notes |
|---|---|---|---|
| JVM heap used % | 75% | 85% | GC pressure imminent above 85% |
| Kafka consumer lag | 100 | 500 | Per partition; unassigned = critical |
| DB query p99 | 500ms | 2000ms | Slow query or lock contention |
| CPU utilization | 70% | 90% | Sustained 90% = runaway or undersized |
| Disk used % | 75% | 90% | Logs fill disk fast |

### Monitoring philosophy

**Alert on symptoms, not just causes:**
- Cause metrics: CPU %, heap % — help root cause analysis but don't always mean service is broken
- Symptom metrics: p99 > SLA, FIX session down, fill count = 0 — always page

**Every alarm needs a runbook link:**
```
--alarm-description "P99 latency high. Runbook: https://wiki/runbooks/order-router"
```

**Dashboard hierarchy:**
```
Level 1: Is the system working? (fills flowing, FIX up, no red panels)
Level 2: Service health (latency, error rate, throughput per service)
Level 3: Infrastructure (JVM, Kafka, DB per service)
Level 4: Drill-down (logs, thread dumps, GC logs when root causing)
```

---

## Escalation Criteria

| Condition | Action |
|---|---|
| FIX session down for > 2 minutes | Escalate — trading impact, involve exchange connectivity team |
| Order router p99 > 1s for > 5 minutes | Escalate — all orders impacted |
| Multiple services degraded simultaneously | Escalate — likely infrastructure issue (network, AWS AZ) |
| Monitoring system itself unavailable | Escalate — flying blind, assume worst, check services manually |
| Persistent feed gaps not self-healing | Escalate — network or exchange issue |
