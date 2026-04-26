# Runbook: Python & Bash Scripting

## Overview

This runbook covers scripting patterns used by support engineers in trading environments: log monitoring, log parsing with awk, process watchdogs, REST API clients, FIX message parsing, and Kafka lag monitoring. All scenarios map to `lab_python.py` (P-01 through P-06).

---

## P-01 — Bash Log Monitor

### The pattern

```bash
#!/bin/bash
# Monitor a log file and alert when CRITICAL count exceeds threshold

LOG_FILE="${1:-/tmp/lab_python/logs/trading_app.log}"
THRESHOLD=5
INTERVAL=10

echo "Monitoring $LOG_FILE for CRITICAL events (threshold=$THRESHOLD)..."

while true; do
    COUNT=$(tail -n 200 "$LOG_FILE" | grep "CRITICAL" | wc -l)
    if [ "$COUNT" -gt "$THRESHOLD" ]; then
        TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
        echo "[$TIMESTAMP] ALERT: $COUNT CRITICAL events in recent log lines!"
        # Production: send to PagerDuty / Slack / email here
    fi
    sleep $INTERVAL
done
```

### Key bash patterns

```bash
# Count matches in last N lines
tail -n 200 "$LOG" | grep "ERROR" | wc -l

# Check if a pattern appeared in the last minute
awk -v t="$(date -d '1 minute ago' '+%Y-%m-%d %H:%M')" '$0 > t' "$LOG" \
  | grep "CRITICAL" | wc -l

# Alert once and reset (avoid alert storms)
ALERTED=0
while true; do
    COUNT=$(tail -n 200 "$LOG" | grep "CRITICAL" | wc -l)
    if [ "$COUNT" -gt "$THRESHOLD" ] && [ "$ALERTED" -eq 0 ]; then
        echo "ALERT: $COUNT CRITICAL events"
        ALERTED=1
    elif [ "$COUNT" -le "$THRESHOLD" ]; then
        ALERTED=0   # reset when count drops
    fi
    sleep 10
done

# Send to syslog
logger -t trading-monitor "CRITICAL count: $COUNT"
```

---

## P-02 — awk Log Parsing

### Structured trading log format

```
2024-01-15 09:30:01 INFO ORDER FILLED symbol=AAPL qty=100 price=185.50 side=BUY
```

### Useful awk one-liners

```bash
LOG="/tmp/lab_python/logs/structured_trades.log"

# Count filled orders per symbol
grep "FILLED" "$LOG" \
  | awk -F'symbol=' '{print $2}' \
  | awk '{print $1}' \
  | sort | uniq -c | sort -rn

# Total volume (qty) per symbol for fills
grep "FILLED" "$LOG" | awk '{
    for(i=1;i<=NF;i++) {
        if ($i ~ /^symbol=/) sym=substr($i,8)
        if ($i ~ /^qty=/)    qty=substr($i,5)+0
    }
    vol[sym]+=qty
}
END { for (s in vol) printf "%8d  %s\n", vol[s], s }' | sort -rn

# Log level distribution
awk '{print $3}' "$LOG" | sort | uniq -c | sort -rn

# Total notional (qty * price) across all fills
grep "FILLED" "$LOG" | awk '{
    for(i=1;i<=NF;i++) {
        if ($i ~ /^qty=/)   qty=substr($i,5)+0
        if ($i ~ /^price=/) price=substr($i,7)+0
    }
    total += qty * price
}
END { printf "Total notional: $%.2f\n", total }'

# Errors between 09:30 and 09:31
awk '/09:3[01]/ && /ERROR/' "$LOG"

# Top 10 error messages ranked
grep "ERROR" "$LOG" \
  | awk '{$1=$2=$3=""; print $0}' \
  | sort | uniq -c | sort -rn | head -10
```

### awk Field Reference

```
$0   entire line
$1   first field (space-delimited by default)
NF   number of fields
NR   current line number
FS   field separator (set with -F or FS=":")
```

---

## P-03 — Python Process Watchdog

```python
#!/usr/bin/env python3
"""Monitors a process and restarts it on crash with exponential backoff."""
import subprocess
import time
import sys
from datetime import datetime

COMMAND     = [sys.executable, '/tmp/lab_python/scripts/crashy_oms.py']
MAX_RETRIES = 5
BASE_DELAY  = 2   # seconds

def ts():
    return datetime.now().strftime('%H:%M:%S')

restart_count = 0
while restart_count < MAX_RETRIES:
    print(f"[{ts()}] Starting (attempt {restart_count + 1}/{MAX_RETRIES})...")
    proc = subprocess.Popen(COMMAND)
    exit_code = proc.wait()
    restart_count += 1

    print(f"[{ts()}] Exited with code {exit_code}")
    if exit_code == 0:
        print(f"[{ts()}] Clean exit — stopping watchdog.")
        break

    delay = BASE_DELAY * (2 ** (restart_count - 1))   # exponential backoff
    print(f"[{ts()}] Restarting in {delay}s...")
    time.sleep(delay)
else:
    print(f"[{ts()}] Max retries reached — paging on-call!")
    sys.exit(1)
```

### Process management patterns

```python
import subprocess

# Launch and forget
proc = subprocess.Popen(['python3', 'worker.py'])

# Launch and wait for completion
result = subprocess.run(['python3', 'script.py'], capture_output=True, text=True)
print(result.stdout)
print(result.stderr)

# Non-blocking poll
proc = subprocess.Popen(...)
if proc.poll() is None:
    print("Still running")
else:
    print(f"Exited: {proc.returncode}")

# Kill if it hangs
try:
    proc.wait(timeout=30)
except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait()
```

---

## P-04 — REST API Client

```python
#!/usr/bin/env python3
"""Query a trading REST API using only stdlib."""
import urllib.request
import json

BASE_URL = 'http://127.0.0.1:8765'

def get(endpoint: str):
    url = BASE_URL + endpoint
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} on {endpoint}")
        return None
    except Exception as e:
        print(f"Error fetching {endpoint}: {e}")
        return None

# Health check
health = get('/api/health')
print(f"Status: {health.get('status')}  uptime: {health.get('uptime_s')}s")

# All orders
orders = get('/api/orders')
for o in orders or []:
    flag = '⚠' if o['status'] == 'REJECTED' else ' '
    print(f"  {flag} {o['id']} {o['symbol']:5} {o['side']:4} {o['qty']:4} @ ${o['price']}  [{o['status']}]")

# Rejection report
rejected = [o for o in (orders or []) if o['status'] == 'REJECTED']
print(f"\nRejected: {len(rejected)}")
for o in rejected:
    print(f"  {o['id']} {o['symbol']} {o['side']} {o['qty']}")
```

### Using requests library (when available)

```python
import requests

response = requests.get(f'{BASE_URL}/api/orders', timeout=5)
response.raise_for_status()   # raises on 4xx/5xx
orders = response.json()

# POST with JSON body
response = requests.post(
    f'{BASE_URL}/api/orders',
    json={'symbol': 'AAPL', 'qty': 100, 'side': 'BUY'},
    headers={'Authorization': f'Bearer {token}'},
    timeout=5,
)
```

---

## P-05 — FIX Message Parser

```python
#!/usr/bin/env python3
"""Parse raw FIX 4.4 messages from a file."""

FIX_TAGS = {
    '8':  'BeginString',  '9':  'BodyLength',   '35': 'MsgType',
    '49': 'SenderCompID', '56': 'TargetCompID', '34': 'MsgSeqNum',
    '11': 'ClOrdID',      '55': 'Symbol',        '54': 'Side',
    '38': 'OrderQty',     '40': 'OrdType',       '44': 'Price',
    '31': 'LastPx',       '32': 'LastQty',       '14': 'CumQty',
    '39': 'OrdStatus',    '10': 'CheckSum',
}

MSG_TYPES = {
    '0': 'Heartbeat', 'A': 'Logon', '5': 'Logout',
    'D': 'NewOrderSingle', '8': 'ExecutionReport', 'F': 'OrderCancelRequest',
}

SIDES      = {'1': 'BUY', '2': 'SELL'}
ORD_STATUS = {'0': 'New', '1': 'PartialFill', '2': 'Filled', '8': 'Rejected'}

def parse_fix(raw: str) -> dict:
    return {
        tag: val
        for pair in raw.split('\x01')
        if '=' in pair
        for tag, val in [pair.split('=', 1)]
    }

# Process a file of FIX messages
with open('/tmp/lab_python/data/fix_messages.txt') as f:
    for i, line in enumerate(f, 1):
        line = line.strip()
        if not line:
            continue
        fields = parse_fix(line)
        msg_type = fields.get('35', '?')
        print(f"Message #{i}: {MSG_TYPES.get(msg_type, msg_type)}")
        if msg_type == 'D':
            print(f"  {SIDES.get(fields.get('54'))} {fields.get('38')} "
                  f"{fields.get('55')} @ {fields.get('44', 'MARKET')}")
        elif msg_type == '8':
            print(f"  Status={ORD_STATUS.get(fields.get('39'))} "
                  f"LastPx={fields.get('31')} CumQty={fields.get('14')}")
```

---

## P-06 — Kafka Consumer Lag Monitor

```python
#!/usr/bin/env python3
"""Read a lag snapshot JSON and alert on high-lag partitions."""
import json
import sys

LAG_FILE  = '/tmp/lab_python/data/consumer_lag_snapshot.json'
THRESHOLD = 100

with open(LAG_FILE) as f:
    data = json.load(f)

print(f"Group: {data['group']}  |  Snapshot: {data['timestamp']}\n")
print(f"  {'TOPIC':<25} {'PART':>5}  {'LAG':>8}  {'CONSUMER':<20}  STATUS")
print(f"  {'-'*25} {'-'*5}  {'-'*8}  {'-'*20}  {'-'*8}")

alerts = []
for p in sorted(data['partitions'], key=lambda x: -x['lag']):
    if p['consumer'] == '-':
        status = 'NO CONSUMER'
        alerts.append(p)
    elif p['lag'] > THRESHOLD:
        status = 'HIGH LAG'
        alerts.append(p)
    elif p['lag'] > 50:
        status = 'ELEVATED'
    else:
        status = 'OK'
    print(f"  {p['topic']:<25} {p['partition']:>5}  {p['lag']:>8}  {p['consumer']:<20}  {status}")

if alerts:
    print(f"\n{len(alerts)} partition(s) need attention:")
    for a in alerts:
        if a['consumer'] == '-':
            print(f"  partition {a['partition']}: no consumer — add worker instances")
        else:
            print(f"  partition {a['partition']}: lag={a['lag']} — consumer too slow")
```

### Production version using kafka-python

```python
from kafka import KafkaAdminClient, KafkaConsumer

def get_consumer_lag(broker: str, group: str, topic: str) -> dict:
    """Return {partition: lag} for a consumer group on a topic."""
    consumer = KafkaConsumer(
        bootstrap_servers=[broker],
        group_id=group,
    )
    partitions = consumer.partitions_for_topic(topic)
    from kafka import TopicPartition
    tps = [TopicPartition(topic, p) for p in partitions]

    committed = {tp: consumer.committed(tp) or 0 for tp in tps}
    end_offsets = consumer.end_offsets(tps)

    consumer.close()
    return {tp.partition: end_offsets[tp] - committed[tp] for tp in tps}
```

---

## Bash Cheat Sheet for Support Engineers

```bash
# Process grep (avoid matching the grep itself)
ps aux | grep '[o]ms_client'
pgrep -la oms_client

# Follow log with grep filter
tail -f /var/log/trading/app.log | grep --line-buffered "ERROR\|CRITICAL"

# Run command every N seconds
watch -n 5 'ps aux --sort=-%cpu | head -10'

# Check if a port is listening
ss -tlnp | grep :8080
nc -zv 127.0.0.1 8080 && echo open || echo closed

# Time a command
time python3 process_trades.py

# Run in background, capture output
nohup python3 worker.py > /var/log/worker.log 2>&1 &
echo $! > /var/run/worker.pid

# Kill a process by port
fuser -k 8080/tcp

# HTTP request from command line
curl -s http://localhost:8765/api/health | python3 -m json.tool
wget -qO- http://localhost:8765/api/orders

# Check if a Python package is installed
python3 -c "import kafka; print(kafka.__version__)"
pip show kafka-python
```

---

## Escalation Criteria

| Condition | Action |
|-----------|--------|
| Watchdog max retries exceeded | Page on-call — service is unable to stay up |
| Log monitor alerting continuously for > 5 minutes | Escalate — sustained production error rate |
| REST API returning 5xx consistently | Escalate to API owner — server-side issue |
| Kafka lag growing unbounded in monitoring script | Escalate — consumer or broker capacity issue |
| FIX parser seeing unknown MsgType | New message type not in dict — update parser; inform dev team |
