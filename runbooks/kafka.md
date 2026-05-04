# Runbook: Kafka

## Overview

This runbook covers Kafka operational issues in a trading environment: consumer group lag, broker failures, partition management, producer configuration, and market data pipeline operations. All scenarios map to `lab_kafka.py` (K-01 through K-12).

---

## Quick Reference — Key Commands

```bash
BROKER="localhost:9092"
TOPIC="trade-executions"
GROUP="risk-engine-group"

# List topics
kafka-topics.sh --bootstrap-server $BROKER --list

# Describe a topic
kafka-topics.sh --bootstrap-server $BROKER --describe --topic $TOPIC

# Check consumer group lag
kafka-consumer-groups.sh --bootstrap-server $BROKER --group $GROUP --describe

# Check under-replicated partitions
kafka-topics.sh --bootstrap-server $BROKER --describe --under-replicated-partitions

# List all consumer groups
kafka-consumer-groups.sh --bootstrap-server $BROKER --list
```

---

## K-02 — Consumer Group Lag

### Symptoms
- Risk engine processing delays
- Position updates falling behind trade events
- Monitoring alert: `consumer_lag > threshold`

### Diagnosis

```bash
# Get full lag picture
kafka-consumer-groups.sh \
  --bootstrap-server $BROKER \
  --group risk-engine-group \
  --describe

# Output columns:
# TOPIC | PARTITION | CURRENT-OFFSET | LOG-END-OFFSET | LAG | CONSUMER-ID | HOST

# Flag unassigned partitions (CONSUMER-ID = "-")
kafka-consumer-groups.sh ... --describe | awk '$7 == "-"'

# Watch lag in real time
watch -n 5 "kafka-consumer-groups.sh --bootstrap-server $BROKER --group $GROUP --describe"
```

### Reading the Lag Output

```
GROUP              TOPIC             PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG   CONSUMER-ID
risk-engine-group  trade-executions  0          10420           10425           5     risk-worker-1   ← OK
risk-engine-group  trade-executions  1          9800            10200           400   risk-worker-2   ← HIGH LAG
risk-engine-group  trade-executions  3          8500            10300           1800  -               ← NO CONSUMER
```

| Condition | Meaning | Fix |
|-----------|---------|-----|
| `CONSUMER-ID = "-"` | Partition unassigned — no consumer covering it | Add more consumer instances |
| Lag growing over time | Consumer too slow for throughput | Scale consumers, optimise processing |
| Lag stable but high | Consumer caught up to a plateau | Check batch size and `max.poll.records` |

### Resolution

```bash
# Too few consumers — add instances to match partition count
# Rule: consumers_in_group ≤ partition_count (extra consumers sit idle)

# If lag is due to slow processing, tune consumer:
# max.poll.records=100      (smaller batches = faster commit cycle)
# max.poll.interval.ms=60000  (increase if processing takes longer)

# For a temporary backlog — increase consumer instances
# Then reduce back to normal once caught up
```

---

## K-03 — Topic Operations

### Create a topic (trading-grade settings)

```bash
kafka-topics.sh \
  --bootstrap-server $BROKER \
  --create \
  --topic trade-executions \
  --partitions 12 \
  --replication-factor 3 \
  --config retention.ms=604800000 \
  --config min.insync.replicas=2

# Why these values:
#   partitions=12    → 12 consumers can process in parallel
#   RF=3             → tolerates 2 broker failures, no data loss
#   min.isr=2        → acks=all requires 2 replicas to confirm write
#   retention=7 days → 7-day replay window for reconciliation
```

### Inspect a topic

```bash
# Full partition/replica/leader layout
kafka-topics.sh --bootstrap-server $BROKER --describe --topic trade-executions

# All under-replicated partitions (non-empty = broker problem)
kafka-topics.sh --bootstrap-server $BROKER --describe --under-replicated-partitions

# All topics with configs
kafka-topics.sh --bootstrap-server $BROKER --describe
```

### Produce / consume for testing

```bash
# Produce a test message with symbol as key
echo 'AAPL:{"symbol":"AAPL","qty":100,"side":"BUY","price":185.50}' | \
  kafka-console-producer.sh \
    --bootstrap-server $BROKER \
    --topic trade-executions \
    --property "parse.key=true" \
    --property "key.separator=:"

# Consume from beginning
kafka-console-consumer.sh \
  --bootstrap-server $BROKER \
  --topic trade-executions \
  --from-beginning \
  --group test-group \
  --max-messages 10
```

### Why symbol as message key
- Same key → same partition (hash routing)
- All AAPL fills land on the same partition → strict ordering per symbol
- Without key → round-robin → AAPL fills interleaved → cannot guarantee fill order

---

## K-04 — Broker Down Incident

### Symptoms
- Producer exceptions: `LeaderNotAvailableException` or `NotEnoughReplicasException`
- Under-replicated partitions alert firing
- Consumer group rebalancing

### Diagnosis

```bash
# 1. Confirm broker is down
systemctl status kafka             # on the broker host
ss -tlnp | grep 9092               # check port not listening
journalctl -u kafka -n 100         # last 100 log lines

# 2. Find root cause in broker logs
grep "ERROR\|FATAL\|Exception" /var/log/kafka/server.log | tail -50

# 3. Check under-replicated partitions from a surviving broker
kafka-topics.sh \
  --bootstrap-server <surviving-broker>:9092 \
  --describe \
  --under-replicated-partitions

# 4. Are producers still working? (RF=3, min.isr=2 — 2 brokers still sufficient)
# With 2 remaining brokers and min.insync.replicas=2:
# acks=all producers WILL succeed — 2 ISR are available
# Monitor producer error rate in your metrics system
```

### Common Broker Crash Causes

| Log Message | Root Cause | Fix Before Restart |
|-------------|-----------|-------------------|
| `No space left on device` | Disk full | Clear old log segments or expand disk |
| `FATAL: too many connections` | Connection pool exhausted | Reduce connections from clients |
| `OutOfMemoryError` | JVM heap too small | Increase heap: `-Xmx6g` in kafka-env.sh |
| `SIGSEGV` / `Segmentation fault` | JVM or OS bug | Check kernel/JVM version, get core dump |

### Resolution Steps

```bash
# Step 1: Fix root cause BEFORE restarting
# Example: disk full
df -h /var/kafka
find /var/kafka/logs -name "*.log" -mtime +7 -delete
# Or increase retention policy:
kafka-configs.sh --bootstrap-server $BROKER --entity-type topics \
  --entity-name trade-executions --alter \
  --add-config retention.ms=172800000   # reduce to 2 days temporarily

# Step 2: Restart the broker
systemctl restart kafka
journalctl -u kafka -f   # watch startup

# Step 3: Verify URPs clear (may take several minutes for catch-up)
watch -n 5 "kafka-topics.sh --bootstrap-server $BROKER --describe --under-replicated-partitions"
# Should return empty once broker is fully caught up

# Step 4: Trigger preferred replica election to rebalance leaders
kafka-preferred-replica-election.sh --bootstrap-server $BROKER
```

---

## K-05 — Python Producer Configuration

### Production-grade producer

```python
from kafka import KafkaProducer
import json

producer = KafkaProducer(
    bootstrap_servers=['kafka-broker-1:9092', 'kafka-broker-2:9092'],
    value_serializer=lambda v: json.dumps(v).encode(),
    key_serializer=lambda k: k.encode(),
    acks='all',                  # wait for all ISR replicas — no silent loss
    enable_idempotence=True,     # deduplicate retries by sequence number
    retries=3,
    max_in_flight_requests_per_connection=5,
)

# Use symbol as key → same symbol → same partition → ordered fills
future = producer.send(
    'trade-executions',
    key='AAPL',
    value={'symbol': 'AAPL', 'qty': 100, 'side': 'BUY', 'price': 185.50},
)
meta = future.get(timeout=5)   # block and confirm delivery
producer.flush()
```

### Production-grade consumer

```python
from kafka import KafkaConsumer
import json

consumer = KafkaConsumer(
    'trade-executions',
    bootstrap_servers=['kafka-broker-1:9092'],
    group_id='risk-engine',
    value_deserializer=lambda m: json.loads(m.decode()),
    key_deserializer=lambda k: k.decode() if k else None,
    auto_offset_reset='earliest',
    enable_auto_commit=False,   # manual commit — no data loss on crash
    consumer_timeout_ms=5000,
)

for msg in consumer:
    process(msg.value)          # do your work first
    consumer.commit()           # THEN commit — at-least-once guarantee
```

---

## K-06 — Delivery Semantics

| Semantic | Producer Config | Risk in Trading | Use Case |
|----------|----------------|----------------|----------|
| At-most-once | `acks=0, retries=0` | Silent data loss — missed fills, wrong P&L | Heartbeats only |
| At-least-once | `acks=all, retries=3` | Duplicate fills if consumer crashes after process but before commit | Most trading events |
| Exactly-once | `transactional_id + acks=all` | None — highest guarantees | Position updates, P&L |

### Achieving exactly-once

```python
# Producer side
producer = KafkaProducer(
    transactional_id='risk-engine-producer-1',
    # enable_idempotence=True is automatically set
)
producer.init_transactions()
producer.begin_transaction()
# ... produce to output topic, send offsets ...
producer.send_offsets_to_transaction(offsets, consumer_group_metadata)
producer.commit_transaction()

# Consumer side — skip uncommitted records
consumer = KafkaConsumer(
    ...,
    isolation_level='read_committed',   # skip transactional messages not yet committed
)
```

### In practice
Idempotent producer (`enable_idempotence=True`) + manual consumer offset commit covers 90% of trading use cases. Full transactions only needed for consume-transform-produce pipelines where you must atomically advance the consumer offset and produce the result.

---

---

## K-07 — Market Data Feed Handler → Kafka Pipeline

The feed handler is the bridge between raw UDP multicast ticks from the exchange and the internal Kafka topic that downstream consumers read from.

```
UDP Multicast (exchange)
     ↓  raw pipe-delimited ticks
Feed Handler (validates, normalizes, partitions by symbol)
     ↓
Kafka topic: market-data.ticks (24 partitions, 1hr retention, lz4 compression)
     ↓  consumers subscribe in parallel
┌─────────────────────────────────────────┐
│ risk-engine-group  (real-time MTM)      │
│ algo-trading-group (signal generation)  │
│ tick-storage-group (HDF5 / TimescaleDB) │
│ vwap-calc-group    (streaming VWAP)     │
└─────────────────────────────────────────┘
```

### Market data topic configuration

```bash
# market-data.ticks topic settings (production)
partitions=24              # 24 symbols → 1 partition per major symbol
replication-factor=3       # standard HA
retention.ms=3600000       # 1 hour — ticks flow to HDF5, no long-term Kafka retention
compression.type=lz4       # fast compression for high-throughput tick data
```

### Validation before publishing

```python
# Feed handler must validate before publishing to main topic
# Invalid ticks → dead letter queue (market-data.ticks.dlq)

def validate(tick):
    if not all(k in tick for k in ['symbol', 'bid', 'ask', 'exchange']):
        return "MISSING_FIELDS"
    if tick['symbol'] not in VALID_SYMBOLS:
        return f"UNKNOWN_SYMBOL:{tick['symbol']}"
    if tick['bid'] < 0 or tick['ask'] < 0:
        return "NEGATIVE_PRICE"
    if tick['bid'] >= tick['ask']:
        return "CROSSED_BOOK"
    return None   # valid
```

### Symbol partitioning

```python
# Partition by symbol — all ticks for a symbol land on the same partition
# This guarantees ordering of bid/ask updates per symbol
partition = abs(hash(symbol)) % num_partitions
```

---

## K-08 — Market Data Consumer Lag at Market Open

At 09:30 market open, tick rate spikes 10x. If tick-storage-group consumers can't keep up, downstream HDF5 writes fall behind — researchers get stale data.

### Reading the lag output

```
GROUP               TOPIC              PARTITION  LAG    CONSUMER-ID
tick-storage-group  market-data.ticks  0          50     tick-1-a1b2    ← OK
tick-storage-group  market-data.ticks  1          10500  tick-1-a1b2    ← HIGH LAG
tick-storage-group  market-data.ticks  5          10500  -              ← NO CONSUMER
```

A `CONSUMER-ID` of `-` means no consumer is assigned to that partition — the pod died or the group is under-provisioned.

### Staleness calculation

```python
# If tick rate = 200 msg/sec and lag = 10,500 messages:
stale_seconds = lag / tick_rate = 10500 / 200 = 52.5 seconds
# Downstream systems (risk engine, VWAP) are 52 seconds behind real-time
```

### Resolution

```bash
# Scale consumer group — add instances to cover unassigned partitions
# Rule: consumer instances ≤ partition count (extras sit idle)
# For 24 partitions: run 24 consumer instances for full coverage

# If consumers are too slow (lag stable but high):
# max.poll.records=100   # reduce batch size → faster commit cycle
# Optimize the HDF5 write path (batch writes, async flush)
```

---

## K-09 — Kafka → Tick Storage Pipeline

### Commit-after-write pattern (critical)

```python
for msg in consumer:
    tick = msg.value

    # 1. Validate the tick
    if not is_valid(tick): route_to_dlq(tick); continue

    # 2. Transform (normalize timestamps, add fields)
    normalized = normalize(tick)

    # 3. Write to HDF5 / database FIRST
    writer.writerow(normalized)

    # 4. THEN commit the Kafka offset
    consumer.commit()   # ← always AFTER the write, never before

# Why commit-after-write:
# If we commit first and then crash, the tick is lost (offset advanced, not written)
# If we write first and then crash, we re-read and write again — idempotent duplicate
# Duplicates are recoverable; lost data is not
```

---

## K-10 — Streaming VWAP Pipeline

VWAP (Volume-Weighted Average Price) must be computed in real time as ticks arrive.

```python
# Stateful VWAP accumulator per symbol
total_notional = defaultdict(float)   # symbol → Σ(price × volume)
total_volume   = defaultdict(float)   # symbol → Σ(volume)

for msg in consumer:
    tick = msg.value
    sym, px, qty = tick['symbol'], tick['price'], tick['volume']

    total_notional[sym] += px * qty
    total_volume[sym]   += qty

    vwap = round(total_notional[sym] / total_volume[sym], 4)
    publish_vwap(sym, vwap)
    consumer.commit()

# VWAP resets at market open each day (clear accumulators at 09:30)
# State must survive consumer restarts → persist to Redis or TimescaleDB
```

---

## K-11 — Dead Letter Queue (DLQ) Pattern

```
market-data.ticks (main topic, 1hr retention)
     ↓
Feed handler validates each message
     ↓
Valid   → publish normally → downstream consumers
Invalid → market-data.ticks.dlq (30-day retention, 3 partitions)
```

### DLQ topic configuration

```bash
kafka-topics.sh --bootstrap-server $BROKER \
  --create --topic market-data.ticks.dlq \
  --partitions 3 \
  --replication-factor 3 \
  --config retention.ms=2592000000   # 30 days — long retention for investigation
```

### Monitor DLQ volume

```bash
# DLQ lag growing = systematic data quality issue upstream
kafka-consumer-groups.sh --bootstrap-server $BROKER \
  --group dlq-monitor-group \
  --describe | grep market-data.ticks.dlq

# Alert if DLQ rate > 1% of main topic volume
```

### Replay from DLQ after fix

```bash
# After fixing the root cause, replay bad messages through the corrected handler
kafka-console-consumer.sh --bootstrap-server $BROKER \
  --topic market-data.ticks.dlq \
  --from-beginning \
  | python3 fixed_handler.py \
  | kafka-console-producer.sh --bootstrap-server $BROKER \
    --topic market-data.ticks
```

---

## K-12 — End-to-End Pipeline Triage

This is the full L1→L2→dev escalation workflow for a data pipeline support ticket.

### Triage sequence

```
1. Read the ticket carefully
   → What is the exact symptom? Which symbols? What time window?

2. Check HDF5 metadata for the affected file
   → h5ls -r /data/ticks/<date>.h5
   → Look for: last_ts cutoff earlier than expected
   → If multiple symbols cut off at the SAME timestamp = pipeline crash (not data quality)

3. Check Kafka consumer lag at the time of the incident
   → kafka-consumer-groups.sh ... --describe
   → CONSUMER-ID = "-" on all partitions = pod died at that moment

4. Check Kubernetes pod logs for the tick storage job
   → kubectl logs <pod> -n data-pipelines --previous
   → Look for: "Killed" and exit code 137 (OOMKilled)
   → kubectl describe pod → confirms Reason: OOMKilled + memory limit

5. Check Kafka retention — is the data still available for replay?
   → market-data.ticks retention = 1 hour
   → If incident was recent, data is still in Kafka → replay is possible
   → Flag as URGENT if replay window is closing

6. Write the L2 escalation (before calling dev)
```

### L2 escalation checklist

```markdown
## Required fields in L2 escalation note

- [ ] Affected symbols and exact time window (from HDF5 metadata)
- [ ] Root cause (OOMKilled / pod crash / Kafka lag — with evidence)
- [ ] Evidence: log snippet, kubectl describe output, HDF5 metadata
- [ ] What was already ruled out (user error, exchange issue, network)
- [ ] Kafka replay window — is data still available? How long until expiry?
- [ ] Recommended action for dev team
- [ ] Impact: who is affected (research, risk, downstream jobs)
```

### Key diagnostic commands

```bash
# HDF5 file metadata — check last_ts per symbol
h5ls -r /data/ticks/<date>.h5

# Kafka consumer lag snapshot
kafka-consumer-groups.sh \
  --bootstrap-server $BROKER \
  --group tick-storage-group \
  --describe

# Pod logs from crashed container
kubectl logs <pod-name> -n data-pipelines --previous

# OOMKilled confirmation
kubectl describe pod <pod-name> -n data-pipelines \
  | grep -A5 "Last State\|Limits\|Reason"
```

---

## Key Concepts Quick Reference

| Concept | Definition |
|---------|-----------|
| **Offset** | Record's position within a partition. Consumers track their position by committing offsets. |
| **Consumer lag** | `LOG-END-OFFSET − CURRENT-OFFSET` per partition. High lag = consumer falling behind. |
| **ISR** | In-Sync Replicas — replicas fully caught up to the leader. `acks=all` waits for all ISR. |
| **RF** | Replication factor — number of copies. RF=3 tolerates 2 broker failures. |
| **min.insync.replicas** | Minimum ISR required for a produce to succeed. `RF=3, min.isr=2` is the trading standard. |
| **Consumer group** | Consumers sharing a group ID. Each partition assigned to exactly ONE consumer in the group. |

---

## Escalation Criteria

| Condition | Action |
|-----------|--------|
| All brokers in an ISR down | Declare incident — potential data loss window |
| Lag growing unbounded on all partitions | Consumer failure — scale or restart consumer fleet |
| Producer NotEnoughReplicasException despite brokers healthy | `min.insync.replicas` misconfiguration |
| Corrupt message causing consumer crash loop | Skip and quarantine: `auto.offset.reset=latest` or manually advance offset |

---

## Troubleshooting Scripts

All scripts live in `scripts/kafka/` from the repo root.

### lag_monitor.py — consumer group lag checker

Reads a lag snapshot and flags HIGH LAG and NO CONSUMER partitions.

```bash
# Against the lab snapshot file
python3 scripts/kafka/lag_monitor.py \
  --file /tmp/lab_kafka/data/consumer_lag_snapshot.json

# Custom lag threshold (default: 100)
python3 scripts/kafka/lag_monitor.py \
  --file /tmp/lab_kafka/data/consumer_lag_snapshot.json \
  --threshold 500

# Live mode — requires kafka-python and a running broker
python3 scripts/kafka/lag_monitor.py \
  --broker localhost:9092 \
  --group risk-engine-group \
  --topic trade-executions \
  --threshold 100
```

**What it reports:**
- Per-partition lag with status: OK / ELEVATED / HIGH LAG / NO CONSUMER
- Summary of partitions needing attention
- Actionable messages: "add worker instances" or "consumer too slow"

**Install kafka-python for live mode:**
```bash
pip3 install kafka-python --break-system-packages
```
