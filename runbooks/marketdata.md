# Runbook: Market Data & Protocols

## Overview

This runbook covers market data concepts, native exchange protocols, HDF5 tick storage, feed handler gap detection, and binary encoding. All scenarios map to `lab_marketdata.py` (MD-01 through MD-05).

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
