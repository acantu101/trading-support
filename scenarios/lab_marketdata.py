#!/usr/bin/env python3
"""
Support Engineer Challenge Lab — Market Data & Protocols
=========================================================
Covers order book concepts, ITCH/OUCH native protocols, HDF5 tick data,
feed handler gap detection, and SBE binary encoding.

SCENARIOS:
  1   MD-01  Order book — L1/L2/L3, bid/ask, build a simple book
  2   MD-02  ITCH protocol — decode raw NASDAQ ITCH 5.0 messages
  3   MD-03  HDF5 tick data — query historical ticks with h5py
  4   MD-04  Feed handler gap detection — sequence gaps, recovery
  5   MD-05  Binary protocols — SBE layout, struct.unpack
  99         ALL scenarios
"""

import sys
import time
import struct
import json
import argparse
import random
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

LAB_ROOT = Path("/tmp/lab_marketdata")
DIRS = {
    "logs":    LAB_ROOT / "logs",
    "scripts": LAB_ROOT / "scripts",
    "data":    LAB_ROOT / "data",
}

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    GREEN, YELLOW, RED, CYAN, BOLD, RESET, SEP,
    ok, warn, err, info, header, lab_footer,
    create_dirs as _create_dirs,
    remove_lab_dir,
    show_status as _show_status,
)

def create_dirs(): _create_dirs(DIRS)
def show_status(): _show_status(DIRS["logs"], "Market Data Lab")


# ══════════════════════════════════════════════
#  SCENARIO LAUNCHERS
# ══════════════════════════════════════════════

def launch_scenario_1():
    header("Scenario MD-01 — Order Book: L1 / L2 / L3")
    print("  Understand how market data is structured and build")
    print("  a simple order book from a stream of updates.\n")

    # Generate a stream of order book updates
    updates_file = DIRS["data"] / "orderbook_updates.json"
    updates = []
    bids = {185.50: 500, 185.49: 1200, 185.48: 800, 185.47: 2000, 185.46: 1500}
    asks = {185.51: 600, 185.52: 900,  185.53: 1100, 185.54: 700,  185.55: 1800}
    ts = datetime(2026, 4, 28, 9, 30, 0)

    for i in range(50):
        ts += timedelta(milliseconds=random.randint(50, 500))
        side = random.choice(["BUY", "SELL"])
        if side == "BUY":
            price = round(random.choice(list(bids.keys())), 2)
            qty_change = random.choice([-100, -200, 100, 200, 300])
            bids[price] = max(0, bids.get(price, 0) + qty_change)
            if bids[price] == 0:
                del bids[price]
        else:
            price = round(random.choice(list(asks.keys())), 2)
            qty_change = random.choice([-100, -200, 100, 200, 300])
            asks[price] = max(0, asks.get(price, 0) + qty_change)
            if asks[price] == 0:
                del asks[price]
        updates.append({
            "timestamp": ts.isoformat() + "Z",
            "symbol": "AAPL",
            "side": side,
            "price": price,
            "qty": abs(qty_change),
            "action": "ADD" if qty_change > 0 else "REDUCE"
        })

    updates_file.write_text(json.dumps(updates, indent=2))
    ok(f"Order book updates: {updates_file}  ({len(updates)} events)")

    script = DIRS["scripts"] / "order_book.py"
    script.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-01: Build a simple L2 order book from a stream of updates.\"\"\"
import json
from collections import defaultdict

UPDATES_FILE = "{updates_file}"

# Initial L2 book state
bids = {{185.50: 500, 185.49: 1200, 185.48: 800, 185.47: 2000, 185.46: 1500}}
asks = {{185.51: 600, 185.52: 900,  185.53: 1100, 185.54: 700,  185.55: 1800}}

def print_book(title="Order Book"):
    top_bids = sorted(bids.items(), reverse=True)[:5]
    top_asks = sorted(asks.items())[:5]
    best_bid = top_bids[0][0] if top_bids else 0
    best_ask = top_asks[0][0] if top_asks else 0
    spread   = round(best_ask - best_bid, 4)
    mid      = round((best_bid + best_ask) / 2, 4)
    print(f"\\n=== {{title}} ===")
    print(f"  Spread: ${{spread:.4f}}   Mid: ${{mid:.4f}}")
    print(f"  {{' ASK PRICE':>12}}  {{' ASK QTY':>10}}")
    for price, qty in reversed(top_asks):
        print(f"  {{price:>12.2f}}  {{qty:>10,}}")
    print(f"  {'--- SPREAD ---':^25}")
    for price, qty in top_bids:
        print(f"  {{price:>12.2f}}  {{qty:>10,}}")
    print(f"  {{' BID PRICE':>12}}  {{' BID QTY':>10}}")

print_book("Initial L2 Book (top 5 levels)")

# Apply updates
with open(UPDATES_FILE) as f:
    updates = json.load(f)

for upd in updates:
    price = upd["price"]
    qty   = upd["qty"]
    if upd["side"] == "BUY":
        if upd["action"] == "ADD":
            bids[price] = bids.get(price, 0) + qty
        else:
            bids[price] = max(0, bids.get(price, 0) - qty)
            if bids.get(price, 0) == 0:
                bids.pop(price, None)
    else:
        if upd["action"] == "ADD":
            asks[price] = asks.get(price, 0) + qty
        else:
            asks[price] = max(0, asks.get(price, 0) - qty)
            if asks.get(price, 0) == 0:
                asks.pop(price, None)

print_book(f"After {{len(updates)}} updates")

print(\"\"\"
Market Data Levels Explained:
  L1 (Top of Book)  : best bid price + size, best ask price + size only
                      What: 185.50 x 500 / 185.51 x 600
                      Used by: most trading apps, simple algos

  L2 (Market Depth) : all price levels with aggregated size at each level
                      What: full book as shown above (multiple price levels)
                      Used by: market making, execution algos, risk

  L3 (Full Order Book): individual orders at each level (not just aggregated size)
                        What: order ID, time, qty at each price
                        Used by: HFT, exchange matching engine simulation

  VWAP  : Volume-Weighted Average Price — used for execution benchmarking
  TWAP  : Time-Weighted Average Price — execution over a time window
  Spread: ask - bid (tighter = more liquid market)
  Mid   : (ask + bid) / 2 — theoretical fair value
\"\"\")
""")
    ok(f"Order book script: {script}")

    print(f"""
{BOLD}── Run the order book: ─────────────────────────────────{RESET}
{CYAN}       python3 {script}{RESET}

{BOLD}── View the raw updates stream: ────────────────────────{RESET}
{CYAN}       cat {updates_file} | head -30{RESET}

{BOLD}── Key concepts ────────────────────────────────────────{RESET}
  In your role you will support systems that:
  - Receive L1/L2 data from exchanges via multicast UDP (ITCH, native feeds)
  - Maintain order books in memory for risk and routing decisions
  - Detect feed gaps when sequence numbers jump
  - Reconnect and replay missed messages on gap detection
""")


def launch_scenario_2():
    header("Scenario MD-02 — ITCH Protocol Decoder")
    print("  NASDAQ uses ITCH 5.0 as its native market data protocol.")
    print("  It is binary, not text. Decode raw ITCH messages.\n")

    # Generate binary ITCH 5.0 messages
    # ITCH 5.0 Add Order (No MPID): type='A', length=36
    # Fields: MsgType(1) SeqNum(8) Timestamp(6) OrderRef(8) Side(1) Shares(4) Stock(8) Price(4)
    itch_file = DIRS["data"] / "itch_messages.bin"

    messages = []
    seq = 1000
    symbols = [b"AAPL    ", b"GOOGL   ", b"MSFT    ", b"TSLA    "]
    ts_ns = 34200000000000  # 9:30 AM in nanoseconds since midnight

    for i in range(10):
        sym = random.choice(symbols)
        side = random.choice([b"B", b"S"])
        shares = random.randint(100, 2000)
        price_raw = random.randint(18000, 55000)  # ITCH price = actual * 10000

        # Add Order message (type A)
        # >: big-endian, H=uint16, Q=uint64, 6s=6 bytes, Q=uint64, c=char, I=uint32, 8s=8 bytes, I=uint32
        msg = struct.pack(">H", 40)  # message length (payload = 1+8+6+8+1+4+8+4 = 40 bytes)
        msg += b"A"                   # message type
        msg += struct.pack(">Q", seq)  # sequence number
        msg += struct.pack(">6s", ts_ns.to_bytes(6, "big"))  # timestamp (6 bytes)
        msg += struct.pack(">Q", 100000 + i)   # order reference number
        msg += side                             # buy/sell indicator
        msg += struct.pack(">I", shares)        # shares
        msg += sym                              # stock (8 bytes, space-padded)
        msg += struct.pack(">I", price_raw)     # price (x10000)
        messages.append(msg)
        seq += 1
        ts_ns += random.randint(100000, 5000000)  # ~0.1ms to 5ms between messages

    with open(itch_file, "wb") as f:
        for msg in messages:
            f.write(msg)
    ok(f"ITCH binary file: {itch_file}  ({len(messages)} messages, {sum(len(m) for m in messages)} bytes)")

    decoder = DIRS["scripts"] / "itch_decoder.py"
    decoder.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-02: Decode raw NASDAQ ITCH 5.0 Add Order messages.\"\"\"
import struct

ITCH_FILE = "{itch_file}"

MSG_TYPES = {{
    b"A": "Add Order (No MPID)",
    b"F": "Add Order (MPID)",
    b"E": "Order Executed",
    b"C": "Order Executed with Price",
    b"X": "Order Cancel",
    b"D": "Order Delete",
    b"U": "Order Replace",
    b"P": "Trade (Non-Cross)",
    b"S": "System Event",
    b"R": "Stock Directory",
}}

SIDES = {{b"B": "BUY", b"S": "SELL"}}

print(f"Decoding ITCH 5.0 binary messages from: {itch_file}\\n")
print(f"  {{' #':>3}} {{' TYPE':<25}} {{' SEQ':>8}} {{' SYMBOL':<8}} {{' SIDE':<5}} {{' SHARES':>7}} {{' PRICE':>10}}")
print(f"  {{'-'*3}} {{'-'*25}} {{'-'*8}} {{'-'*8}} {{'-'*5}} {{'-'*7}} {{'-'*10}}")

with open(ITCH_FILE, "rb") as f:
    msg_num = 0
    while True:
        length_bytes = f.read(2)
        if len(length_bytes) < 2:
            break
        length = struct.unpack(">H", length_bytes)[0]
        payload = f.read(length)
        if len(payload) < length:
            break

        msg_type = bytes([payload[0]])
        type_name = MSG_TYPES.get(msg_type, f"Unknown({{msg_type}})")

        if msg_type == b"A" and length >= 40:
            seq       = struct.unpack(">Q", payload[1:9])[0]
            ts_bytes  = payload[9:15]
            ts_ns     = int.from_bytes(ts_bytes, "big")
            order_ref = struct.unpack(">Q", payload[15:23])[0]
            side      = SIDES.get(bytes([payload[23]]), "?")
            shares    = struct.unpack(">I", payload[24:28])[0]
            stock     = payload[28:36].decode("ascii").strip()
            price_raw = struct.unpack(">I", payload[36:40])[0]
            price     = price_raw / 10000.0
            ts_sec    = ts_ns / 1_000_000_000
            h, rem    = divmod(int(ts_sec), 3600)
            m, s      = divmod(rem, 60)

            msg_num += 1
            print(f"  {{msg_num:>3}} {{type_name:<25}} {{seq:>8}} {{stock:<8}} {{side:<5}} {{shares:>7,}} {{price:>10.4f}}")
        else:
            msg_num += 1
            seq = struct.unpack(">Q", payload[1:9])[0] if length >= 9 else 0
            print(f"  {{msg_num:>3}} {{type_name:<25}} {{seq:>8}} (no decode for this type)")

print(f\"\"\"
ITCH 5.0 Add Order Message Layout (type 'A', 36 bytes after length):
  Offset  Size  Type      Field
  ------  ----  --------  -----
  0       1     char      Message Type ('A')
  1       8     uint64    Sequence Number
  9       6     bytes     Timestamp (nanoseconds since midnight, big-endian)
  15      8     uint64    Order Reference Number
  23      1     char      Buy/Sell Indicator ('B' or 'S')
  24      4     uint32    Shares
  28      8     string    Stock (ASCII, space-padded to 8 chars)
  36      4     uint32    Price (x10000 — divide by 10000 for dollars)

Key differences from FIX:
  FIX  : text-based (tag=value), human-readable, slower to parse
  ITCH : binary, fixed-width fields, struct.unpack, much faster
  ITCH is one-way (exchange → you), FIX is bidirectional (orders + fills)
  ITCH is multicast UDP — no TCP handshake, ultra-low latency
\"\"\")
""")
    ok(f"ITCH decoder: {decoder}")

    print(f"""
{BOLD}── ITCH binary file (not human readable): ──────────────{RESET}
{CYAN}       xxd {itch_file} | head -20{RESET}

{BOLD}── Decode it: ──────────────────────────────────────────{RESET}
{CYAN}       python3 {decoder}{RESET}

{BOLD}── ITCH vs FIX ─────────────────────────────────────────{RESET}
  FIX  : text, tag=value, bidirectional, orders + fills
  ITCH : binary, multicast, one-way, market data only
  OUCH : binary, TCP, bidirectional, NASDAQ order entry (like FIX but faster)
  SBE  : Simple Binary Encoding — CME, ICE, many modern exchanges

{BOLD}── Where you see this in your role ─────────────────────{RESET}
  The market-data-feed service receives ITCH/native feeds from exchanges.
  A feed gap (sequence number jump) means missed messages → stale order book.
  Your job: detect the gap, alert, trigger reconnect/replay.
""")


def launch_scenario_3():
    header("Scenario MD-03 — HDF5 Tick Data")
    print("  Quants store historical tick data in HDF5 for backtests.")
    print("  Learn what it is, how to query it, and why it is used.\n")

    # Try to use h5py — fall back to a pure Python simulation if not installed
    hdf5_file = DIRS["data"] / "ticks_2026.h5"
    try:
        import h5py
        import numpy as np

        with h5py.File(hdf5_file, "w") as f:
            for symbol in ["AAPL", "GOOGL", "MSFT", "TSLA"]:
                grp = f.create_group(f"ticks/{symbol}/2026/04/28")
                n = 1000
                base_ts = 1745836200  # 2026-04-28 09:30:00 UTC unix
                timestamps = np.array([base_ts + i * 0.5 for i in range(n)], dtype=np.float64)
                base_price = {"AAPL": 185.5, "GOOGL": 141.2, "MSFT": 380.1, "TSLA": 248.0}[symbol]
                prices = base_price + np.cumsum(np.random.randn(n) * 0.05)
                sizes  = np.random.randint(100, 2000, size=n)
                grp.create_dataset("timestamp", data=timestamps, compression="gzip")
                grp.create_dataset("price",     data=prices,     compression="gzip")
                grp.create_dataset("size",       data=sizes,      compression="gzip")

        ok(f"HDF5 file created: {hdf5_file}  (4 symbols, 1000 ticks each)")
        h5py_available = True
    except ImportError:
        ok("h5py not installed — using CSV simulation (concepts are identical)")
        h5py_available = False
        # Fall back to CSV
        csv_file = DIRS["data"] / "ticks_2026_AAPL.csv"
        lines = ["timestamp,price,size"]
        base_ts = 1745836200.0
        price = 185.5
        for i in range(1000):
            price += random.gauss(0, 0.05)
            lines.append(f"{base_ts + i*0.5:.3f},{price:.4f},{random.randint(100,2000)}")
        csv_file.write_text("\n".join(lines) + "\n")
        ok(f"CSV fallback: {csv_file}")

    script = DIRS["scripts"] / "hdf5_query.py"
    if h5py_available:
        script.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-03: Query HDF5 tick data — VWAP, price range, tick count.\"\"\"
import h5py
import numpy as np

HDF5_FILE = "{hdf5_file}"

print(f"HDF5 file structure:")
with h5py.File(HDF5_FILE, "r") as f:
    def show_tree(name, obj):
        indent = "  " * name.count("/")
        kind = "GROUP" if isinstance(obj, h5py.Group) else f"DATASET shape={{obj.shape}}"
        print(f"  {{indent}}{{name.split('/')[-1]}}  [{{kind}}]")
    f.visititems(show_tree)

print("\\n=== AAPL Tick Analysis ===")
with h5py.File(HDF5_FILE, "r") as f:
    grp = f["ticks/AAPL/2026/04/28"]
    prices     = grp["price"][:]
    sizes      = grp["size"][:]
    timestamps = grp["timestamp"][:]

    vwap    = np.sum(prices * sizes) / np.sum(sizes)
    total_v = int(np.sum(sizes))

    print(f"  Ticks:       {{len(prices):,}}")
    print(f"  Price range: ${{prices.min():.4f}} — ${{prices.max():.4f}}")
    print(f"  VWAP:        ${{vwap:.4f}}")
    print(f"  Total volume: {{total_v:,}} shares")
    print(f"  Time range:  {{timestamps[0]:.0f}} — {{timestamps[-1]:.0f}} (unix ts)")

print("\\n=== All symbols VWAP comparison ===")
with h5py.File(HDF5_FILE, "r") as f:
    for sym in ["AAPL", "GOOGL", "MSFT", "TSLA"]:
        grp    = f[f"ticks/{{sym}}/2026/04/28"]
        p, s   = grp["price"][:], grp["size"][:]
        vwap   = np.sum(p * s) / np.sum(s)
        print(f"  {{sym:<6}}  ticks={{len(p):,}}  VWAP=${{vwap:.4f}}  range=${{p.min():.2f}}-${{p.max():.2f}}")

print(\"\"\"
Why HDF5 for market data?
  Hierarchical  : data/symbol/year/month/day — easy to navigate
  Columnar      : read just "price" without loading "size" — fast
  Compressed    : gzip inside the file — smaller than CSV
  NumPy native  : dataset[:] returns a numpy array directly
  Random access : seek to any row without reading the whole file
  vs CSV        : CSV is ~10x larger, ~20x slower to read for large datasets
  vs SQL        : HDF5 has no query planner overhead for pure timeseries reads
\"\"\")
""")
    else:
        script.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-03: HDF5 tick data concepts (using CSV fallback — h5py not installed).\"\"\"
import csv, statistics

CSV_FILE = "{DIRS["data"] / "ticks_2026_AAPL.csv"}"

prices, sizes = [], []
with open(CSV_FILE) as f:
    reader = csv.DictReader(f)
    for row in reader:
        prices.append(float(row["price"]))
        sizes.append(int(row["size"]))

vwap    = sum(p * s for p, s in zip(prices, sizes)) / sum(sizes)
print(f"AAPL Tick Analysis (CSV fallback — same concepts apply to HDF5)")
print(f"  Ticks:       {{len(prices):,}}")
print(f"  Price range: ${{min(prices):.4f}} — ${{max(prices):.4f}}")
print(f"  VWAP:        ${{vwap:.4f}}")
print(f"  Total volume: {{sum(sizes):,}}")

print(\"\"\"
HDF5 vs CSV:
  HDF5: hierarchical, compressed, columnar, NumPy-native, fast random access
  CSV:  flat, uncompressed, row-oriented, slow for large timeseries
  Install h5py to run the full HDF5 version: pip install h5py numpy
\"\"\")
""")
    ok(f"Query script: {script}")

    print(f"""
{BOLD}── Run the tick data analysis: ─────────────────────────{RESET}
{CYAN}       python3 {script}{RESET}

{BOLD}── Key HDF5 concepts ───────────────────────────────────{RESET}
  File      : one .h5 file per day or per year
  Group     : like a folder — /ticks/AAPL/2026/04/28/
  Dataset   : array of data — timestamps, prices, sizes
  Compression: gzip inside the file, transparent to the reader
  h5py      : Python library to read/write HDF5

{BOLD}── Where HDF5 is used in trading ───────────────────────{RESET}
  Historical tick storage for backtest replay
  Factor data for quant research (P/E ratios, vol surfaces)
  Risk scenario data (stress test matrices)
  Anything that is large, time-series, and needs fast columnar reads
""")


def launch_scenario_4():
    header("Scenario MD-04 — Feed Handler Gap Detection")
    print("  The market data feed handler receives sequenced UDP packets.")
    print("  Detect gaps, identify missing messages, and simulate recovery.\n")

    # Generate a packet stream with intentional gaps
    packets_file = DIRS["data"] / "feed_packets.json"
    base_ts = datetime(2026, 4, 28, 9, 30, 0)
    packets = []
    seq = 1

    for i in range(80):
        base_ts += timedelta(milliseconds=random.randint(1, 50))
        # Introduce gaps at seq 15-17 and 42
        if seq in (15, 16, 17, 42):
            seq += 1
            continue
        packets.append({
            "seq": seq,
            "timestamp": base_ts.isoformat() + "Z",
            "symbol": random.choice(["AAPL", "MSFT", "GOOGL"]),
            "bid": round(185.50 + random.gauss(0, 0.1), 2),
            "ask": round(185.52 + random.gauss(0, 0.1), 2),
        })
        seq += 1

    packets_file.write_text(json.dumps(packets, indent=2))
    ok(f"Feed packets: {packets_file}  ({len(packets)} packets, gaps at seq 15-17 and 42)")

    script = DIRS["scripts"] / "gap_detector.py"
    script.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-04: Detect sequence gaps in a market data feed.\"\"\"
import json
from datetime import datetime

PACKETS_FILE = "{packets_file}"

with open(PACKETS_FILE) as f:
    packets = json.load(f)

print(f"Processing {{len(packets)}} packets...\\n")

expected_seq = None
gaps = []
last_good_ts = None

for pkt in packets:
    seq = pkt["seq"]
    ts  = pkt["timestamp"]

    if expected_seq is None:
        expected_seq = seq
        print(f"  First packet: seq={{seq}}  ts={{ts}}")

    if seq != expected_seq:
        gap_start = expected_seq
        gap_end   = seq - 1
        gap_size  = gap_end - gap_start + 1
        gaps.append((gap_start, gap_end, gap_size, last_good_ts, ts))
        print(f"  ⚠ GAP detected: seq {{gap_start}} to {{gap_end}} ({{gap_size}} messages missing)")
        print(f"      Last good: {{last_good_ts}}")
        print(f"      Resumed:   {{ts}}")
        expected_seq = seq + 1
    else:
        expected_seq = seq + 1

    last_good_ts = ts

print(f"\\n=== Summary ===")
print(f"  Total packets received: {{len(packets)}}")
print(f"  Gaps detected:          {{len(gaps)}}")
for gap_start, gap_end, gap_size, before_ts, after_ts in gaps:
    print(f"  Gap {{gap_start}}-{{gap_end}}: {{gap_size}} missing messages")

print(f\"\"\"
=== Recovery Procedure ===
When a gap is detected:

1. Send a Retransmission Request to the exchange:
   "Please resend messages {{gap_start}} through {{gap_end}}"
   (ITCH: use the gap-fill UDP replay channel)
   (FIX:  send ResendRequest(35=2) with BeginSeqNo/EndSeqNo)

2. While waiting for the replay:
   - Mark the order book as STALE for affected symbols
   - Risk checks should use last-known-good data with wider margins
   - Do NOT trade on a stale book

3. After receiving the replay:
   - Apply missed messages in sequence order
   - Mark order book as FRESH
   - Resume normal operation

4. If replay not received within timeout (e.g. 5 seconds):
   - Drop TCP/UDP connection
   - Reconnect to exchange
   - Request full book snapshot
   - Rebuild order book from snapshot

Business impact of undetected gaps:
   - Order book shows wrong bid/ask → orders fill at wrong price
   - Risk limits calculated on stale positions → limit breaches
   - Surveillance misses trading activity → regulatory exposure
\"\"\")
""")
    ok(f"Gap detector: {script}")

    print(f"""
{BOLD}── Feed packets (with gaps): ───────────────────────────{RESET}
{CYAN}       cat {packets_file} | python3 -m json.tool | grep seq | head -25{RESET}

{BOLD}── Run gap detection: ──────────────────────────────────{RESET}
{CYAN}       python3 {script}{RESET}

{BOLD}── Gap detection logic ─────────────────────────────────{RESET}
  Every packet has a sequence number.
  Expected: each packet seq = previous seq + 1
  If seq jumps (e.g. 14 → 18): messages 15,16,17 are missing → GAP

{BOLD}── Where you see this in your role ─────────────────────{RESET}
  Your market-data-feed service logs will show gap detection events.
  When you see "sequence gap detected" in logs — that is this scenario.
  The service should auto-recover, but if gaps are frequent → investigate
  network packet loss, multicast group membership, or exchange issues.
""")


def launch_scenario_5():
    header("Scenario MD-05 — Binary Protocols & SBE")
    print("  SBE (Simple Binary Encoding) is used by CME, ICE, and")
    print("  many modern exchanges. Understand binary encoding.\n")

    # Create a binary SBE-like message
    sbe_file = DIRS["data"] / "sbe_messages.bin"

    messages = []
    # Simplified SBE market data message:
    # templateId(2) schemaId(2) version(2) blockLength(2)
    # header above = 8 bytes, then body:
    # transactTime(8) securityId(8) bidPx(8) offerPx(8) bidSize(4) offerSize(4) = 40 bytes body
    TEMPLATE_ID   = 1  # MarketDataIncrementalRefresh
    SCHEMA_ID     = 1
    VERSION       = 1
    BLOCK_LENGTH  = 40

    symbol_ids = {1001: "AAPL", 1002: "GOOGL", 1003: "MSFT", 1004: "TSLA"}
    ts_base    = 1745836200000000000  # ns

    for i in range(8):
        sec_id   = random.choice(list(symbol_ids.keys()))
        bid      = round(random.uniform(140, 500), 4)
        offer    = round(bid + random.uniform(0.01, 0.05), 4)
        bid_raw  = int(bid * 10000)
        ask_raw  = int(offer * 10000)
        bid_sz   = random.randint(100, 5000)
        ask_sz   = random.randint(100, 5000)
        ts       = ts_base + i * 500000000

        # SBE header (8 bytes, little-endian for CME SBE)
        header = struct.pack("<HHHH", BLOCK_LENGTH, TEMPLATE_ID, SCHEMA_ID, VERSION)
        # SBE body (little-endian)
        body   = struct.pack("<QqqqII", ts, sec_id, bid_raw, ask_raw, bid_sz, ask_sz)
        messages.append(header + body)

    with open(sbe_file, "wb") as f:
        for msg in messages:
            f.write(msg)
    ok(f"SBE binary file: {sbe_file}  ({len(messages)} messages)")

    decoder = DIRS["scripts"] / "sbe_decoder.py"
    decoder.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-05: Decode simplified SBE (Simple Binary Encoding) messages.\"\"\"
import struct

SBE_FILE = "{sbe_file}"

SYMBOL_IDS = {{1001: "AAPL", 1002: "GOOGL", 1003: "MSFT", 1004: "TSLA"}}

# SBE message = 8-byte header + 40-byte body = 48 bytes total
MSG_SIZE = 48
HEADER_FMT = "<HHHH"   # little-endian: blockLen, templateId, schemaId, version
BODY_FMT   = "<QqqqII" # little-endian: timestamp, secId, bidRaw, askRaw, bidSz, askSz

print(f"Decoding SBE messages from: {sbe_file}\\n")
print(f"  {{' #':>3}} {{' SYMBOL':<8}} {{' BID':>10}} {{' ASK':>10}} {{' BID_SZ':>8}} {{' ASK_SZ':>8}} {{' SPREAD':>8}}")
print(f"  {{'-'*3}} {{'-'*8}} {{'-'*10}} {{'-'*10}} {{'-'*8}} {{'-'*8}} {{'-'*8}}")

with open(SBE_FILE, "rb") as f:
    msg_num = 0
    while True:
        raw = f.read(MSG_SIZE)
        if len(raw) < MSG_SIZE:
            break
        msg_num += 1

        # Decode header
        block_len, template_id, schema_id, version = struct.unpack(HEADER_FMT, raw[:8])

        # Decode body
        ts, sec_id, bid_raw, ask_raw, bid_sz, ask_sz = struct.unpack(BODY_FMT, raw[8:])

        bid    = bid_raw / 10000.0
        ask    = ask_raw / 10000.0
        spread = round(ask - bid, 4)
        symbol = SYMBOL_IDS.get(sec_id, f"ID{{sec_id}}")

        print(f"  {{msg_num:>3}} {{symbol:<8}} {{bid:>10.4f}} {{ask:>10.4f}} {{bid_sz:>8,}} {{ask_sz:>8,}} {{spread:>8.4f}}")

print(f\"\"\"
SBE (Simple Binary Encoding) Explained:
  Used by: CME Group, ICE, Deutsche Börse, many modern exchanges
  Encoding: binary, fixed-width fields, extremely compact
  Endianness: little-endian (CME) or big-endian (ITCH) — check the spec!

  Key difference from ITCH:
    ITCH  : big-endian, multicast UDP, market data only (exchange → you)
    SBE   : usually little-endian, used for both market data AND order entry
    FIX   : text, tag=value, human readable, slower

  Why binary protocols?
    FIX message: "35=D|49=FIRM|55=AAPL|54=1|38=100|44=185.50|" (~50 bytes, text)
    SBE message: same data in ~20 bytes, no parsing needed, just struct.unpack
    At 1 million messages/second: 30MB/s vs 50MB/s, and ~10x faster to parse

  Struct format chars:
    < = little-endian (Intel/AMD, CME)
    > = big-endian (NASDAQ ITCH, network byte order)
    H = uint16 (2 bytes)    h = int16
    I = uint32 (4 bytes)    i = int32
    Q = uint64 (8 bytes)    q = int64
    s = bytes (n chars)     f = float32   d = float64
\"\"\")
""")
    ok(f"SBE decoder: {decoder}")

    print(f"""
{BOLD}── SBE binary (hex view): ──────────────────────────────{RESET}
{CYAN}       xxd {sbe_file} | head -20{RESET}

{BOLD}── Decode it: ──────────────────────────────────────────{RESET}
{CYAN}       python3 {decoder}{RESET}

{BOLD}── Protocol comparison ─────────────────────────────────{RESET}
  FIX   : text, tag=value, bidirectional, universal, easy to read
  ITCH  : binary, multicast UDP, market data only, NASDAQ
  OUCH  : binary, TCP, order entry, NASDAQ (like FIX but faster)
  SBE   : binary, CME/ICE/ICE, market data + order entry
  FAST  : compressed FIX, older, being replaced by SBE
""")


def launch_scenario_99():
    header("Scenario 99 — ALL Market Data Scenarios")
    for fn in [launch_scenario_1, launch_scenario_2, launch_scenario_3,
               launch_scenario_4, launch_scenario_5]:
        fn()
        time.sleep(0.2)


def teardown():
    header("Tearing Down Market Data Lab")
    remove_lab_dir(LAB_ROOT)


SCENARIO_MAP = {
    1:  (launch_scenario_1, "MD-01  Order book L1/L2/L3"),
    2:  (launch_scenario_2, "MD-02  ITCH protocol decode"),
    3:  (launch_scenario_3, "MD-03  HDF5 tick data"),
    4:  (launch_scenario_4, "MD-04  Feed handler gap detection"),
    5:  (launch_scenario_5, "MD-05  SBE binary protocol"),
    99: (launch_scenario_99, "      ALL scenarios"),
}


def main():
    parser = argparse.ArgumentParser(description="Market Data & Protocols Challenge Lab",
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
        header("Market Data & Protocols Challenge Lab")
        for num, (_, desc) in SCENARIO_MAP.items():
            print(f"    {num:<4} {desc}")
        choice = input("\n  Enter scenario number (or q): ").strip()
        if choice.lower() == "q": return
        try:
            fn, _ = SCENARIO_MAP[int(choice)]; fn()
        except (KeyError, ValueError): err(f"Invalid: {choice}")
    lab_footer("lab_marketdata.py")


if __name__ == "__main__":
    main()
