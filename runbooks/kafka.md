# Runbook: Kafka

## Overview

This runbook covers Kafka operational issues in a trading environment: consumer group lag, broker failures, partition management, and producer configuration. All scenarios map to `lab_kafka.py` (K-01 through K-06).

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
