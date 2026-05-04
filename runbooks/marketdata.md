# Runbook: Market Data & Protocols

## Overview

This runbook covers market data concepts, native exchange protocols, HDF5 tick storage, feed handler gap detection, binary encoding, live UDP multicast operations, order book management, and market impact analysis. All scenarios map to `lab_marketdata.py` (MD-01 through MD-14).

---

## MD-01 — Order Book: L1 / L2 / L3

### Market data levels

| Level | Data | Used by |
|---|---|---|
| L1 (Top of Book) | Best bid + size, best ask + size only | Most trading apps, basic algos |
| L2 (Market Depth) | All price levels with aggregated size | Market making, execution algos, risk |
| L3 (Full Order Book) | Individual orders at each level (order ID, time, qty) | HFT, exchange matching simulation |

### Order book terminology

| Term | Definition |
|---|---|
| Bid | Highest price a buyer is willing to pay |
| Ask / Offer | Lowest price a seller is willing to accept |
| Spread | Ask − Bid (tighter = more liquid) |
| Mid | (Ask + Bid) / 2 — theoretical fair value |
| Top of Book | Best bid and best ask |
| Depth | Number of price levels visible in L2 |
| VWAP | Volume-Weighted Average Price |
| TWAP | Time-Weighted Average Price |

### Simple order book in Python

```python
from collections import defaultdict

bids = {185.50: 500, 185.49: 1200, 185.48: 800}
asks = {185.51: 600, 185.52: 900,  185.53: 1100}

def best_bid(): return max(bids.keys())
def best_ask(): return min(asks.keys())
def spread():   return round(best_ask() - best_bid(), 4)
def mid():      return round((best_ask() + best_bid()) / 2, 4)

def apply_update(side, price, qty, action):
    book = bids if side == "BUY" else asks
    if action == "ADD":
        book[price] = book.get(price, 0) + qty
    elif action == "REDUCE":
        book[price] = max(0, book.get(price, 0) - qty)
        if book[price] == 0:
            del book[price]
    elif action == "DELETE":
        book.pop(price, None)
```

---

## MD-02 — ITCH Protocol

### What is ITCH?

NASDAQ ITCH 5.0 is NASDAQ's native market data protocol:
- **Binary**, not text — compact, fast to parse
- **Multicast UDP** — one-way, exchange pushes data to subscribers
- **Sequenced** — every message has a sequence number for gap detection
- **Market data only** — you cannot send orders over ITCH (use OUCH for that)

### ITCH 5.0 Add Order message layout

```
Field             Size  Type      Notes
--------------    ----  --------  -----
Message Length    2     uint16    Total bytes in this message
Message Type      1     char      'A' = Add Order (No MPID)
Sequence Number   8     uint64    Monotonically increasing
Timestamp         6     bytes     Nanoseconds since midnight (big-endian)
Order Ref Number  8     uint64    Unique order identifier
Buy/Sell          1     char      'B' = Buy, 'S' = Sell
Shares            4     uint32    Quantity
Stock             8     string    ASCII, space-padded to 8 chars
Price             4     uint32    Price × 10000 (divide by 10000 for dollars)
```

### Decoding in Python

```python
import struct

def decode_itch_add_order(raw: bytes) -> dict:
    # raw starts AFTER the 2-byte length prefix
    msg_type  = chr(raw[0])
    seq       = struct.unpack_from(">Q", raw, 1)[0]
    ts_bytes  = raw[9:15]
    ts_ns     = int.from_bytes(ts_bytes, "big")
    order_ref = struct.unpack_from(">Q", raw, 15)[0]
    side      = "BUY" if raw[23] == ord("B") else "SELL"
    shares    = struct.unpack_from(">I", raw, 24)[0]
    stock     = raw[28:36].decode("ascii").strip()
    price     = struct.unpack_from(">I", raw, 36)[0] / 10000.0
    return {
        "type": msg_type, "seq": seq, "ts_ns": ts_ns,
        "order_ref": order_ref, "side": side,
        "shares": shares, "stock": stock, "price": price
    }
```

### ITCH message types

| Type | Name | Description |
|---|---|---|
| `S` | System Event | Market open/close, trading halts |
| `R` | Stock Directory | Reference data for each symbol |
| `A` | Add Order (No MPID) | New order added to the book |
| `F` | Add Order (MPID) | New order with market participant ID |
| `E` | Order Executed | Order fully or partially filled |
| `X` | Order Cancel | Order quantity reduced |
| `D` | Order Delete | Order removed from book entirely |
| `U` | Order Replace | Order replaced at new price/qty |
| `P` | Trade (Non-Cross) | Executed trade (not on the book) |

---

## MD-03 — HDF5 Tick Data

### What is HDF5?

HDF5 (Hierarchical Data Format) is the standard storage format for large market data sets:
- **Hierarchical:** organized like a file system — `/ticks/AAPL/2026/04/28/`
- **Columnar:** read only `price` without loading `size` — fast for analysis
- **Compressed:** gzip inside the file, transparent to reader
- **NumPy-native:** `dataset[:]` returns a numpy array directly
- **Random access:** seek to any row without reading the whole file

### Basic h5py operations

```python
import h5py
import numpy as np

# Open and explore
with h5py.File("ticks_2026.h5", "r") as f:
    f.visititems(lambda name, obj: print(name, type(obj)))

# Read a dataset
with h5py.File("ticks_2026.h5", "r") as f:
    prices = f["ticks/AAPL/2026/04/28/price"][:]      # full array
    first_100 = f["ticks/AAPL/2026/04/28/price"][:100] # slice

# VWAP calculation
with h5py.File("ticks_2026.h5", "r") as f:
    grp    = f["ticks/AAPL/2026/04/28"]
    prices = grp["price"][:]
    sizes  = grp["size"][:]
    vwap   = np.sum(prices * sizes) / np.sum(sizes)

# Write data
with h5py.File("ticks_2026.h5", "w") as f:
    grp = f.create_group("ticks/AAPL/2026/04/28")
    grp.create_dataset("timestamp", data=timestamps, compression="gzip")
    grp.create_dataset("price",     data=prices,     compression="gzip")
    grp.create_dataset("size",       data=sizes,      compression="gzip")
```

### HDF5 vs alternatives

| Format | Size | Read speed | Query | Use case |
|---|---|---|---|---|
| HDF5 | Small (compressed) | Very fast (columnar) | h5py, pandas | Tick data, factor data |
| CSV | Large | Slow | pandas | Simple exports |
| Parquet | Small | Fast | pandas, Athena | Processed trade data, S3 |
| SQL | Medium | Medium | SQL | Relational queries |

### Common HDF5 support tasks

```python
# Check file size and group structure
import os, h5py
print(f"File size: {os.path.getsize('ticks.h5') / 1e6:.1f} MB")

with h5py.File("ticks.h5", "r") as f:
    # Count total ticks across all symbols
    total = 0
    def count(name, obj):
        global total
        if isinstance(obj, h5py.Dataset) and name.endswith("price"):
            total += len(obj)
    f.visititems(count)
    print(f"Total ticks: {total:,}")
```

---

## MD-04 — Feed Handler Gap Detection

### What is a sequence gap?

Every message from an exchange feed has a monotonically increasing sequence number. A gap occurs when the sequence jumps:

```
Received: seq 14, seq 15, seq 16, seq 20   ← gap: messages 17, 18, 19 missing
```

Gaps are caused by:
- UDP packet loss (multicast delivery is not guaranteed)
- Feed handler restart
- Network congestion or failover
- Exchange system event (trading halt, symbol add)

### Gap detection logic

```python
expected_seq = None

for packet in packets:
    seq = packet["seq"]
    if expected_seq is None:
        expected_seq = seq
    if seq != expected_seq:
        print(f"GAP: expected {expected_seq}, got {seq} — missing {seq - expected_seq} messages")
        # Trigger recovery
    expected_seq = seq + 1
```

### Recovery procedure

```
1. Detect gap (seq jump)
2. Send Retransmission Request to exchange replay channel
   - ITCH: use the NASDAQ Glimpse UDP replay service
   - FIX:  send ResendRequest (35=2) with BeginSeqNo/EndSeqNo
3. While waiting for replay:
   - Mark affected symbols as STALE
   - Risk engine uses last-known-good data with wider margins
   - Do NOT trade on a stale order book
4. Receive replay, apply in sequence order
5. Mark symbols as FRESH, resume normal operation
6. If no replay within timeout (5s): disconnect, reconnect, request full snapshot
```

### Business impact of undetected gaps

| Impact | Consequence |
|---|---|
| Stale order book | Orders fill at wrong price |
| Stale risk position | Risk limit breaches undetected |
| Missing trades | Surveillance misses activity → regulatory exposure |
| Wrong VWAP | Execution benchmarks incorrect |

### Feed gap logs to watch for

```
WARN [market-data-feed] Sequence gap detected: expected=15 received=20 symbol=AAPL
WARN [market-data-feed] Requesting replay: seq 15-19
INFO [market-data-feed] Replay received: 5 messages applied
INFO [market-data-feed] Symbol AAPL order book refreshed

# Bad pattern — gap not recovering:
ERROR [market-data-feed] Replay timeout after 5000ms — reconnecting
WARN [market-data-feed] Full snapshot requested for AAPL
```

---

## MD-05 — Binary Protocols & SBE

### Protocol comparison

| Protocol | Encoding | Transport | Direction | Used by |
|---|---|---|---|---|
| FIX 4.x | Text (tag=value) | TCP | Bidirectional | Universal — orders + fills |
| ITCH 5.0 | Binary | Multicast UDP | One-way (exchange→firm) | NASDAQ market data |
| OUCH 4.x | Binary | TCP | Bidirectional | NASDAQ order entry |
| SBE | Binary | UDP or TCP | Both | CME, ICE, Deutsche Börse |
| FAST | Compressed FIX | Multicast | One-way | Older feeds, being replaced |

### SBE (Simple Binary Encoding)

SBE is the modern binary protocol used by CME Group, ICE, and many exchanges:
- Fixed-width fields — no parsing overhead
- Little-endian (CME) or big-endian (check the spec)
- Schema-defined — a `.xml` schema file describes every message

### Struct format cheatsheet

```python
import struct

# Format prefix
# < = little-endian (Intel/AMD, CME SBE)
# > = big-endian (ITCH, network byte order)

# Type codes
# H = uint16 (2 bytes)    h = int16
# I = uint32 (4 bytes)    i = int32
# Q = uint64 (8 bytes)    q = int64
# f = float32             d = float64
# s = bytes (prefix with count: 8s = 8 bytes)

# Decode a CME SBE-like message (little-endian)
header_fmt = "<HHHH"   # blockLength, templateId, schemaId, version
body_fmt   = "<QqqqII" # timestamp, securityId, bidRaw, askRaw, bidSz, askSz

header = struct.unpack(header_fmt, raw[:8])
body   = struct.unpack(body_fmt, raw[8:48])

bid_price = body[2] / 10000.0  # price stored as integer × 10000
ask_price = body[3] / 10000.0
```

### Endianness quick check

```python
# If decoded price looks like garbage (9999999999) try the other endian
# ITCH = big-endian (>)
# CME SBE = little-endian (<)
# Check the exchange spec document to confirm
```

---

## Escalation Criteria

| Condition | Action |
|---|---|
| Feed gap rate > 5/minute | Escalate — network issue or exchange problem |
| Feed completely down (no ticks for >30s) | Escalate — reconnect procedure, check exchange status page |
| Order book shows crossed market (bid > ask) | Escalate — missed a delete/cancel message, book corrupted |
| HDF5 file corrupted | Do not delete — escalate, restore from S3 backup |
| All feed sessions disconnecting simultaneously | Check multicast group membership and network team |

---

## MD-06 to MD-10 — Live UDP Multicast Operations

### MD-06: Receive from primary/secondary feed

```python
import socket, struct

def join_multicast(group: str, port: int, iface: str = "0.0.0.0"):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(iface))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    return sock

# A/B feed arbitration: prefer primary, fall back to secondary on gap or silence
# Track last received sequence number on each feed
# If primary gaps: switch to secondary for recovery; switch back after snapshot
```

### MD-07: Sequence gap detection and recovery

```
Normal: SEQ 1, 2, 3, 4, 5 ...
Gap:    SEQ 1, 2, 3, [4, 5, 6 missing], 7, 8 ...

On gap detected:
1. Mark book as STALE — stop pricing until recovered
2. Request retransmission (if exchange supports it) or wait for snapshot
3. Apply snapshot to rebuild book
4. Apply incrementals with SEQ > snapshot_seq
5. Mark book as LIVE — resume pricing
```

### MD-08: NIC buffer tuning for high-throughput feeds

```bash
# Check current kernel receive buffer maximum
sysctl net.core.rmem_max    # default: 212992 (208KB) — too small for multicast

# Increase to 256MB for high-throughput market data
sudo sysctl -w net.core.rmem_max=268435456
sudo sysctl -w net.core.rmem_default=268435456

# Persist across reboots
echo "net.core.rmem_max=268435456" | sudo tee -a /etc/sysctl.conf

# Set per-socket buffer in the feed handler
sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 67108864)  # 64MB
```

### MD-09: Feed latency measurement

```python
# Exchange embeds send timestamp in each packet header
# Calculate one-way latency: local_recv_time - exchange_send_time

import time

def measure_latency(packet):
    exchange_ts_us = parse_exchange_timestamp(packet)   # microseconds since epoch
    local_ts_us    = time.time_ns() // 1000             # convert ns to us
    latency_us     = local_ts_us - exchange_ts_us
    return latency_us

# Normal: 50-300µs for co-located systems
# Spike > 1ms: network congestion, NIC interrupt delay, or CPU scheduling issue
```

### MD-10: IGMP multicast group membership

```bash
# List all multicast groups the host is subscribed to
ip maddr show

# Or
netstat -g

# Verify IGMP membership for a specific group
ip maddr show | grep "224.1.1.10"

# IGMP membership is managed automatically by the OS when
# IP_ADD_MEMBERSHIP is called on a socket
```

---

## MD-11 — Order Book Invalidation

When a sequence gap is detected in the order book update stream, the book must be marked STALE immediately to prevent pricing on stale data.

```python
class OrderBook:
    def __init__(self, symbol):
        self.symbol = symbol
        self.bids   = {}    # price → quantity
        self.asks   = {}
        self.status = "LIVE"
        self.last_seq = 0

    def apply(self, msg):
        seq = msg['seq']

        # Gap detected — invalidate immediately
        if seq != self.last_seq + 1:
            self.status = "STALE"
            raise BookInvalidated(
                f"{self.symbol}: gap at seq {self.last_seq+1}–{seq-1}"
            )

        self.last_seq = seq
        action = msg['action']

        if action == 'ADD':
            self._add(msg)
        elif action == 'MODIFY':
            self._modify(msg)
        elif action == 'DELETE':
            self._delete(msg)
        elif action == 'EXECUTE':
            self._execute(msg)
```

### Recovery procedure

```
1. Mark book STALE on gap
2. Request snapshot from exchange (or wait for periodic snapshot)
3. Receive snapshot at SEQ=N — rebuild bids/asks from scratch
4. Apply all queued incrementals with SEQ > N in order
5. Mark book LIVE — resume pricing
```

---

## MD-12 — Snapshot + Incremental Recovery

Exchanges publish periodic snapshots to allow recovery from sequence gaps without waiting for a full session reset.

```python
def recover_from_snapshot(snapshot, incrementals):
    """Rebuild order book from snapshot + pending incrementals."""
    # snapshot = {'seq': 50, 'bids': [...], 'asks': [...]}
    # incrementals = list of updates received after the gap

    book = OrderBook(snapshot['symbol'])
    book.bids = {p: q for p, q in snapshot['bids']}
    book.asks = {p: q for p, q in snapshot['asks']}
    snap_seq  = snapshot['seq']
    book.last_seq = snap_seq

    # Apply only incrementals with SEQ > snapshot_seq
    for inc in sorted(incrementals, key=lambda x: x['seq']):
        if inc['seq'] <= snap_seq:
            continue   # stale — already reflected in snapshot
        book.apply(inc)

    book.status = "LIVE"
    return book
```

---

## MD-13 — Crossed Book Detection

A crossed book (bid >= ask) indicates a data feed error and must be caught before the price reaches downstream systems.

```python
def check_crossed(book):
    if not book.bids or not book.asks:
        return False

    best_bid = max(book.bids.keys())
    best_ask = min(book.asks.keys())

    if best_bid >= best_ask:
        raise CrossedBook(
            f"{book.symbol}: bid={best_bid} >= ask={best_ask} — FEED ERROR"
        )
    return True

# Crossed book causes:
# 1. Sequence gap — stale bid/ask from different time points
# 2. Feed handler bug — applied update to wrong side
# 3. Exchange error — malformed packet
# Action: mark book STALE, alert ops, do not use for pricing
```

---

## MD-14 — Market Impact and Slippage

When executing a large order, you "walk the book" — consuming multiple price levels before filling completely. The difference between the expected price and the actual average fill price is slippage.

```python
def calculate_market_impact(order_side, order_qty, book):
    """Calculate average fill price and slippage for a market order."""
    levels = sorted(book.asks.items()) if order_side == 'BUY' else \
             sorted(book.bids.items(), reverse=True)

    remaining = order_qty
    total_cost = 0.0
    fills = []

    for price, qty in levels:
        filled = min(remaining, qty)
        total_cost += filled * price
        fills.append((price, filled))
        remaining -= filled
        if remaining == 0:
            break

    if remaining > 0:
        raise InsufficientLiquidity(f"Only {order_qty - remaining} of {order_qty} can fill")

    avg_fill_price = total_cost / order_qty
    reference_price = levels[0][0]   # best ask (BUY) or best bid (SELL)
    slippage_bps = abs(avg_fill_price - reference_price) / reference_price * 10000

    return avg_fill_price, slippage_bps, fills

# Example: 4500-share BUY walking 3 ask levels
# Level 1: 185.10 × 1000 = $185,100
# Level 2: 185.15 × 2000 = $370,300
# Level 3: 185.20 × 1500 = $277,800
# Average fill: $833,200 / 4500 = $185.16
# Slippage: (185.16 - 185.10) / 185.10 × 10000 = 3.2 bps
```

### Slippage reference

| Slippage | Context |
|----------|---------|
| < 1 bps | Liquid large-cap, small order |
| 1–5 bps | Normal for medium-sized orders |
| 5–20 bps | Large order or thin market |
| > 20 bps | Market impact significant — consider TWAP/VWAP execution |
