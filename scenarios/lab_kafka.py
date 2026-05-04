#!/usr/bin/env python3
"""
Support Engineer Challenge Lab — Kafka Setup
============================================
Creates the environment for all Kafka challenge scenarios (K1-K6).
Since a real Kafka cluster requires significant infrastructure, this lab
provides realistic simulation: mock brokers, consumer-group snapshots,
working kafka-python producer/consumer scripts, and config files to inspect.

Run with: python3 lab_kafka.py [--scenario N] [--teardown]

SCENARIOS:
  1   K-01  Kafka concepts quiz — config files + reference sheet
  2   K-02  Consumer group lag — snapshot file + analysis scripts
  3   K-03  Topic operations — CLI command reference + mock topic config
  4   K-04  Broker down incident — simulated broker failure logs + response guide
  5   K-05  Python producer + consumer — working kafka-python scripts
  6   K-06  At-least-once vs exactly-once — config examples + explainer
  7   K-07  Market data feed handler → Kafka pipeline
  8   K-08  Market data consumer lag at market open
  9   K-09  Data pipeline — Kafka → tick storage
  10  K-10  Streaming VWAP pipeline (consume-transform-produce)
  11  K-11  Dead letter queue pattern
  12  K-12  End-to-end pipeline triage — researcher ticket → root cause → escalation
  99        ALL scenarios
"""

import os, sys, time, signal, shutil, json, argparse, subprocess, multiprocessing
from pathlib import Path
from datetime import datetime, timedelta
import random

LAB_ROOT = Path("/tmp/lab_kafka")
DIRS = {
    "config":   LAB_ROOT / "config",
    "logs":     LAB_ROOT / "logs",
    "scripts":  LAB_ROOT / "scripts",
    "data":     LAB_ROOT / "data",
    "pids":     LAB_ROOT / "run",
}

from common import (
    GREEN, YELLOW, RED, CYAN, BOLD, RESET, SEP,
    ok, warn, err, info, header, lab_footer,
    create_dirs as _create_dirs,
    save_pid as _save_pid, load_pids as _load_pids,
    spawn as _spawn, kill_pids, kill_strays, remove_lab_dir,
    show_status as _show_status,
)

def create_dirs():  _create_dirs(DIRS)
def save_pid(n, p): _save_pid(DIRS["pids"], n, p)
def load_pids():    return _load_pids(DIRS["pids"])
def spawn(t, a, n): return _spawn(t, a, DIRS["pids"], n)


# ══════════════════════════════════════════════
#  SCENARIO 1 — Concepts & Config Files
# ══════════════════════════════════════════════

def launch_scenario_1():
    header("Scenario K-01 — Kafka Architecture & Key Concepts")
    print("  Core Kafka concepts every support engineer must know.\n")

    ref = DIRS["config"] / "kafka_reference.md"
    ref.write_text("""\
# Kafka Architecture Reference
=================================

## Core Concepts

TOPIC
  Logical stream of records. Think: a named queue that persists.
  Topics are split into PARTITIONS for parallelism.

PARTITION
  Ordered, immutable sequence of records. Each record has an OFFSET.
  A topic with 12 partitions allows 12 consumers to read in parallel.
  Records with the same KEY always land on the same partition → ordering per key.

OFFSET
  A record's position within a partition. Consumers commit their offset
  to track how far they've read.

BROKER
  A single Kafka server. A cluster has 3+ brokers for HA.
  Each partition has 1 LEADER and N-1 FOLLOWERS (replicas).

REPLICATION FACTOR
  How many copies of each partition exist across brokers.
  RF=3 tolerates 2 broker failures without data loss.

ISR (In-Sync Replicas)
  Replicas that are fully caught up to the leader.
  Producer acks=all means: wait for ALL ISR to confirm write.

CONSUMER GROUP
  A set of consumer instances that together consume a topic.
  Each partition is assigned to exactly ONE consumer in the group.
  Rule: if consumers > partitions, some consumers sit idle.

## Producer Key Settings
  acks=0             Fire and forget. Fast but may lose data.
  acks=1             Leader acknowledges. Lost if leader dies before replica sync.
  acks=all (-1)      All ISR acknowledge. Safest. Required for trading.
  enable.idempotence=true   Deduplicates retried messages (exactly-once per partition).
  retries=3          Retry on transient failure.
  max.in.flight.requests.per.connection=1   Preserves ordering with retries.

## Consumer Key Settings
  auto.offset.reset=earliest   Start from oldest record if no committed offset.
  auto.offset.reset=latest     Start from newest (may miss records during downtime).
  enable.auto.commit=false     Commit offsets manually after processing (safer).
  max.poll.records=500         Batch size per poll().
  max.poll.interval.ms=300000  If consumer doesn't poll within 5min → kicked from group.
  session.timeout.ms=10000     Heartbeat timeout. Broker removes consumer after 10s silence.

## Trading-Specific Patterns
  Symbol-keyed messages:  key=symbol → AAPL always → same partition → ordered fills per symbol
  RF=3, min.insync.replicas=2: tolerates 1 broker down AND guarantees 2 replicas confirm write
  retention.ms=604800000: 7-day replay window for trade reconciliation
""")
    ok(f"Kafka reference sheet: {ref}")

    broker_cfg = DIRS["config"] / "server.properties"
    broker_cfg.write_text("""\
# Kafka Broker Configuration (server.properties)
# ================================================

# Broker identity
broker.id=1
listeners=PLAINTEXT://0.0.0.0:9092
advertised.listeners=PLAINTEXT://kafka-broker-1.trading.internal:9092

# Storage
log.dirs=/var/kafka/logs
num.partitions=12
default.replication.factor=3
min.insync.replicas=2

# Retention
log.retention.hours=168
log.retention.bytes=107374182400
log.segment.bytes=1073741824

# Performance
num.network.threads=8
num.io.threads=16
socket.send.buffer.bytes=102400
socket.receive.buffer.bytes=102400
socket.request.max.bytes=104857600

# ZooKeeper / KRaft
# Old: zookeeper.connect=zk-1:2181,zk-2:2181,zk-3:2181
# New KRaft mode: process.roles=broker,controller
""")
    ok(f"Broker config: {broker_cfg}")

    print(f"""
{BOLD}── Reference files ─────────────────────────────────────{RESET}
{CYAN}       cat {ref}
       cat {broker_cfg}{RESET}

{BOLD}── Key concepts to know cold ────────────────────────────{RESET}
  Q: Why use symbol as message key?
  A: Same key → same partition → ordered fills per symbol.
     Without key → round-robin → AAPL fills interleaved, unordered.

  Q: RF=3 with min.insync.replicas=2 — what does it mean?
  A: 3 copies of data. Producer acks=all waits for 2 ISR to confirm.
     Tolerates 1 broker failure with zero data loss.

  Q: Consumers > partitions — what happens?
  A: Some consumers sit idle (each partition → exactly 1 consumer).
     Fix: increase partition count (cannot decrease without recreating topic).

  Q: What is consumer lag?
  A: LOG-END-OFFSET - CURRENT-OFFSET per partition.
     High lag = consumer not keeping up with producer throughput.
""")


# ══════════════════════════════════════════════
#  SCENARIO 2 — Consumer Group Lag
# ══════════════════════════════════════════════

def launch_scenario_2():
    header("Scenario K-02 — Consumer Group Lag Investigation")
    print("  risk-engine-group is falling behind. Investigate lag.\n")

    # Build a realistic consumer-group snapshot
    lag_data = DIRS["data"] / "consumer_group_describe.txt"
    lag_data.write_text("""\
GROUP              TOPIC             PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG   CONSUMER-ID                       HOST       CLIENT-ID
risk-engine-group  trade-executions  0          10420           10425           5     risk-worker-1-abc123-0            /10.0.1.10  risk-worker-1
risk-engine-group  trade-executions  1          9800            10200           400   risk-worker-2-def456-1            /10.0.1.11  risk-worker-2
risk-engine-group  trade-executions  2          11000           11002           2     risk-worker-1-abc123-0            /10.0.1.10  risk-worker-1
risk-engine-group  trade-executions  3          8500            10300           1800  -                                 -          -
risk-engine-group  trade-executions  4          9100            9105            5     risk-worker-3-ghi789-2            /10.0.1.12  risk-worker-3
risk-engine-group  trade-executions  5          8800            10200           1400  -                                 -          -
""")
    ok(f"Consumer group snapshot: {lag_data}")

    # Analysis script
    analyze = DIRS["scripts"] / "analyze_lag.py"
    analyze.write_text(f"""\
#!/usr/bin/env python3
\"\"\"Analyze Kafka consumer group lag snapshot.\"\"\"

LAG_FILE  = "{lag_data}"
THRESHOLD = 100

print(f"Analyzing: {{LAG_FILE}}\\n")

total_lag = 0
issues = []

with open(LAG_FILE) as f:
    for i, line in enumerate(f):
        if i == 0: continue  # header
        parts = line.split()
        if len(parts) < 6: continue
        topic, partition, cur, end, lag_str = parts[1], parts[2], parts[3], parts[4], parts[5]
        consumer = parts[6] if len(parts) > 6 else "-"
        try:
            lag = int(lag_str)
        except ValueError:
            continue
        total_lag += lag
        flag = ""
        if consumer == "-":
            flag = "🔴 NO CONSUMER"
            issues.append((partition, lag, "unassigned partition — add consumer instance"))
        elif lag > THRESHOLD:
            flag = "⚠  HIGH LAG"
            issues.append((partition, lag, f"consumer {{consumer}} falling behind"))
        elif lag > 50:
            flag = "⚠  ELEVATED"
        else:
            flag = "✅ OK"
        print(f"  Partition {{partition:>2}}  lag={{lag:>6}}  {{consumer[:30]:<30}}  {{flag}}")

print(f"\\nTotal lag across all partitions: {{total_lag}}")
if issues:
    print(f"\\n⚠  {{len(issues)}} issues found:")
    for p, lag, reason in issues:
        print(f"   partition {{p}}: {{reason}} (lag={{lag}})")
    print("\\nRoot cause analysis:")
    print("  Partitions 3,5 have NO CONSUMER (-): group has 3 workers but 6 partitions.")
    print("  Fix: add 3 more consumer instances to risk-engine-group.")
    print("  Partition 1 lagging 400: consumer-2 is slow — check CPU/processing time.")
""")
    ok(f"Analysis script: {analyze}")

    print(f"""
{BOLD}── Consumer group output (simulated): ──────────────────{RESET}
{CYAN}       cat {lag_data}{RESET}

{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Read the snapshot and identify issues
{CYAN}       cat {lag_data}{RESET}

  2. Analyse with the Python script
{CYAN}       python3 {analyze}{RESET}

  3. On a real cluster you'd run:
{CYAN}       kafka-consumer-groups.sh \\
         --bootstrap-server localhost:9092 \\
         --group risk-engine-group \\
         --describe{RESET}

{BOLD}── Root causes in this snapshot ────────────────────────{RESET}
  Partitions 3 & 5: CONSUMER = "-" → unassigned. Only 3 workers for 6 partitions.
  Partition 1: lag=400, growing → consumer-2 too slow.
  Fix: add 3 more consumer instances to match partition count.
""")


# ══════════════════════════════════════════════
#  SCENARIO 3 — Topic Operations
# ══════════════════════════════════════════════

def launch_scenario_3():
    header("Scenario K-03 — Kafka Topic Operations")
    print("  Create and inspect a trading topic correctly.\n")

    topic_cfg = DIRS["config"] / "trade_executions_topic.properties"
    topic_cfg.write_text("""\
# Topic configuration for trade-executions
# ==========================================
# Applied at creation:
#   kafka-topics.sh --create --topic trade-executions
#     --partitions 12 --replication-factor 3
#     --config retention.ms=604800000
#     --config min.insync.replicas=2

# Why these values:
# partitions=12: 12 consumers can process in parallel.
#   Partition by symbol prefix → AAPL always → partition 0 (ordering per symbol).
# RF=3: tolerates 2 broker failures. Never lose a trade event.
# min.insync.replicas=2: with acks=all, producer only succeeds if 2+ replicas confirm.
#   Prevents silent data loss when 1 broker is temporarily down.
# retention.ms=604800000 (7 days): downstream systems can replay up to 7 days of trades.

# Topic describe output (what you'd see after creation):
#
# Topic: trade-executions  PartitionCount: 12  ReplicationFactor: 3  Configs: ...
#   Topic: trade-executions  Partition: 0  Leader: 1  Replicas: 1,2,3  Isr: 1,2,3
#   Topic: trade-executions  Partition: 1  Leader: 2  Replicas: 2,3,1  Isr: 2,3,1
#   ... (12 partitions)
""")
    ok(f"Topic config reference: {topic_cfg}")

    cli_ref = DIRS["scripts"] / "kafka_cli_commands.sh"
    cli_ref.write_text("""\
#!/bin/bash
# Kafka CLI commands reference for the challenge lab
# Run these against a real Kafka cluster at localhost:9092

BROKER="localhost:9092"
TOPIC="trade-executions"

# ── Create topic ──────────────────────────────────────────
kafka-topics.sh \\
  --bootstrap-server $BROKER \\
  --create \\
  --topic $TOPIC \\
  --partitions 12 \\
  --replication-factor 3 \\
  --config retention.ms=604800000 \\
  --config min.insync.replicas=2

# ── Describe topic ────────────────────────────────────────
kafka-topics.sh --bootstrap-server $BROKER --describe --topic $TOPIC

# ── List all topics ───────────────────────────────────────
kafka-topics.sh --bootstrap-server $BROKER --list

# ── Produce a test message ────────────────────────────────
echo '{"symbol":"AAPL","qty":100,"side":"BUY","price":185.50}' | \\
  kafka-console-producer.sh \\
    --bootstrap-server $BROKER \\
    --topic $TOPIC \\
    --property "key.separator=:" \\
    --property "parse.key=true"

# ── Consume from beginning ────────────────────────────────
kafka-console-consumer.sh \\
  --bootstrap-server $BROKER \\
  --topic $TOPIC \\
  --from-beginning \\
  --group test-group \\
  --max-messages 10

# ── Check under-replicated partitions ─────────────────────
kafka-topics.sh --bootstrap-server $BROKER --describe --under-replicated-partitions

# ── Delete a topic (CAREFUL in prod!) ─────────────────────
# kafka-topics.sh --bootstrap-server $BROKER --delete --topic $TOPIC
""")
    ok(f"CLI commands reference: {cli_ref}")

    print(f"""
{BOLD}── Config and CLI reference files ──────────────────────{RESET}
{CYAN}       cat {topic_cfg}
       cat {cli_ref}{RESET}

{BOLD}── Study the CLI commands (no Kafka needed to read them) {RESET}
{CYAN}       bash {cli_ref}   # will fail without Kafka — read instead
       cat {cli_ref}{RESET}

{BOLD}── If you have Kafka running locally ───────────────────{RESET}
{CYAN}       # Start ZooKeeper + Kafka (binary install):
       bin/zookeeper-server-start.sh config/zookeeper.properties &
       bin/kafka-server-start.sh config/server.properties &
       # Then run the commands in {cli_ref}{RESET}

{BOLD}── Key interview answers ────────────────────────────────{RESET}
  Q: Why 12 partitions?   → 12 consumers can read in parallel
  Q: Why RF=3?            → Tolerates 2 broker failures, no data loss
  Q: Why acks=all?        → All ISR must confirm → no silent write loss
  Q: Why symbol as key?   → Same symbol → same partition → ordered fills
""")


# ══════════════════════════════════════════════
#  SCENARIO 4 — Broker Down Incident
# ══════════════════════════════════════════════

def launch_scenario_4():
    header("Scenario K-04 — Kafka Broker Down Incident Response")
    print("  broker-2 has gone down. Producers throwing exceptions.\n")

    # Simulate broker logs
    broker2_log = DIRS["logs"] / "kafka-broker-2.log"
    broker2_log.write_text("""\
[2024-01-15 09:28:00,001] INFO  Kafka version: 3.5.0 (kafka.Kafka$)
[2024-01-15 09:28:00,201] INFO  Kafka commitId: abc1234def5678 (kafka.Kafka$)
[2024-01-15 09:28:01,102] INFO  [KafkaServer id=2] started (kafka.server.KafkaServer)
[2024-01-15 09:29:00,010] INFO  [GroupCoordinator 2]: Loading offsets...
[2024-01-15 09:30:00,000] INFO  [ReplicaManager broker=2] Market open — high throughput expected
[2024-01-15 09:30:47,312] WARN  [Log partition=trade-executions-3] Slow log flush: 180ms
[2024-01-15 09:30:48,100] WARN  [Log partition=trade-executions-5] Slow log flush: 240ms
[2024-01-15 09:30:55,444] ERROR [ReplicaFetcherThread-0-1] Error fetching from broker 1: Connection reset
[2024-01-15 09:30:55,450] WARN  [ReplicaManager] broker-2 disk I/O at 95% utilisation
[2024-01-15 09:30:56,001] FATAL [KafkaServer id=2] Fatal error during KafkaServer startup. Prepare to shutdown
[2024-01-15 09:30:56,002] ERROR java.io.IOException: No space left on device
[2024-01-15 09:30:56,003]   at sun.nio.ch.FileDispatcherImpl.write0(Native Method)
[2024-01-15 09:30:56,010] INFO  [KafkaServer id=2] shutting down (kafka.server.KafkaServer)
""")
    ok(f"Broker-2 crash log: {broker2_log}")

    urp_output = DIRS["data"] / "under_replicated_partitions.txt"
    urp_output.write_text("""\
# Output of: kafka-topics.sh --describe --under-replicated-partitions
# Captured 30 seconds after broker-2 crashed
#
# Topic: trade-executions  Partition: 1  Leader: 1  Replicas: 1,2,3  Isr: 1,3
# Topic: trade-executions  Partition: 3  Leader: 3  Replicas: 3,2,1  Isr: 3,1
# Topic: trade-executions  Partition: 5  Leader: 1  Replicas: 1,2,3  Isr: 1,3
# Topic: trade-executions  Partition: 7  Leader: 3  Replicas: 3,2,1  Isr: 3,1
# Topic: market-data        Partition: 0  Leader: 1  Replicas: 1,2,3  Isr: 1,3
# Topic: market-data        Partition: 2  Leader: 3  Replicas: 3,2,1  Isr: 3,1
#
# ISR is now 2 replicas (broker-2 absent). min.insync.replicas=2 still met.
# Producers can still write (acks=all will wait for 2 ISR).
# Consumers can still read from brokers 1 and 3.
# Broker-2 partitions have been reassigned to brokers 1 and 3 as leaders.
""")
    ok(f"URP snapshot: {urp_output}")

    response_guide = DIRS["scripts"] / "broker_down_response.sh"
    response_guide.write_text(f"""\
#!/bin/bash
# K-04 Incident Response: Broker Down
# =====================================

BROKER="localhost:9092"   # use a surviving broker

echo "=== Step 1: Confirm broker is down ==="
systemctl status kafka      # on the broker host
ss -tlnp | grep 9092        # check port
journalctl -u kafka -n 100  # last 100 log lines

echo "=== Step 2: Check under-replicated partitions ==="
# Non-empty = broker-2 replicas are offline
kafka-topics.sh \\
  --bootstrap-server $BROKER \\
  --describe \\
  --under-replicated-partitions
# Reference output:
cat {urp_output}

echo "=== Step 3: Verify producers still working (RF=3, min.isr=2) ==="
# With 2 remaining brokers and min.insync.replicas=2, producers with acks=all
# will still succeed (2 ISR available). Check producer error rate in monitoring.

echo "=== Step 4: Root cause from broker-2 log ==="
cat {broker2_log}
# Root cause: 'No space left on device' — disk full on broker-2
# Fix before restart: clear old log segments or expand disk

echo "=== Step 5: Clear disk then restart ==="
# df -h /var/kafka                              # check disk
# find /var/kafka/logs -name '*.log' -mtime +7 -delete  # remove old segments
# systemctl restart kafka                        # restart broker
# journalctl -u kafka -f                        # watch startup

echo "=== Step 6: Verify URPs clear after restart ==="
# watch -n 5 "kafka-topics.sh --bootstrap-server $BROKER --describe --under-replicated-partitions"
# Should return empty within a few minutes (broker catching up)

echo "=== Step 7: Trigger preferred replica election (rebalance) ==="
# kafka-preferred-replica-election.sh --bootstrap-server $BROKER
""")
    ok(f"Response guide: {response_guide}")

    print(f"""
{BOLD}── Incident artifacts ──────────────────────────────────{RESET}
{CYAN}       cat {broker2_log}          # broker-2 crash log
       cat {urp_output}    # under-replicated partitions
       cat {response_guide}  # full response playbook{RESET}

{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Read the broker log — what caused the crash?
{CYAN}       grep "ERROR\\|FATAL" {broker2_log}{RESET}

  2. Understand the URP output
{CYAN}       cat {urp_output}{RESET}

  3. Walk through the response guide
{CYAN}       cat {response_guide}{RESET}

{BOLD}── Key insight from this scenario ──────────────────────{RESET}
  Root cause: disk full ('No space left on device')
  RF=3, min.isr=2 → cluster survived: 2 brokers still serving reads+writes
  Fix: clear old log segments on broker-2 FIRST, then restart
  Blind restart on a full disk = immediate re-crash
""")


# ══════════════════════════════════════════════
#  SCENARIO 5 — Python Producer + Consumer
# ══════════════════════════════════════════════

def launch_scenario_5():
    header("Scenario K-05 — Python Kafka Producer & Consumer")
    print("  Write working producer/consumer scripts using kafka-python.\n")

    producer_script = DIRS["scripts"] / "kafka_producer.py"
    producer_script.write_text("""\
#!/usr/bin/env python3
\"\"\"
K-05: Kafka producer — sends trade events to trade-executions topic.
Install: pip3 install kafka-python --break-system-packages

NOTE: Requires a running Kafka broker at localhost:9092.
To test without Kafka, set DRY_RUN=True below.
\"\"\"

DRY_RUN = True   # set False when you have a real Kafka broker

TOPIC  = "trade-executions"
BROKER = "localhost:9092"

trades = [
    {"symbol": "AAPL",  "qty": 100, "side": "BUY",  "price": 185.50},
    {"symbol": "GOOGL", "qty":  50, "side": "SELL", "price": 141.20},
    {"symbol": "AAPL",  "qty": 200, "side": "SELL", "price": 185.55},
    {"symbol": "MSFT",  "qty":  75, "side": "BUY",  "price": 380.10},
    {"symbol": "TSLA",  "qty":  30, "side": "BUY",  "price": 248.00},
]

if DRY_RUN:
    import json
    print(f"[DRY RUN] Would produce to topic={TOPIC} broker={BROKER}")
    for i, trade in enumerate(trades):
        # Simulate partition assignment: hash(symbol) % 12
        partition = hash(trade["symbol"]) % 12
        print(f"  [{i}] key={trade['symbol']:<5} → partition {partition}  "
              f"value={json.dumps(trade)}")
    print("\\nKey insight: using symbol as key guarantees all AAPL trades")
    print("land on the same partition → strict ordering per symbol.")
else:
    from kafka import KafkaProducer
    import json
    try:
        producer = KafkaProducer(
            bootstrap_servers=[BROKER],
            value_serializer=lambda v: json.dumps(v).encode(),
            key_serializer=lambda k: k.encode(),
            acks="all",          # wait for all ISR replicas
            enable_idempotence=True,
            retries=3,
        )
        for trade in trades:
            future = producer.send(
                TOPIC,
                key=trade["symbol"],   # same symbol → same partition → ordered
                value=trade,
            )
            meta = future.get(timeout=5)
            print(f"Sent {trade['symbol']} → partition {meta.partition} offset {meta.offset}")
        producer.flush()
        print("\\nAll messages sent successfully.")
    except Exception as e:
        print(f"Producer error: {e}")
        print("Is Kafka running? pip3 install kafka-python?")
""")
    ok(f"Producer script: {producer_script}")

    consumer_script = DIRS["scripts"] / "kafka_consumer.py"
    consumer_script.write_text("""\
#!/usr/bin/env python3
\"\"\"
K-05: Kafka consumer — reads from trade-executions, prints summary.
Install: pip3 install kafka-python --break-system-packages

NOTE: Requires a running Kafka broker at localhost:9092.
\"\"\"

TOPIC  = "trade-executions"
BROKER = "localhost:9092"
GROUP  = "risk-engine"

try:
    from kafka import KafkaConsumer
    import json

    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=[BROKER],
        group_id=GROUP,
        value_deserializer=lambda m: json.loads(m.decode()),
        key_deserializer=lambda k: k.decode() if k else None,
        auto_offset_reset="earliest",       # start from beginning
        enable_auto_commit=False,           # manual commit — safer for trading
        consumer_timeout_ms=5000,           # stop after 5s of no messages
    )

    print(f"Consuming from {TOPIC} as group {GROUP}...\\n")
    count = 0
    for msg in consumer:
        t = msg.value
        print(f"  [p{msg.partition:02d}@{msg.offset:05d}] key={msg.key:5}  "
              f"{t['side']:4} {t['qty']:4} {t['symbol']:5} @ ${t['price']}")
        consumer.commit()   # manual commit after processing
        count += 1

    print(f"\\nConsumed {count} messages.")
    consumer.close()

except ImportError:
    print("kafka-python not installed.")
    print("Run: pip3 install kafka-python --break-system-packages")
except Exception as e:
    print(f"Consumer error: {e}")
""")
    ok(f"Consumer script: {consumer_script}")

    print(f"""
{BOLD}── Scripts ─────────────────────────────────────────────{RESET}
{CYAN}       python3 {producer_script}
       python3 {consumer_script}{RESET}

{BOLD}── Producer runs in DRY_RUN mode (no Kafka needed) ─────{RESET}
  Shows partition routing by symbol key.
  Set DRY_RUN=False to send to a real Kafka broker.

{BOLD}── To set up a local Kafka for real testing ────────────{RESET}
{CYAN}       # Option 1: Docker (easiest)
       docker run -d -p 9092:9092 \\
         -e KAFKA_CFG_NODE_ID=1 \\
         -e KAFKA_CFG_PROCESS_ROLES=broker,controller \\
         -e KAFKA_CFG_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093 \\
         bitnami/kafka:latest

       pip3 install kafka-python --break-system-packages
       python3 {producer_script}   # set DRY_RUN=False
       python3 {consumer_script}{RESET}

{BOLD}── Key interview point ──────────────────────────────────{RESET}
  Using symbol as key → same symbol → same partition → ordered.
  Without key → round-robin → AAPL fills interleaved → can't guarantee order.
  enable_auto_commit=False → commit AFTER processing → no data loss on crash.
""")


# ══════════════════════════════════════════════
#  SCENARIO 6 — Delivery Semantics
# ══════════════════════════════════════════════

def launch_scenario_6():
    header("Scenario K-06 — Delivery Semantics in Trading")
    print("  At-most-once vs at-least-once vs exactly-once.\n")

    semantics_guide = DIRS["config"] / "delivery_semantics.md"
    semantics_guide.write_text("""\
# Kafka Delivery Semantics for Trading
=======================================

## The Three Semantics

AT-MOST-ONCE
  Message sent once. If the broker doesn't ACK, it's dropped — NOT retried.
  Producer config: acks=0, retries=0
  Risk in trading: SILENT DATA LOSS. A trade event vanishes. P&L is wrong.
  Use case: non-critical metrics, heartbeats.

AT-LEAST-ONCE
  Message retried until the broker ACKs it. May be delivered 2+ times.
  Producer config: acks=all, retries=3
  Risk in trading: DUPLICATE FILLS. A 100-share BUY processed twice = 200 shares.
  If your consumer crashes after processing but before committing the offset,
  it will reprocess the message on restart.

EXACTLY-ONCE
  Message delivered and processed precisely once, even with failures.
  Requires both idempotent producer AND transactional consume-transform-produce.
  Use case: trade execution, P&L calculation, position updates.

## How Kafka Achieves Exactly-Once

STEP 1 — IDEMPOTENT PRODUCER (per-partition exactly-once)
  enable.idempotence=true
  max.in.flight.requests.per.connection=1
  
  Each message gets a (ProducerID, SequenceNumber). The broker deduplicates
  any retry with the same sequence number. Safe to retry — no duplicates.

STEP 2 — TRANSACTIONS (cross-partition, consume-transform-produce)
  producer.initTransactions()
  producer.beginTransaction()
    → consume from input topic
    → process the message
    → produce to output topic
    producer.sendOffsetsToTransaction(offsets, consumerGroupMetadata)
  producer.commitTransaction()
  
  If the producer crashes mid-transaction, the transaction is aborted.
  Consumers with isolation.level=read_committed skip uncommitted records.

## Trading Config Examples

SAFE PRODUCER (at-least-once, good for most trading use cases):
  acks=all
  enable.idempotence=true
  retries=3
  max.in.flight.requests.per.connection=5

TRANSACTIONAL PRODUCER (exactly-once, trade execution + position updates):
  transactional.id=risk-engine-1
  enable.idempotence=true   (automatically set when transactional.id is set)

SAFE CONSUMER (prevents duplicate processing):
  enable.auto.commit=false
  isolation.level=read_committed
  → commit offsets ONLY after successful processing + DB write
""")
    ok(f"Semantics guide: {semantics_guide}")

    idempotent_example = DIRS["scripts"] / "idempotent_producer_example.py"
    idempotent_example.write_text("""\
#!/usr/bin/env python3
\"\"\"
K-06 Example: Idempotent producer configuration.
This script DOES NOT require Kafka — it shows the correct config pattern.
\"\"\"
# WRONG — at-most-once (dangerous for trading)
WRONG_CONFIG = {
    "acks": 0,
    "retries": 0,
}
print("WRONG (at-most-once):", WRONG_CONFIG)
print("  Risk: silent data loss — broker doesn't ACK, message dropped")

# BETTER — at-least-once with idempotent producer (safe for most cases)
BETTER_CONFIG = {
    "acks": "all",
    "enable_idempotence": True,
    "retries": 3,
    "max_in_flight_requests_per_connection": 5,
}
print("\\nBETTER (idempotent, at-least-once → effectively once per partition):", BETTER_CONFIG)
print("  Broker deduplicates retried messages using sequence numbers")
print("  Safe for trade events as long as consumer is also idempotent")

# BEST — transactional (true exactly-once across partitions)
BEST_CONFIG = {
    "transactional_id": "risk-engine-producer-1",
    # enable_idempotence is automatically True with transactional_id
}
print("\\nBEST (transactional, exactly-once):", BEST_CONFIG)
print("  Use for consume-transform-produce pipelines")
print("  Consumer must use isolation_level=read_committed")
print("\\nIn practice: idempotent producer + manual offset commit covers 90%% of trading needs.")
""")
    ok(f"Idempotent example: {idempotent_example}")

    print(f"""
{BOLD}── Reference files ─────────────────────────────────────{RESET}
{CYAN}       cat {semantics_guide}
       python3 {idempotent_example}{RESET}

{BOLD}── Model interview answer ──────────────────────────────{RESET}
  "At-most-once is unacceptable for trade events — silent loss means
   wrong P&L. At-least-once with idempotent producer covers most cases:
   the broker deduplicates retries using sequence numbers. For position
   updates that span multiple partitions, we use Kafka transactions to
   get true exactly-once across the consume-transform-produce pipeline."
""")


# ══════════════════════════════════════════════
#  SCENARIO 7 — Market Data Feed → Kafka Pipeline
# ══════════════════════════════════════════════

def launch_scenario_7():
    header("Scenario K-07 — Market Data Feed Handler → Kafka")
    print("  The feed handler receives raw UDP multicast ticks,")
    print("  normalizes them, and publishes to a Kafka topic.\n")
    print("  This is the entry point of the market data pipeline.\n")

    # Generate a simulated raw feed log (what comes off the wire)
    raw_feed = DIRS["data"] / "raw_feed.log"
    now = datetime.now()
    lines = []
    symbols = ["AAPL", "MSFT", "GOOG", "AMZN", "SPY"]
    prices  = {"AAPL": 185.00, "MSFT": 420.00, "GOOG": 175.00, "AMZN": 198.00, "SPY": 520.00}
    for i in range(30):
        ts = (now + timedelta(milliseconds=i * 50)).strftime("%H:%M:%S.%f")[:-3]
        sym = symbols[i % len(symbols)]
        prices[sym] += random.uniform(-0.05, 0.05)
        seq = 1000 + i
        # Pipe-delimited raw format (as if tr '\001' '|' was applied)
        lines.append(f"SEQ={seq}|TS={ts}|SYM={sym}|BID={prices[sym]-0.05:.2f}|ASK={prices[sym]+0.05:.2f}|BIDSZ={random.randint(100,1000)}|ASKSZ={random.randint(100,1000)}|EXCH=NASDAQ")
    raw_feed.write_text("\n".join(lines) + "\n")
    ok(f"Raw feed log (30 ticks): {raw_feed}")

    # Topic config for market data
    topic_cfg = DIRS["config"] / "market_data_topic.properties"
    topic_cfg.write_text("""\
# Kafka topic: market-data.ticks
# ================================
# High-throughput, short-retention, many partitions

# Create command:
# kafka-topics.sh --bootstrap-server localhost:9092 --create \\
#   --topic market-data.ticks \\
#   --partitions 24 \\
#   --replication-factor 3 \\
#   --config retention.ms=3600000 \\        # 1 hour — market data goes stale fast
#   --config min.insync.replicas=2 \\
#   --config compression.type=lz4 \\        # compress — ticks are repetitive
#   --config max.message.bytes=1048576

# Why different from trade-executions topic?
#   partitions=24        market data is higher volume than trades
#   retention=1hr        yesterday's tick is worthless for real-time consumers
#                        (analytics/storage pipeline persists to HDF5/DB separately)
#   compression=lz4      tick data is very repetitive — compresses 70-80%
#   key=symbol           same symbol → same partition → ordered L2 updates

# Downstream consumers of market-data.ticks:
#   risk-engine-group       → real-time MTM, margin calculation
#   algo-trading-group      → signal generation, execution algo pricing
#   tick-storage-group      → write to HDF5 / TimescaleDB for backtests
#   vwap-calc-group         → streaming VWAP aggregation
#   market-making-group     → quote generation
""")
    ok(f"Market data topic config: {topic_cfg}")

    # Feed handler script — reads raw feed, normalizes, publishes to Kafka
    feed_handler = DIRS["scripts"] / "feed_handler.py"
    feed_handler.write_text(f"""\
#!/usr/bin/env python3
\"\"\"
K-07: Market Data Feed Handler
Reads raw feed log, normalizes ticks, publishes to Kafka topic.

In production: this reads from a live UDP multicast socket instead of a file.
DRY_RUN=True simulates Kafka publish without a real broker.
\"\"\"
import json, time
from datetime import datetime

RAW_FEED  = '{raw_feed}'
TOPIC     = 'market-data.ticks'
BROKER    = 'localhost:9092'
DRY_RUN   = True   # set False to use real Kafka

published = 0
errors    = 0
lag_us    = []

print(f"Feed Handler starting | topic={{TOPIC}} | dry_run={{DRY_RUN}}\\n")
print(f"  {{' SEQ':>6}}  {{'SYM':<5}}  {{'BID':>8}}  {{'ASK':>8}}  {{'SPREAD':>7}}  {{'PART':>5}}  STATUS")
print(f"  " + "-"*60)

with open(RAW_FEED) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue

        # ── Parse raw feed format ──────────────────────────
        try:
            fields = dict(p.split('=', 1) for p in line.split('|') if '=' in p)
            seq  = fields['SEQ']
            sym  = fields['SYM']
            bid  = float(fields['BID'])
            ask  = float(fields['ASK'])
            bsz  = int(fields['BIDSZ'])
            asz  = int(fields['ASKSZ'])
            exch = fields.get('EXCH', '?')
            ts   = fields['TS']
        except Exception as e:
            errors += 1
            print(f"  PARSE ERROR: {{e}} | line={{line[:60]}}")
            continue

        # ── Validate ───────────────────────────────────────
        spread = round(ask - bid, 4)
        if bid >= ask:
            print(f"  SEQ={{seq}} ⚠ CROSSED BOOK (bid={{bid}} >= ask={{ask}}) — routing to DLQ")
            errors += 1
            continue
        if spread > 1.0:
            print(f"  SEQ={{seq}} ⚠ WIDE SPREAD ({{spread:.4f}}) — flagging for review")

        # ── Normalize to internal format ───────────────────
        tick = {{
            "seq":       int(seq),
            "symbol":    sym,
            "bid":       bid,
            "ask":       ask,
            "bid_size":  bsz,
            "ask_size":  asz,
            "mid":       round((bid + ask) / 2, 4),
            "spread":    spread,
            "exchange":  exch,
            "recv_ts":   datetime.now().isoformat(),
        }}

        # Partition by symbol (consistent routing)
        partition = abs(hash(sym)) % 24

        if DRY_RUN:
            print(f"  {{seq:>6}}  {{sym:<5}}  {{bid:>8.2f}}  {{ask:>8.2f}}  {{spread:>7.4f}}  p{{partition:>3}}  OK")
        else:
            from kafka import KafkaProducer
            producer = KafkaProducer(
                bootstrap_servers=[BROKER],
                value_serializer=lambda v: json.dumps(v).encode(),
                key_serializer=lambda k: k.encode(),
                acks='all',
                enable_idempotence=True,
            )
            future = producer.send(TOPIC, key=sym, value=tick)
            meta   = future.get(timeout=5)
            print(f"  {{seq:>6}}  {{sym:<5}}  p{{meta.partition:>3}}@{{meta.offset}}  OK")

        published += 1
        time.sleep(0.005)   # simulate 50ms inter-message interval in slow-motion

print(f"\\n  Published: {{published}}  Errors: {{errors}}")
print(f"  Key routing: symbol → consistent partition → ordered L2 updates per symbol")
print(f"  In production: feed handler processes ~100k-1M ticks/second at market open")
""")
    ok(f"Feed handler: {feed_handler}")

    print(f"""
{BOLD}── Architecture ────────────────────────────────────────{RESET}
  UDP Multicast (exchange)
       ↓  raw pipe-delimited ticks
  Feed Handler (feed_handler.py)
       ↓  validate, normalize, partition by symbol
  Kafka topic: market-data.ticks (24 partitions)
       ↓  consumers subscribe in parallel
  ┌────────────────────────────────────────┐
  │ risk-engine-group  (real-time MTM)     │
  │ algo-trading-group (signal generation) │
  │ tick-storage-group (HDF5 / TimescaleDB)│
  │ vwap-calc-group    (streaming VWAP)    │
  └────────────────────────────────────────┘

{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Read the raw feed log — understand the format
{CYAN}       cat {raw_feed}{RESET}

  2. Run the feed handler — watch normalization + routing
{CYAN}       python3 {feed_handler}{RESET}

  3. Read the topic configuration
{CYAN}       cat {topic_cfg}{RESET}

  4. Use awk to count ticks per symbol from the raw feed
{CYAN}       awk -F'|' '{{for(i=1;i<=NF;i++) if($i~/^SYM=/) syms[substr($i,5)]++}}
       END {{for(s in syms) print syms[s], s}}' {raw_feed} | sort -rn{RESET}

{BOLD}── Key Interview Points ───────────────────────────────{RESET}
  • Feed handler is the bridge: UDP multicast → Kafka
  • Normalize early: all downstream consumers get clean, consistent data
  • Partition by symbol: guaranteed ordering of L2 updates per symbol
  • market-data.ticks: short retention (1hr) — analytics pipeline writes to permanent storage
  • Validate at ingestion: crossed book, wide spread → DLQ before polluting consumers
""")


# ══════════════════════════════════════════════
#  SCENARIO 8 — Market Data Consumer Lag
# ══════════════════════════════════════════════

def launch_scenario_8():
    header("Scenario K-08 — Market Data Consumer Lag at Market Open")
    print("  At 09:30 the tick rate spikes 10x. Consumers can't keep up.")
    print("  Lag grows → downstream systems get stale prices → risk breach.\n")

    # Realistic lag snapshot during market open burst
    lag_file = DIRS["data"] / "market_data_lag_snapshot.txt"
    lag_file.write_text("""\
GROUP               TOPIC                PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG     CONSUMER-ID
risk-engine-group   market-data.ticks    0          450200          450250          50      risk-1-a1b2
risk-engine-group   market-data.ticks    1          448100          449800          1700    risk-1-a1b2
risk-engine-group   market-data.ticks    2          451000          451005          5       risk-2-c3d4
risk-engine-group   market-data.ticks    3          445000          450200          5200    risk-2-c3d4
risk-engine-group   market-data.ticks    4          449000          449010          10      risk-3-e5f6
risk-engine-group   market-data.ticks    5          440000          450500          10500   risk-3-e5f6
risk-engine-group   market-data.ticks    6          448500          448510          10      -
risk-engine-group   market-data.ticks    7          447000          451000          4000    -
risk-engine-group   market-data.ticks    8          450000          450020          20      -
algo-trading-group  market-data.ticks    0          450248          450250          2       algo-1-g7h8
algo-trading-group  market-data.ticks    1          449795          449800          5       algo-1-g7h8
algo-trading-group  market-data.ticks    2          451003          451005          2       algo-2-i9j0
algo-trading-group  market-data.ticks    3          450198          450200          2       algo-2-i9j0
""")
    ok(f"Market data lag snapshot: {lag_file}")

    analyze = DIRS["scripts"] / "market_data_lag_analyzer.py"
    analyze.write_text(f"""\
#!/usr/bin/env python3
\"\"\"K-08: Analyze market data consumer group lag — identify high-lag groups.\"\"\"

LAG_FILE       = '{lag_file}'
STALE_THRESHOLD = 1000   # lag > this = prices are stale for that group

print(f"Market Data Consumer Lag Analysis")
print(f"Stale threshold: {{STALE_THRESHOLD}} messages\\n")

groups = {{}}
with open(LAG_FILE) as f:
    for i, line in enumerate(f):
        if i == 0: continue
        parts = line.split()
        if len(parts) < 6: continue
        group, topic, part = parts[0], parts[1], parts[2]
        lag_str  = parts[5]
        consumer = parts[6] if len(parts) > 6 else '-'
        try:
            lag = int(lag_str)
        except ValueError:
            continue
        if group not in groups:
            groups[group] = {{'total_lag': 0, 'max_lag': 0, 'unassigned': 0, 'stale': 0}}
        groups[group]['total_lag'] += lag
        groups[group]['max_lag']    = max(groups[group]['max_lag'], lag)
        if consumer == '-':
            groups[group]['unassigned'] += 1
        if lag > STALE_THRESHOLD:
            groups[group]['stale'] += 1
            print(f"  ⚠ {{group:<22}} partition={{part:<3}} lag={{lag:>7}} → STALE PRICES")

print()
for group, stats in groups.items():
    health = "✅ OK" if stats['max_lag'] < STALE_THRESHOLD else "🔴 STALE"
    print(f"  {{group:<22}} total_lag={{stats['total_lag']:>8}}  max={{stats['max_lag']:>6}}  unassigned={{stats['unassigned']}}  {{health}}")

print(f\"\"\"
── Root Cause: risk-engine-group ───────────────────────────────
  Partitions 6,7,8: NO CONSUMER (-) — only 3 workers for 9 partitions
  Partitions 1,3,5: HIGH LAG (1700-10500) — consumers can't keep up with burst

── Business Impact ─────────────────────────────────────────────
  risk-engine-group lag=10500 → risk engine sees prices 10,000+ ticks old
  At 200 ticks/sec = ~52 seconds stale → MTM wrong → margin calls wrong
  algo-trading-group lag=5 → OK, algos getting current prices

── Fix ─────────────────────────────────────────────────────────
  Immediate: add 6 more consumers to risk-engine-group (match 9 partitions)
  Short-term: tune max.poll.records=50 (smaller batches, faster commits)
  Long-term: pre-warm consumers before market open (09:25 startup)
  Consider: separate topic per asset class (equities vs futures) for isolation
\"\"\")
""")
    ok(f"Lag analyzer: {analyze}")

    print(f"""
{BOLD}── Lag snapshot (market open burst): ──────────────────{RESET}
{CYAN}       cat {lag_file}{RESET}

{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Analyze the lag — identify which groups have stale prices
{CYAN}       python3 {analyze}{RESET}

  2. Find the worst partition with awk
{CYAN}       awk 'NR>1 {{print $6, $1, "p"$3}}' {lag_file} | sort -rn | head -5{RESET}

  3. On a real cluster:
{CYAN}       watch -n 5 "kafka-consumer-groups.sh \\
         --bootstrap-server localhost:9092 \\
         --group risk-engine-group --describe"{RESET}

{BOLD}── Key Interview Points ───────────────────────────────{RESET}
  • Market data lag = stale prices = wrong risk calculations = real money at risk
  • rule: consumers_in_group ≤ partitions (extra consumers sit idle doing nothing)
  • Pre-warm before market open — don't cold-start consumers at 09:30
  • Separate consumer groups per use case: risk ≠ algo ≠ storage
  • Lag on market-data is more urgent than lag on trade-executions
    (stale prices → bad fills → immediate P&L impact)
""")


# ══════════════════════════════════════════════
#  SCENARIO 9 — Data Pipeline: Tick Storage
# ══════════════════════════════════════════════

def launch_scenario_9():
    header("Scenario K-09 — Data Pipeline: Kafka → Tick Storage")
    print("  The tick-storage pipeline consumes market-data.ticks")
    print("  and writes to persistent storage for backtests and analytics.\n")

    # Generate a simulated Kafka topic log (messages with offsets)
    topic_log = DIRS["data"] / "market_data_ticks_topic.json"
    ticks = []
    now = datetime.now()
    syms   = ["AAPL", "MSFT", "GOOG", "AMZN", "SPY"]
    prices = {"AAPL": 185.00, "MSFT": 420.00, "GOOG": 175.00, "AMZN": 198.00, "SPY": 520.00}
    for i in range(50):
        sym = syms[i % len(syms)]
        prices[sym] += random.uniform(-0.1, 0.1)
        ts  = (now + timedelta(milliseconds=i * 50)).isoformat()
        ticks.append({
            "partition": abs(hash(sym)) % 24,
            "offset":    10000 + i,
            "key":       sym,
            "value": {
                "seq":      1000 + i,
                "symbol":   sym,
                "bid":      round(prices[sym] - 0.05, 2),
                "ask":      round(prices[sym] + 0.05, 2),
                "mid":      round(prices[sym], 2),
                "spread":   0.10,
                "exchange": "NASDAQ",
                "recv_ts":  ts,
            }
        })
    import json as _json
    topic_log.write_text(_json.dumps(ticks, indent=2))
    ok(f"Simulated topic log (50 ticks): {topic_log}")

    storage_out = DIRS["data"] / "tick_storage_output.csv"

    pipeline = DIRS["scripts"] / "tick_storage_pipeline.py"
    pipeline.write_text(f"""\
#!/usr/bin/env python3
\"\"\"
K-09: Tick Storage Pipeline
Consumes from market-data.ticks (simulated), writes to CSV/HDF5.

Pattern: consume → validate → transform → store → commit offset
DRY_RUN=True uses simulated topic log. Set False for real Kafka.
\"\"\"
import json, csv, time
from datetime import datetime

TOPIC_LOG  = '{topic_log}'   # simulated Kafka messages
OUTPUT_CSV = '{storage_out}'
DRY_RUN    = True

COMMIT_EVERY = 10   # commit offset every N messages (tunable)

written    = 0
errors     = 0
last_offset = {{}}  # partition → offset (simulates committed offsets)

print(f"Tick Storage Pipeline | output={{OUTPUT_CSV}}")
print(f"Commit interval: every {{COMMIT_EVERY}} messages\\n")

with open(TOPIC_LOG) as f:
    messages = json.load(f)

with open(OUTPUT_CSV, 'w', newline='') as csvfile:
    writer = csv.DictWriter(csvfile,
        fieldnames=['recv_ts', 'symbol', 'bid', 'ask', 'mid', 'spread', 'exchange', 'offset'])
    writer.writeheader()

    for i, msg in enumerate(messages):
        partition = msg['partition']
        offset    = msg['offset']
        tick      = msg['value']

        # ── Validate ───────────────────────────────────────
        if tick['bid'] >= tick['ask']:
            print(f"  [p{{partition}}@{{offset}}] CROSSED — routing to DLQ")
            errors += 1
            continue

        # ── Transform: add computed fields ─────────────────
        tick['spread'] = round(tick['ask'] - tick['bid'], 4)

        # ── Store ──────────────────────────────────────────
        writer.writerow({{
            'recv_ts':  tick['recv_ts'],
            'symbol':   tick['symbol'],
            'bid':      tick['bid'],
            'ask':      tick['ask'],
            'mid':      tick['mid'],
            'spread':   tick['spread'],
            'exchange': tick['exchange'],
            'offset':   offset,
        }})
        written += 1

        # ── Commit offset (manual, every N messages) ───────
        if written % COMMIT_EVERY == 0:
            last_offset[partition] = offset
            print(f"  Committed offset p{{partition}}@{{offset}} ({{written}} ticks stored)")

    # Final commit
    for p, o in last_offset.items():
        print(f"  Final commit p{{p}}@{{o}}")

print(f"\\n── Pipeline Summary ──────────────────────────────────")
print(f"  Messages processed : {{written + errors}}")
print(f"  Written to storage : {{written}}")
print(f"  Errors (DLQ)       : {{errors}}")
print(f"  Output file        : {{OUTPUT_CSV}}")
print(f\"\"\"
── Pipeline Pattern: at-least-once with idempotent writes ──────
  1. Consume batch from Kafka (max.poll.records messages)
  2. Validate each message (crossed book → DLQ, missing fields → DLQ)
  3. Transform (normalize, enrich, compute derived fields)
  4. Write to storage (CSV/HDF5/TimescaleDB)
     - Use UPSERT or deduplication key to handle reprocessing safely
  5. Commit Kafka offset ONLY after successful write
     If the writer crashes before commit → replay from last committed offset
     Storage must handle duplicate writes (idempotent storage)

── Why commit AFTER write? ──────────────────────────────────────
  If you commit BEFORE writing to storage:
    Kafka thinks you processed the message
    Writer crashes → message is LOST from storage forever
  If you commit AFTER writing to storage:
    Crash before commit → message replayed → may write twice
    Storage must be idempotent (UPSERT by (symbol, timestamp))
  This is at-least-once delivery: storage gets ≥1 write per tick
\"\"\")
""")
    ok(f"Pipeline script: {pipeline}")

    print(f"""
{BOLD}── Architecture ────────────────────────────────────────{RESET}
  market-data.ticks (Kafka)
       ↓  tick-storage-group consumes
  tick_storage_pipeline.py
       ↓  validate → transform → write
  tick_storage_output.csv  (production: HDF5 / TimescaleDB / KDB+)
       ↓
  Backtest engine / Quant research queries

{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Run the pipeline — watch it consume, validate, and store
{CYAN}       python3 {pipeline}{RESET}

  2. Inspect the output CSV
{CYAN}       cat {storage_out}{RESET}

  3. Compute VWAP per symbol from the stored ticks using awk
{CYAN}       awk -F',' 'NR>1 {{vol[$(3)]+=$(4); px[$(3)]+=$(4)*$(6); n[$(3)]++}}
       END {{for(s in vol) print s, px[s]/vol[s]}}' {storage_out}{RESET}

{BOLD}── Key Interview Points ───────────────────────────────{RESET}
  • Commit offset AFTER writing to storage — never before
  • Storage must be idempotent (UPSERT) to handle replay safely
  • DLQ = dead letter queue: bad messages go there, not silently dropped
  • Pipeline must survive broker restart without data loss or duplication
  • Tick storage retention: Kafka 1hr → HDF5/DB forever (different retention per tier)
""")


# ══════════════════════════════════════════════
#  SCENARIO 10 — Streaming VWAP Pipeline
# ══════════════════════════════════════════════

def launch_scenario_10():
    header("Scenario K-10 — Streaming VWAP Pipeline (Consume-Transform-Produce)")
    print("  Consume trade-executions, compute running VWAP per symbol,")
    print("  publish results to market-data.vwap topic.\n")

    # Generate simulated trade events
    trades_log = DIRS["data"] / "trade_executions_topic.json"
    import json as _json
    trades = []
    symbols = ["AAPL", "MSFT", "GOOG"]
    base    = {"AAPL": 185.0, "MSFT": 420.0, "GOOG": 175.0}
    now     = datetime.now()
    for i in range(40):
        sym = symbols[i % len(symbols)]
        px  = round(base[sym] + random.gauss(0, 0.5), 2)
        qty = random.choice([100, 200, 300, 500, 1000])
        trades.append({
            "partition": abs(hash(sym)) % 12,
            "offset":    5000 + i,
            "key":       sym,
            "value": {
                "seq":    500 + i,
                "symbol": sym,
                "side":   random.choice(["BUY", "SELL"]),
                "price":  px,
                "qty":    qty,
                "status": "FILLED",
                "ts":     (now + timedelta(milliseconds=i * 100)).isoformat(),
            }
        })
    trades_log.write_text(_json.dumps(trades, indent=2))
    ok(f"Simulated trade-executions topic: {trades_log}")

    pipeline = DIRS["scripts"] / "vwap_pipeline.py"
    pipeline.write_text(f"""\
#!/usr/bin/env python3
\"\"\"
K-10: Streaming VWAP Pipeline — consume-transform-produce pattern.
Reads trade-executions, maintains running VWAP per symbol,
publishes updated VWAP to market-data.vwap topic.

This is a stateful streaming job — maintains state (VWAP) across messages.
\"\"\"
import json
from collections import defaultdict

TRADES_LOG = '{trades_log}'
DRY_RUN    = True

# State: running VWAP per symbol
total_notional = defaultdict(float)   # sum(price * qty)
total_volume   = defaultdict(int)     # sum(qty)
trade_count    = defaultdict(int)

vwap_output = []   # simulated output topic messages

print(f"Streaming VWAP Pipeline | input=trade-executions | output=market-data.vwap\\n")
print(f"  {{' SEQ':>5}}  {{'SYM':<5}}  {{'SIDE':<5}}  {{'PX':>8}}  {{'QTY':>6}}  {{'VWAP':>10}}  {{'COUNT':>6}}")
print(f"  " + "-"*60)

with open(TRADES_LOG) as f:
    messages = json.load(f)

for msg in messages:
    t = msg['value']
    if t.get('status') != 'FILLED':
        continue

    sym = t['symbol']
    px  = t['price']
    qty = t['qty']

    # Update running VWAP state
    total_notional[sym] += px * qty
    total_volume[sym]   += qty
    trade_count[sym]    += 1

    vwap = round(total_notional[sym] / total_volume[sym], 4)

    # Output message to market-data.vwap topic
    vwap_msg = {{
        "symbol":       sym,
        "vwap":         vwap,
        "total_volume": total_volume[sym],
        "trade_count":  trade_count[sym],
        "last_price":   px,
        "ts":           t['ts'],
    }}
    vwap_output.append(vwap_msg)

    print(f"  {{t['seq']:>5}}  {{sym:<5}}  {{t['side']:<5}}  {{px:>8.2f}}  {{qty:>6}}  {{vwap:>10.4f}}  {{trade_count[sym]:>6}}")

print(f"\\n── Final VWAP per Symbol ────────────────────────────")
for sym in sorted(total_volume.keys()):
    vwap = round(total_notional[sym] / total_volume[sym], 4)
    print(f"  {{sym:<6}}  VWAP={{vwap:.4f}}  volume={{total_volume[sym]:,}}  trades={{trade_count[sym]}}")

print(f\"\"\"
── Consume-Transform-Produce Pattern ───────────────────────────
  INPUT:   trade-executions topic (partitioned by symbol)
  PROCESS: maintain running VWAP state per symbol
  OUTPUT:  market-data.vwap topic (used by risk, algos, display)

  In production (with real Kafka):
    Use Kafka Streams or Faust for stateful stream processing
    State stored in a local RocksDB store (fast, crash-recoverable)
    KTable: symbol → {{vwap, volume, count}} updated per message
    Changelog topic: state changes persisted to Kafka for recovery

── Why symbol partitioning matters here ───────────────────────
  All AAPL trades land on partition 3 (hash("AAPL") % 12)
  The VWAP consumer for partition 3 sees ALL AAPL trades in order
  → it can maintain correct VWAP without coordinating with other consumers
  If AAPL trades were spread across partitions: VWAP would be wrong
    (each consumer only sees partial volume → partial VWAP)

── Stateful vs Stateless Pipelines ────────────────────────────
  Stateless: each message processed independently (tick storage, feed handler)
  Stateful:  maintain state across messages (VWAP, position, OFI)
  Stateful pipelines must handle: state recovery on restart, late arrivals,
  windowing (1min VWAP vs 5min VWAP vs daily VWAP)
\"\"\")
""")
    ok(f"VWAP pipeline: {pipeline}")

    print(f"""
{BOLD}── Architecture ────────────────────────────────────────{RESET}
  trade-executions (Kafka) ─── partitioned by symbol ───→
  vwap_pipeline.py (stateful) → running VWAP per symbol
  market-data.vwap (Kafka) ─→ risk-engine, algo-trading, display

{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Run the VWAP pipeline
{CYAN}       python3 {pipeline}{RESET}

  2. Inspect the raw trade events
{CYAN}       python3 -c "import json; [print(m['value']['symbol'], m['value']['price'], m['value']['qty']) for m in json.load(open('{trades_log}'))]"{RESET}

  3. Compute VWAP manually with awk to verify
{CYAN}       python3 -c "import json; \\
       [print(m['value']['symbol'], m['value']['price'], m['value']['qty']) \\
        for m in json.load(open('{trades_log}'))]" | \\
       awk '{{n[$(1)]+=$(2)*$(3); v[$(1)]+=$(3)}} END {{for(s in v) print s, n[s]/v[s]}}'{RESET}

{BOLD}── Key Interview Points ───────────────────────────────{RESET}
  • Stateful pipelines require state recovery — what happens if the job restarts?
  • Symbol partitioning ensures one consumer owns all state for a symbol
  • VWAP is the most common streaming aggregation in trading Kafka pipelines
  • Kafka Streams / Faust handle windowing, state stores, and changelog topics
  • This same pattern applies to: running P&L, position aggregation, OFI
""")


# ══════════════════════════════════════════════
#  SCENARIO 11 — Dead Letter Queue
# ══════════════════════════════════════════════

def launch_scenario_11():
    header("Scenario K-11 — Dead Letter Queue (DLQ) Pattern")
    print("  Bad messages crash pipeline jobs if not handled.")
    print("  Route failed messages to a DLQ topic instead of losing them.\n")

    # Generate mixed good/bad messages
    import json as _json
    mixed_log = DIRS["data"] / "mixed_ticks_topic.json"
    msgs = [
        {"offset": 1,  "value": {"seq": 1, "symbol": "AAPL", "bid": 184.90, "ask": 185.10, "exchange": "NASDAQ"}},
        {"offset": 2,  "value": {"seq": 2, "symbol": "MSFT", "bid": 419.80, "ask": 420.20, "exchange": "NASDAQ"}},
        {"offset": 3,  "value": {"seq": 3, "symbol": "AAPL", "bid": 185.20, "ask": 185.00, "exchange": "NASDAQ"}},   # CROSSED
        {"offset": 4,  "value": {"seq": 4, "symbol": "???",  "bid": 0.0,    "ask": 0.0,    "exchange": "UNKNOWN"}},  # BAD SYMBOL
        {"offset": 5,  "value": {"seq": 5, "symbol": "GOOG", "bid": 174.90, "ask": 175.10, "exchange": "NYSE"}},
        {"offset": 6,  "value": {"seq": 6, "symbol": "TSLA", "bid": -50.0,  "ask": 240.00, "exchange": "NASDAQ"}},   # NEGATIVE BID
        {"offset": 7,  "value": {"seq": 7, "symbol": "AMZN", "bid": 197.90, "ask": 198.10, "exchange": "NASDAQ"}},
        {"offset": 8,  "value": {"seq": 8}},                                                                          # MISSING FIELDS
        {"offset": 9,  "value": {"seq": 9, "symbol": "SPY",  "bid": 519.90, "ask": 520.10, "exchange": "BATS"}},
        {"offset": 10, "value": {"seq": 10,"symbol": "AAPL", "bid": 184.95, "ask": 185.05, "exchange": "NASDAQ"}},
    ]
    mixed_log.write_text(_json.dumps(msgs, indent=2))
    ok(f"Mixed messages (with bad ones): {mixed_log}")

    dlq_out  = DIRS["data"] / "dlq_market_data.json"
    good_out = DIRS["data"] / "good_ticks.json"

    dlq_script = DIRS["scripts"] / "dlq_handler.py"
    dlq_script.write_text(f"""\
#!/usr/bin/env python3
\"\"\"
K-11: Dead Letter Queue Handler
Processes market-data.ticks with validation; routes bad messages to DLQ.
Bad messages never crash the pipeline — they get quarantined for review.
\"\"\"
import json

TOPIC_LOG = '{mixed_log}'
GOOD_OUT  = '{good_out}'
DLQ_OUT   = '{dlq_out}'

VALID_SYMBOLS = {{"AAPL","MSFT","GOOG","AMZN","SPY","TSLA","QQQ","NVDA"}}

good_msgs = []
dlq_msgs  = []

print(f"Processing messages | good→{{GOOD_OUT}} | bad→DLQ {{DLQ_OUT}}\\n")
print(f"  {{'OFFSET':>6}}  {{'SYM':<6}}  {{'BID':>8}}  {{'ASK':>8}}  STATUS")
print(f"  " + "-"*50)

with open(TOPIC_LOG) as f:
    messages = json.load(f)

for msg in messages:
    offset = msg.get('offset', '?')
    tick   = msg.get('value', {{}})

    # ── Validation rules ───────────────────────────────
    error = None
    sym   = tick.get('symbol', '')
    bid   = tick.get('bid')
    ask   = tick.get('ask')

    if not sym or bid is None or ask is None:
        error = "MISSING_FIELDS"
    elif sym not in VALID_SYMBOLS:
        error = f"UNKNOWN_SYMBOL:{{sym}}"
    elif bid < 0 or ask < 0:
        error = f"NEGATIVE_PRICE bid={{bid}} ask={{ask}}"
    elif bid >= ask:
        error = f"CROSSED_BOOK bid={{bid}} >= ask={{ask}}"

    if error:
        dlq_entry = {{
            "original_offset": offset,
            "error":           error,
            "raw_message":     tick,
            "dlq_ts":          "2026-05-04T09:30:00",
        }}
        dlq_msgs.append(dlq_entry)
        print(f"  {{offset:>6}}  {{str(sym):<6}}  {{'N/A':>8}}  {{'N/A':>8}}  🔴 DLQ: {{error}}")
    else:
        good_msgs.append(tick)
        print(f"  {{offset:>6}}  {{sym:<6}}  {{bid:>8.2f}}  {{ask:>8.2f}}  ✅ OK")

with open(GOOD_OUT, 'w') as f:
    json.dump(good_msgs, f, indent=2)
with open(DLQ_OUT, 'w') as f:
    json.dump(dlq_msgs, f, indent=2)

print(f"\\n── Summary ─────────────────────────────────────────────")
print(f"  Total messages : {{len(messages)}}")
print(f"  Good (stored)  : {{len(good_msgs)}}")
print(f"  DLQ (bad)      : {{len(dlq_msgs)}}")
print()
for d in dlq_msgs:
    print(f"  DLQ offset={{d['original_offset']:>3}}  error={{d['error']}}")

print(f\"\"\"
── Dead Letter Queue Pattern ───────────────────────────────────
  WITHOUT DLQ:  bad message → consumer crashes → lag grows → all processing stops
  WITH DLQ:     bad message → DLQ topic → pipeline continues → ops team reviews DLQ

  DLQ topic config:
    name: market-data.ticks.dlq  (or trade-executions.dlq)
    retention.ms=2592000000      (30 days — long retention for investigation)
    partitions=3                 (lower volume than main topic)

  DLQ Operations:
    Monitor DLQ lag daily — growing DLQ = systematic data quality issue
    Replay from DLQ after fix: kafka-console-consumer | fix | kafka-console-producer
    Alert if DLQ rate > threshold (e.g. 1% of main topic volume)

  Common causes of messages landing in DLQ:
    - Exchange sends bad tick (malformed, wrong price)
    - New instrument not in symbol reference data
    - Schema change in upstream feed handler (version mismatch)
    - Upstream bug (negative bid, zero ask)
\"\"\")
""")
    ok(f"DLQ handler: {dlq_script}")

    print(f"""
{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Look at the mixed input — spot the bad messages
{CYAN}       python3 -c "import json; [print(m['offset'], m['value']) for m in json.load(open('{mixed_log}'))]"{RESET}

  2. Run the DLQ handler — watch validation and routing
{CYAN}       python3 {dlq_script}{RESET}

  3. Inspect what ended up in the DLQ
{CYAN}       python3 -c "import json; [print(d) for d in json.load(open('{dlq_out}'))]"{RESET}

  4. Count DLQ error types with awk
{CYAN}       python3 -c "import json; [print(d['error']) for d in json.load(open('{dlq_out}'))]" | sort | uniq -c{RESET}

{BOLD}── Key Interview Points ───────────────────────────────{RESET}
  • DLQ prevents one bad message from stopping the entire pipeline
  • Never discard bad messages silently — always route to DLQ for audit
  • DLQ retention should be longer than main topic (30 days vs 1 hour)
  • Monitor DLQ volume: spike = upstream data quality incident
  • Replay from DLQ after the root cause is fixed (do not lose the data)
""")


# ══════════════════════════════════════════════
#  SCENARIO 12 — End-to-End Pipeline Triage
# ══════════════════════════════════════════════

def launch_scenario_12():
    header("Scenario K-12 — End-to-End Pipeline Triage")
    print("  This is the job. A researcher opens a ticket.")
    print("  You triage L1, escalate to L2, then write it up for the dev team (L3).\n")
    print("  Ticket: 'AAPL and NVDA data looks wrong for yesterday 14:30-15:00.'")
    print("  Your job: find the root cause across Kafka → K8s → HDF5 before escalating.\n")

    # ── Support ticket ──────────────────────────────────────
    ticket = DIRS["data"] / "support_ticket_k12.txt"
    ticket.write_text("""\
TICKET #2847  |  Priority: HIGH  |  Opened: 2026-05-04 09:14 EST
Opened by:  sarah.lin@firm.com (Quantitative Research)
Assigned to: app-support-queue

SUBJECT: Missing market data for AAPL and NVDA yesterday afternoon

DESCRIPTION:
Hi support team,

I'm running a backtest that uses yesterday's (2026-05-03) tick data and
noticed that AAPL and NVDA have a large gap between approximately 14:30
and 15:00. The HDF5 replay file shows zero ticks for both symbols in
that window. MSFT and TSLA look fine for the same period.

This is affecting two research projects that need to be submitted by EOD.
Can you investigate urgently?

Steps I already tried:
- Verified the HDF5 file path is correct
- Confirmed the issue is only on 2026-05-03 (today's data looks fine)
- Checked with two colleagues — they see the same gap

Thanks,
Sarah
""")
    ok(f"Ticket: {ticket}")

    # ── L1 triage checklist ─────────────────────────────────
    l1_check = DIRS["scripts"] / "k12_l1_triage.sh"
    l1_check.write_text(f"""\
#!/usr/bin/env bash
# K-12: L1 Triage Checklist — run BEFORE escalating
# ====================================================
# Goal: confirm the scope, rule out user error, gather facts for L2.

echo "=== L1 TRIAGE: Ticket #2847 — Missing AAPL/NVDA data 14:30-15:00 ==="
echo ""

echo "1. Verify the issue is real (not user path error)"
echo "   Check HDF5 metadata file:"
cat {DIRS["data"]}/hdf5_metadata_k12.txt
echo ""

echo "2. Confirm scope — is it just AAPL/NVDA or all symbols?"
echo "   (Ticket says MSFT/TSLA are fine — this is a partial outage, not total)"
echo ""

echo "3. Check if the Kafka consumer was lagging at that time"
echo "   Look at lag snapshot saved at 14:35:"
cat {DIRS["logs"]}/kafka_lag_1435_k12.txt
echo ""

echo "4. Check the Kubernetes pod that runs tick storage"
echo "   Look for crashes around 14:28:"
cat {DIRS["logs"]}/tick_storage_pod_k12.log
echo ""

echo "5. Determine: is today's data complete? (ticket says yes)"
echo "   If yes: isolated to 2026-05-03 window → historical backfill needed"
echo ""

echo "L1 CONCLUSION:"
echo "  - Issue confirmed: AAPL + NVDA missing 14:30-15:00 on 2026-05-03"
echo "  - Scope: 2 of 5 symbols affected (partial)"
echo "  - Root cause hint: tick-storage pod OOMKilled at 14:28"
echo "  - Action: escalate to L2 with findings"
""")
    ok(f"L1 checklist: {l1_check}")

    # ── HDF5 metadata file ──────────────────────────────────
    hdf5_meta = DIRS["data"] / "hdf5_metadata_k12.txt"
    hdf5_meta.write_text("""\
# HDF5 Tick File Metadata: 2026-05-03.h5
# Generated by: h5ls -r /data/ticks/2026-05-03.h5
# ================================================

/ticks/AAPL    Dataset {2,284,103}   first_ts=2026-05-03T09:30:00  last_ts=2026-05-03T14:28:47
/ticks/MSFT    Dataset {1,923,847}   first_ts=2026-05-03T09:30:00  last_ts=2026-05-03T15:59:59
/ticks/TSLA    Dataset {3,481,203}   first_ts=2026-05-03T09:30:00  last_ts=2026-05-03T15:59:59
/ticks/GOOGL   Dataset {1,102,394}   first_ts=2026-05-03T09:30:00  last_ts=2026-05-03T15:59:59
/ticks/NVDA    Dataset {1,847,201}   first_ts=2026-05-03T09:30:00  last_ts=2026-05-03T14:28:47
/ticks/QQQ     Dataset {4,103,847}   first_ts=2026-05-03T09:30:00  last_ts=2026-05-03T15:59:59
/ticks/SPY     Dataset {5,291,034}   first_ts=2026-05-03T09:30:00  last_ts=2026-05-03T15:59:59

PROBLEM SYMBOLS:
  AAPL: last_ts=14:28:47  (expected 15:59:59) → missing ~91 minutes
  NVDA: last_ts=14:28:47  (expected 15:59:59) → missing ~91 minutes

NOTE: Both AAPL and NVDA cut off at the SAME timestamp (14:28:47).
      This is NOT a per-symbol data quality issue — it is a pipeline interruption.
      The tick-storage process stopped writing at that exact moment.
""")
    ok(f"HDF5 metadata: {hdf5_meta}")

    # ── Kafka lag snapshot at 14:35 ─────────────────────────
    lag_snap = DIRS["logs"] / "kafka_lag_1435_k12.txt"
    lag_snap.write_text("""\
# kafka-consumer-groups.sh --describe --group tick-storage-group
# Snapshot taken: 2026-05-03 14:35:02 EST
# ================================================================

GROUP               TOPIC                PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG       CONSUMER-ID
tick-storage-group  market-data.ticks    0          8,291,847       8,734,201       442,354   -
tick-storage-group  market-data.ticks    1          6,103,291       6,589,103       485,812   -
tick-storage-group  market-data.ticks    2          7,847,103       8,310,291       463,188   -
tick-storage-group  market-data.ticks    3          5,291,847       5,784,103       492,256   -
tick-storage-group  market-data.ticks    4          9,103,291       9,601,847       498,556   -
tick-storage-group  market-data.ticks    5          4,847,103       5,341,291       494,188   -
tick-storage-group  market-data.ticks    6          6,291,847       6,789,103       497,256   -
tick-storage-group  market-data.ticks    7          7,103,847       7,601,291       497,444   -

CONSUMER-ID: all "-" means NO ACTIVE CONSUMERS in this group.
             The tick-storage pod crashed — all partitions are unassigned.
             Lag is growing at ~10,000 msg/sec (normal market rate).

Total lag: ~3,870,000 messages across 8 partitions
At 10k msg/sec: data is ~387 seconds (6.5 minutes) stale and growing.
""")
    ok(f"Kafka lag snapshot: {lag_snap}")

    # ── Kubernetes pod log ──────────────────────────────────
    pod_log = DIRS["logs"] / "tick_storage_pod_k12.log"
    pod_log.write_text("""\
# kubectl logs tick-storage-worker-7d4f8c9b5-xr2pq -n data-pipelines --previous
# (--previous flag: logs from the terminated container, not the restarted one)
# ================================================================

2026-05-03T14:20:11Z INFO  tick_storage.py  Consuming from market-data.ticks (8 partitions)
2026-05-03T14:20:11Z INFO  Connected to HDF5: /data/ticks/2026-05-03.h5
2026-05-03T14:20:12Z INFO  Processing at 12,847 msg/sec  lag=1,203 msgs
2026-05-03T14:25:03Z INFO  Processing at 11,934 msg/sec  lag=987 msgs
2026-05-03T14:27:41Z WARN  Memory usage: 3,812Mi / 4,096Mi  (93%)
2026-05-03T14:28:01Z WARN  Memory usage: 3,991Mi / 4,096Mi  (97%)  GC thrashing
2026-05-03T14:28:31Z WARN  Memory usage: 4,071Mi / 4,096Mi  (99%)  GC overhead exceeded
2026-05-03T14:28:47Z FATAL tick_storage.py terminated
Killed

# kubectl describe pod tick-storage-worker-7d4f8c9b5-xr2pq -n data-pipelines
# Containers:
#   tick-storage:
#     State: Waiting
#       Reason: CrashLoopBackOff
#     Last State: Terminated
#       Reason:    OOMKilled
#       Exit Code: 137
#       Finished:  Mon, 03 May 2026 14:28:47 +0000
#   Limits:
#     memory: 4Gi
#   Requests:
#     memory: 2Gi
#
# Events:
#   14:28:47  Warning  OOMKilling  Node killed pod due to memory limit exceeded
#   14:28:50  Normal   Pulled      Successfully pulled image data-pipeline:v2.1.4
#   14:28:51  Normal   Started     Started container tick-storage (restart #3 today)
#   14:29:05  Warning  BackOff     Back-off restarting failed container
""")
    ok(f"Pod log: {pod_log}")

    # ── L2 escalation template ──────────────────────────────
    escalation = DIRS["scripts"] / "k12_l2_escalation.md"
    escalation.write_text("""\
# L2 Escalation — Ticket #2847
# ==============================
# Fill this out BEFORE handing to the dev team.
# A complete write-up means fewer back-and-forth questions.

## Summary
Tick storage pod (tick-storage-worker) OOMKilled at 14:28:47 EST on 2026-05-03,
causing all market data writes to stop. AAPL and NVDA HDF5 datasets are incomplete;
the remaining 5 symbols were already fully written before the crash.

## Impact
- **Affected symbols**: AAPL, NVDA
- **Missing window**: 2026-05-03 14:28:47 → 15:59:59 EST (~91 minutes of ticks)
- **Unaffected symbols**: MSFT, TSLA, GOOGL, QQQ, SPY (fully written)
- **Downstream impact**: Research backtests failing; replay website returning empty for window

## Root Cause (L2 finding)
Pod OOMKilled (exit code 137). Memory usage climbed from 93% at 14:27 to limit
at 14:28:47. Pod has been restarting since (CrashLoopBackOff — same OOM on restart).

Memory limit: 4Gi. Actual peak usage from logs: ~4.07Gi.
Three prior restarts today (Events section of kubectl describe) — this is not a one-off.

## Evidence
1. HDF5 metadata: AAPL and NVDA both cut off at 14:28:47 (simultaneous = same process)
2. kubectl logs --previous: "Killed" at 14:28:47, memory 99% utilization
3. kubectl describe: Reason: OOMKilled, Exit Code: 137, restart count 3
4. Kafka consumer lag snapshot (14:35): tick-storage-group has NO active consumers,
   lag ~3.87M msgs across all partitions — confirms consumer stopped at OOM time

## What I Already Checked
- [ ] User path/query error → ruled out (HDF5 file itself is truncated)
- [ ] Exchange feed issue → ruled out (Kafka topic has full data, lag shows messages present)
- [ ] Network issue → ruled out (other symbols written fine by same pod until OOM)
- [ ] Data in Kafka for recovery → YES, market-data.ticks has 1hr retention
  → Window data still in Kafka until ~15:30 EST today (urgent!)

## Recommended Dev Actions
1. Increase memory limit from 4Gi to 6Gi (or investigate memory leak if 4Gi was sufficient before)
2. Replay missing window from Kafka before retention expires (~15:30 today)
3. Update HDF5 files for AAPL/NVDA with backfilled data
4. Notify researcher (Ticket #2847) when replay is complete

## Kafka Replay Window (URGENT)
Topic: market-data.ticks  |  Retention: 1 hour from write time
Messages from 14:28 will expire at ~15:28-15:30 today.
Replay must start NOW or data is permanently lost.
""")
    ok(f"Escalation template: {escalation}")

    print(f"""
{BOLD}── Read the support ticket ───────────────────────────────{RESET}
{CYAN}  cat {ticket}{RESET}

{BOLD}── Step 1: L1 triage — confirm scope and gather evidence ─{RESET}
{CYAN}  bash {l1_check}{RESET}

{BOLD}── Step 2: HDF5 metadata — confirm cutoff timestamp ─────{RESET}
{CYAN}  cat {hdf5_meta}{RESET}

{BOLD}── Step 3: Kafka lag — confirm no active consumer ───────{RESET}
{CYAN}  cat {lag_snap}{RESET}

{BOLD}── Step 4: Pod logs — find OOMKilled at 14:28:47 ────────{RESET}
{CYAN}  cat {pod_log}{RESET}

{BOLD}── Step 5: Write up L2 escalation to dev team ───────────{RESET}
{CYAN}  cat {escalation}{RESET}

{BOLD}── The Triage Path (memorise this sequence) ─────────────{RESET}
  Ticket received
       ↓
  Read the HDF5 metadata → both symbols cut off at SAME timestamp
  (same timestamp = not a per-symbol issue = pipeline crash)
       ↓
  Check Kafka lag → CONSUMER-ID shows "-" → no active consumer = pod died
       ↓
  kubectl logs --previous → "Killed" exit 137 → OOMKilled at 14:28:47
       ↓
  kubectl describe pod → confirms OOMKilled, memory limit 4Gi, restart #3
       ↓
  Check Kafka retention → data still there but expires ~15:30 → flag as URGENT
       ↓
  Write L2 escalation with: impact, root cause, evidence, replay window urgency

{BOLD}── Key Interview Points ─────────────────────────────────{RESET}
  Same cutoff timestamp for multiple symbols = pipeline interruption, not data quality
  exit 137 = OOMKilled (always — this number is not ambiguous)
  kubectl logs --previous = logs from the DEAD container, not the restarted one
  Kafka retention window = if you wait too long, the data is gone permanently
  L2 escalation quality = what separates a good support engineer from a great one
""")


def launch_scenario_99():
    header("Scenario 99 — ALL Kafka Scenarios")
    for fn in [launch_scenario_1, launch_scenario_2, launch_scenario_3,
               launch_scenario_4, launch_scenario_5, launch_scenario_6,
               launch_scenario_7, launch_scenario_8, launch_scenario_9,
               launch_scenario_10, launch_scenario_11, launch_scenario_12]:
        fn(); time.sleep(0.3)


# ══════════════════════════════════════════════
#  TEARDOWN / STATUS / MAIN
# ══════════════════════════════════════════════

def teardown():
    header("Tearing Down Kafka Lab")
    kill_pids(DIRS["pids"])
    kill_strays(["mock_broker"])
    remove_lab_dir(LAB_ROOT)

def show_status():
    _show_status(DIRS["pids"], "Kafka Lab")

SCENARIO_MAP = {
    1:  (launch_scenario_1,  "K-01  Kafka architecture & concepts"),
    2:  (launch_scenario_2,  "K-02  Consumer group lag investigation"),
    3:  (launch_scenario_3,  "K-03  Topic operations & CLI reference"),
    4:  (launch_scenario_4,  "K-04  Broker down incident response"),
    5:  (launch_scenario_5,  "K-05  Python producer + consumer"),
    6:  (launch_scenario_6,  "K-06  Delivery semantics — exactly-once"),
    7:  (launch_scenario_7,  "K-07  Market data feed handler → Kafka pipeline"),
    8:  (launch_scenario_8,  "K-08  Market data consumer lag at market open"),
    9:  (launch_scenario_9,  "K-09  Data pipeline — Kafka → tick storage"),
    10: (launch_scenario_10, "K-10  Streaming VWAP pipeline (consume-transform-produce)"),
    11: (launch_scenario_11, "K-11  Dead letter queue pattern"),
    12: (launch_scenario_12, "K-12  End-to-end pipeline triage (ticket → root cause → escalation)"),
    99: (launch_scenario_99, "      ALL scenarios"),
}

def main():
    parser = argparse.ArgumentParser(description="Kafka Challenge Lab Setup",
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
        fn, _ = SCENARIO_MAP[args.scenario]; fn()
    else:
        header("Kafka Challenge Lab")
        for num, (_, desc) in SCENARIO_MAP.items():
            print(f"    {num:<4} {desc}")
        choice = input("\n  Enter scenario number (or q): ").strip()
        if choice.lower() == "q": return
        try:
            fn, _ = SCENARIO_MAP[int(choice)]; fn()
        except (KeyError, ValueError): err(f"Invalid: {choice}")
    lab_footer("lab_kafka.py")

if __name__ == "__main__":
    main()
