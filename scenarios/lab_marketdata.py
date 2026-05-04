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
import socket
import argparse
import random
import threading
import multiprocessing
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

LAB_ROOT = Path("/tmp/lab_marketdata")
DIRS = {
    "logs":    LAB_ROOT / "logs",
    "scripts": LAB_ROOT / "scripts",
    "data":    LAB_ROOT / "data",
    "pids":    LAB_ROOT / "run",
}

sys.path.insert(0, str(Path(__file__).parent))
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
def show_status():  _show_status(DIRS["pids"], "Market Data Lab")

# ── Live feed constants ────────────────────────────────
MCAST_PRIMARY   = ("224.1.1.10", 5010)
MCAST_SECONDARY = ("224.1.1.11", 5011)
SYMBOLS     = ["AAPL", "MSFT", "GOOG", "AMZN", "SPY", "QQQ", "TSLA", "NVDA"]
BASE_PRICES = {"AAPL": 185.00, "MSFT": 420.00, "GOOG": 175.00, "AMZN": 198.00,
               "SPY": 520.00,  "QQQ": 445.00,  "TSLA": 240.00, "NVDA": 875.00}


# ══════════════════════════════════════════════
#  BACKGROUND WORKERS (live multicast)
# ══════════════════════════════════════════════

def _live_feed(group, port, name, drop_seqs=None, stop_after=None, interval=0.05):
    """Generic multicast tick sender. Drops specified seqs; goes silent after stop_after."""
    try:
        import setproctitle; setproctitle.setproctitle(name)
    except ImportError:
        pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    drop = set(drop_seqs or [])
    prices = dict(BASE_PRICES)
    start, seq = time.time(), 1
    while True:
        if stop_after and (time.time() - start) > stop_after:
            time.sleep(1); continue
        sym = SYMBOLS[seq % len(SYMBOLS)]
        prices[sym] += 0.01 * (1 if seq % 3 else -1)
        ts  = int(time.time() * 1_000_000)
        msg = f"SEQ={seq}|SYM={sym}|PX={prices[sym]:.2f}|QTY={100+(seq%20)*50}|TS={ts}"
        if seq not in drop:
            try: sock.sendto(msg.encode(), (group, port))
            except Exception: pass
        seq += 1
        time.sleep(interval)


def _heartbeat_feed(group, port, name, stop_after=20, hb_interval=1.0):
    """Sends ticks + heartbeats, goes completely silent after stop_after seconds."""
    try:
        import setproctitle; setproctitle.setproctitle(name)
    except ImportError:
        pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    prices = dict(BASE_PRICES)
    start, seq, last_hb = time.time(), 1, time.time()
    while True:
        if (time.time() - start) > stop_after:
            time.sleep(0.5); continue
        now = time.time()
        ts  = int(now * 1_000_000)
        if now - last_hb >= hb_interval:
            sock.sendto(f"SEQ={seq}|TYPE=HB|TS={ts}".encode(), (group, port))
            last_hb = now; seq += 1
        sym = SYMBOLS[seq % len(SYMBOLS)]
        prices[sym] += 0.01
        sock.sendto(f"SEQ={seq}|TYPE=TICK|SYM={sym}|PX={prices[sym]:.2f}|TS={ts}".encode(),
                    (group, port))
        seq += 1
        time.sleep(0.1)


def _latency_feed(group, port, name, interval=0.05):
    """Sends ticks with embedded exchange timestamps + random jitter spikes."""
    try:
        import setproctitle; setproctitle.setproctitle(name)
    except ImportError:
        pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    prices = dict(BASE_PRICES)
    seq = 1
    while True:
        sym = SYMBOLS[seq % len(SYMBOLS)]
        prices[sym] += 0.01
        exchange_ts = int(time.time() * 1_000_000)
        jitter_us = random.gauss(200, 50)
        if random.random() < 0.05:
            jitter_us += random.uniform(4000, 8000)   # 5% spike
        time.sleep(max(0, jitter_us / 1_000_000))
        msg = f"SEQ={seq}|SYM={sym}|PX={prices[sym]:.2f}|EXCHANGE_TS={exchange_ts}"
        try: sock.sendto(msg.encode(), (group, port))
        except Exception: pass
        seq += 1
        time.sleep(interval)


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


def launch_scenario_6():
    header("Scenario MD-06 — Live Sequence Gap Detection (Real UDP)")
    print("  A live UDP multicast feed is running with packets SEQ 47-51 silently dropped.")
    print("  Listen, detect the gap, and understand the recovery path.\n")

    group, port = MCAST_PRIMARY
    # Drop seqs 47-51 to simulate packet loss
    pid = spawn(_live_feed, (group, port, "md_gap_live", list(range(47, 52)), None, 0.05),
                "md_gap_live")
    ok(f"Live gap feed on {group}:{port}  PID={pid}")
    warn("  SEQ 47-51 are silently dropped — gap will fire at seq 52")

    detector = DIRS["scripts"] / "live_gap_detector.py"
    detector.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-06: Listen to live UDP feed and detect sequence gaps in real time.\"\"\"
import socket, struct, time

GROUP, PORT = '{group}', {port}
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(('', PORT))
mreq = struct.pack('4sL', socket.inet_aton(GROUP), socket.INADDR_ANY)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
sock.settimeout(5.0)

print(f"Listening on {{GROUP}}:{{PORT}} — watching for gaps...")
expected, gaps = 1, []
try:
    while True:
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            print("Feed timed out"); break
        fields = dict(f.split('=', 1) for f in data.decode().split('|') if '=' in f)
        seq = int(fields.get('SEQ', 0))
        sym = fields.get('SYM', '?')
        px  = fields.get('PX', '?')
        if seq != expected:
            missing = list(range(expected, seq))
            gaps.append(missing)
            print(f"  *** GAP: expected {{expected}} got {{seq}} — missing {{missing}} ***")
            print(f"      → RetransmitRequest for SEQ {{expected}}-{{seq-1}}")
            expected = seq + 1
        else:
            print(f"  SEQ={{seq:>4}}  {{sym:<5}}  {{px}}")
            expected += 1
except KeyboardInterrupt:
    pass
print(f"\\nGaps found: {{len(gaps)}}")
for g in gaps:
    print(f"  Missing: {{g}}")
""")
    ok(f"Live detector: {detector}")

    print(f"""
{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Run the live gap detector — watch for the gap at SEQ 47-51
{CYAN}       python3 {detector}{RESET}

  2. Verify raw packets on the wire
{CYAN}       sudo tcpdump -i lo 'dst host {group}' -n -A | grep SEQ{RESET}

  3. Check receive buffer — small buffer = kernel drops before your app sees them
{CYAN}       cat /proc/sys/net/core/rmem_max
       sudo sysctl -w net.core.rmem_max=26214400{RESET}

{BOLD}── Key Interview Points ───────────────────────────────{RESET}
  • Gap on UDP = packets gone forever — YOU must detect and recover
  • RetransmitRequest sent on TCP channel (separate from the UDP feed)
  • Gap too large → request full snapshot, rebuild book from scratch
  • rmem_max too small → kernel silently drops bursts → false gaps
""")


def launch_scenario_7():
    header("Scenario MD-07 — Feed Arbitration (A/B Feed Failover)")
    print("  Primary A-feed dies after ~25s. Secondary B-feed runs continuously.")
    print("  Detect the failure and switch — this is standard production architecture.\n")

    g_a, p_a = MCAST_PRIMARY
    g_b, p_b = MCAST_SECONDARY

    pid_a = spawn(_live_feed, (g_a, p_a, "md_feed_a", [], 25, 0.05), "md_feed_a")
    ok(f"A-feed (PRIMARY)   {g_a}:{p_a}  PID={pid_a}")
    pid_b = spawn(_live_feed, (g_b, p_b, "md_feed_b", [], None, 0.05), "md_feed_b")
    ok(f"B-feed (SECONDARY) {g_b}:{p_b}  PID={pid_b}")
    warn("  A-feed goes silent in ~25 seconds — watch the arbitrator failover!")

    arb = DIRS["scripts"] / "feed_arbitrator.py"
    arb.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-07: Monitor A and B feeds — auto-failover when primary goes stale.\"\"\"
import socket, struct, threading, time

A_GROUP, A_PORT = '{g_a}', {p_a}
B_GROUP, B_PORT = '{g_b}', {p_b}
STALE_SEC = 3.0

last = {{'A': time.time(), 'B': time.time()}}
lock = threading.Lock()

def listen(group, port, label):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('', port))
    mreq = struct.pack('4sL', socket.inet_aton(group), socket.INADDR_ANY)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.settimeout(1.0)
    while True:
        try:
            s.recvfrom(4096)
            with lock: last[label] = time.time()
        except socket.timeout:
            pass

threading.Thread(target=listen, args=(A_GROUP, A_PORT, 'A'), daemon=True).start()
threading.Thread(target=listen, args=(B_GROUP, B_PORT, 'B'), daemon=True).start()

active = 'A'
print(f"Arbitrator running | stale threshold={{STALE_SEC}}s")
print(f"{{' TIME':>8}}  {{'ACTIVE':<8}}  {{'A-AGE':>8}}  {{'B-AGE':>8}}  STATUS")
print("-" * 50)

try:
    while True:
        now  = time.time()
        with lock:
            age_a = now - last['A']
            age_b = now - last['B']
        if active == 'A' and age_a > STALE_SEC:
            active = 'B'
            print(f"  *** FAILOVER → B-feed (A stale {{age_a:.1f}}s) ***")
        elif active == 'B' and age_a < STALE_SEC:
            active = 'A'
            print(f"  *** RECOVERY → A-feed restored ***")
        status = "OK" if (active=='A' and age_a<STALE_SEC) or (active=='B' and age_b<STALE_SEC) else "⚠ BOTH STALE"
        print(f"{{time.strftime('%H:%M:%S'):>8}}  {{active:<8}}  {{age_a:>7.1f}}s  {{age_b:>7.1f}}s  {{status}}")
        time.sleep(1)
except KeyboardInterrupt:
    print("\\nArbitrator stopped.")
""")
    ok(f"Arbitrator: {arb}")

    print(f"""
{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Run the arbitrator — watch the automatic failover at ~25s
{CYAN}       python3 {arb}{RESET}

  2. Monitor both feeds manually (two terminals)
{CYAN}       sudo tcpdump -i lo 'dst host {g_a}' -n | grep -oP 'SEQ=\\K[0-9]+'
       sudo tcpdump -i lo 'dst host {g_b}' -n | grep -oP 'SEQ=\\K[0-9]+'
{RESET}
{BOLD}── Key Interview Points ───────────────────────────────{RESET}
  • ALL major exchanges publish identical A-feed and B-feed on different groups
  • Your handler subscribes to BOTH simultaneously; arbitration picks the source
  • Deduplication: if B-feed seq ≤ last A-feed seq → discard (already processed)
  • Both stale → declare feed-down, page on-call immediately
  • CME MDP 3.0, NASDAQ ITCH, NYSE XDP all use A/B architecture
""")


def launch_scenario_8():
    header("Scenario MD-08 — Stale Feed Detection (Heartbeat Timeout)")
    print("  Feed sends ticks + 1-second heartbeats. Goes completely silent after ~20s.")
    print("  Detect staleness within the timeout window.\n")

    group, port = MCAST_PRIMARY
    pid = spawn(_heartbeat_feed, (group, port, "md_stale", 20, 1.0), "md_stale")
    ok(f"Heartbeat feed on {group}:{port}  PID={pid}")
    warn("  Feed goes silent in ~20 seconds — watch for the stale alert!")

    monitor = DIRS["scripts"] / "heartbeat_monitor.py"
    monitor.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-08: Alert when feed heartbeat exceeds stale threshold.\"\"\"
import socket, struct, time

GROUP, PORT     = '{group}', {port}
STALE_MS        = 3000

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(('', PORT))
mreq = struct.pack('4sL', socket.inet_aton(GROUP), socket.INADDR_ANY)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
sock.settimeout(0.5)

last_msg, alerted, count = time.time(), False, 0
print(f"Heartbeat monitor {{GROUP}}:{{PORT}} | stale threshold={{STALE_MS}}ms")

try:
    while True:
        try:
            data, _ = sock.recvfrom(4096)
            fields   = dict(f.split('=',1) for f in data.decode().split('|') if '=' in f)
            last_msg = time.time(); alerted = False; count += 1
            mtype    = fields.get('TYPE','TICK')
            label    = "HB  " if mtype=='HB' else f"TICK {fields.get('SYM','?')} @ {{fields.get('PX','?')}}"
            print(f"  [{{time.strftime('%H:%M:%S')}}] {{label}}  SEQ={{fields.get('SEQ','?')}}")
        except socket.timeout:
            pass
        age_ms = (time.time() - last_msg) * 1000
        if age_ms > STALE_MS and not alerted:
            print(f"\\n  ⚠ STALE FEED — no data for {{age_ms:.0f}}ms!")
            print(f"     Action: failover to B-feed, check sender PID, check IGMP membership")
            alerted = True
except KeyboardInterrupt:
    pass
print(f"\\nTotal messages: {{count}}")
""")
    ok(f"Heartbeat monitor: {monitor}")

    print(f"""
{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Run the monitor and wait for the stale alert (~20s)
{CYAN}       python3 {monitor}{RESET}

  2. After alert, check IGMP membership and NIC drops
{CYAN}       ip maddr show
       ip -s link show lo     # RX dropped > 0 ?{RESET}

  3. Check if sender process is still alive
{CYAN}       pgrep -la md_stale{RESET}

{BOLD}── Key Interview Points ───────────────────────────────{RESET}
  • Even in a quiet market the exchange sends heartbeats — silence = problem
  • Stale threshold is asset-class dependent: futures ~1s, equities ~3-5s
  • First action: failover to B-feed. THEN investigate cause.
  • IGMP snooping on switches can silently prune your multicast subscription
  • Check: NIC RX drops, sender process health, multicast routing (IGMP)
""")


def launch_scenario_9():
    header("Scenario MD-09 — Feed Latency Analysis (Exchange Timestamp)")
    print("  Every tick carries EXCHANGE_TS (microseconds). Measure transit latency.")
    print("  Identify spikes and understand their root causes.\n")

    group, port = MCAST_PRIMARY
    pid = spawn(_latency_feed, (group, port, "md_latency", 0.05), "md_latency")
    ok(f"Latency feed on {group}:{port}  PID={pid}")

    analyzer = DIRS["scripts"] / "latency_analyzer.py"
    analyzer.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-09: Measure per-tick feed latency from embedded exchange timestamps.\"\"\"
import socket, struct, time, statistics

GROUP, PORT = '{group}', {port}
SAMPLES     = 100
SPIKE_US    = 1000

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(('', PORT))
mreq = struct.pack('4sL', socket.inet_aton(GROUP), socket.INADDR_ANY)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
sock.settimeout(2.0)

print(f"Collecting {{SAMPLES}} samples | spike threshold={{SPIKE_US}}µs")
print(f"{{' SEQ':>6}}  {{'SYM':<5}}  {{'LAT µs':>8}}  STATUS")
print("-" * 35)

latencies, spikes = [], []
try:
    for _ in range(SAMPLES):
        data, _ = sock.recvfrom(4096)
        recv_ts  = int(time.time() * 1_000_000)
        fields   = dict(f.split('=',1) for f in data.decode().split('|') if '=' in f)
        exc_ts   = int(fields.get('EXCHANGE_TS', recv_ts))
        lat      = recv_ts - exc_ts
        latencies.append(lat)
        seq, sym = fields.get('SEQ','?'), fields.get('SYM','?')
        flag = "⚠ SPIKE" if lat > SPIKE_US else "OK"
        if lat > SPIKE_US: spikes.append((seq, lat))
        print(f"{{seq:>6}}  {{sym:<5}}  {{lat:>8}}  {{flag}}")
except (KeyboardInterrupt, socket.timeout):
    pass

if latencies:
    s = sorted(latencies)
    print(f"\\n── Latency Report ({{len(latencies)}} samples) ────────────")
    print(f"  Min : {{min(latencies):>8}}µs")
    print(f"  P50 : {{s[len(s)//2]:>8}}µs")
    print(f"  P95 : {{s[int(len(s)*.95)]:>8}}µs")
    print(f"  P99 : {{s[int(len(s)*.99)]:>8}}µs")
    print(f"  Max : {{max(latencies):>8}}µs")
    print(f"  Spikes >{{SPIKE_US}}µs: {{len(spikes)}}")
    print()
    print("  Spike causes to investigate:")
    print("    ethtool -c eth0        → NIC interrupt coalescing (rx-usecs > 0)")
    print("    sar -u 1 5             → CPU contention")
    print("    grep GC app.log        → JVM GC pause")
    print("    cyclictest             → OS scheduler jitter")
""")
    ok(f"Latency analyzer: {analyzer}")

    print(f"""
{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Run the analyzer and observe P50/P95/P99 distribution
{CYAN}       python3 {analyzer}{RESET}

  2. Check NIC coalescing (biggest tunable for sub-ms latency)
{CYAN}       ethtool -c eth0 2>/dev/null || echo "check: ip link"{RESET}

  3. Use awk to compute your own P99 from raw output
{CYAN}       python3 {analyzer} | grep -E '^[0-9]' | awk '{{print $3}}' | sort -n | tail -1{RESET}

{BOLD}── Key Interview Points ───────────────────────────────{RESET}
  • Always report P99, not mean — mean hides worst-case latency
  • NIC interrupt coalescing: batches IRQs → adds consistent delay
     Fix: ethtool -C eth0 rx-usecs 0
  • JVM GC: minor GC = 5-50ms pause → spikes in P99
  • OS scheduler: non-RT Linux can delay wakeup 1-4ms
  • Colocation (your servers IN the exchange DC) = best possible latency
  • PTP clock sync required for cross-host latency measurement accuracy
""")


def launch_scenario_10():
    header("Scenario MD-10 — NIC Buffer Tuning & Drop Detection")
    print("  High-throughput feeds overflow the NIC receive buffer.")
    print("  The kernel silently drops packets — you see gaps but no errors.\n")

    group, port = MCAST_PRIMARY
    # Fast feed — 200 msgs/sec to stress the receiver
    pid = spawn(_live_feed, (group, port, "md_burst", [], None, 0.005), "md_burst")
    ok(f"Burst feed on {group}:{port}  PID={pid}  (200 msg/s)")

    ref = DIRS["data"] / "nic_tuning_reference.txt"
    ref.write_text("""\
NIC BUFFER TUNING REFERENCE
=============================

WHY PACKETS GET DROPPED:
  UDP multicast: exchange → NIC DMA → kernel ring buffer → your app
  If your app is slow (or CPU is busy), kernel ring buffer fills up.
  Kernel drops the packet. No error to the exchange. No retransmit.
  You just see a gap in sequence numbers.

DIAGNOSE DROPS:
  # Check NIC drop counter
  ip -s link show eth0
  # Look for: RX: dropped > 0

  # ethtool stats (more detail)
  ethtool -S eth0 | grep -i "miss\\|drop\\|error"

  # Socket-level drops (per-socket)
  cat /proc/net/udp          # shows drops per UDP socket
  ss -u -s                   # UDP socket summary

  # Kernel ring buffer status
  cat /proc/net/softnet_stat  # column 2 = drops

FIXES:
  1. Increase receive buffer size (most impactful)
     sudo sysctl -w net.core.rmem_max=67108864      # 64MB max
     sudo sysctl -w net.core.rmem_default=26214400  # 25MB default

  2. Set per-socket buffer in your app
     sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 26214400)

  3. Increase kernel ring buffer (NIC level)
     ethtool -G eth0 rx 4096    # increase RX ring buffer entries

  4. Use dedicated CPU core for packet processing
     Bind IRQ affinity to a specific core (not the trading core)
     echo 2 > /proc/irq/<NIC_IRQ>/smp_affinity

  5. Use DPDK (bypass kernel entirely) — extreme low-latency only

VERIFY FIX:
  ip -s link show eth0    # dropped counter should stop growing
  Watch gap_detector output — gaps should disappear

RULE OF THUMB:
  rmem_max ≥ burst_rate_bytes_per_sec × latency_budget_sec
  Example: 100MB/s feed, 0.1s latency budget → rmem_max ≥ 10MB
""")
    ok(f"NIC tuning reference: {ref}")

    drop_checker = DIRS["scripts"] / "drop_checker.py"
    drop_checker.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-10: Receive the burst feed and check for sequence gaps (caused by drops).\"\"\"
import socket, struct, time

GROUP, PORT = '{group}', {port}

# Set large receive buffer to reduce drops
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 26214400)  # 25MB
sock.bind(('', PORT))
mreq = struct.pack('4sL', socket.inet_aton(GROUP), socket.INADDR_ANY)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
sock.settimeout(5.0)

print(f"Receiving burst feed on {{GROUP}}:{{PORT}} (SO_RCVBUF=25MB)")
print(f"Run without SO_RCVBUF to see more drops\\n")

received, gaps, expected = 0, 0, None
start = time.time()

try:
    while time.time() - start < 10:
        try:
            data, _ = sock.recvfrom(4096)
            fields   = dict(f.split('=',1) for f in data.decode().split('|') if '=' in f)
            seq = int(fields.get('SEQ', 0))
            if expected is None:
                expected = seq
            if seq != expected:
                dropped = seq - expected
                gaps   += dropped
                print(f"  GAP: {{dropped}} packets dropped (SEQ {{expected}}-{{seq-1}})")
                expected = seq + 1
            else:
                received += 1
                expected += 1
        except socket.timeout:
            break
except KeyboardInterrupt:
    pass

elapsed = time.time() - start
print(f"\\n── Results ({{elapsed:.1f}}s) ──────────────────────────────")
print(f"  Received : {{received}}")
print(f"  Dropped  : {{gaps}}")
print(f"  Drop rate: {{gaps/(received+gaps)*100 if received+gaps>0 else 0:.1f}}%")
print(f"  Rate     : {{received/elapsed:.0f}} msg/s")
print()
print("  Check NIC counters:")
print("    ip -s link show lo    → RX: dropped should be 0 with large buffer")
print("    cat /proc/net/softnet_stat  → column 2 = kernel drops")
""")
    ok(f"Drop checker: {drop_checker}")

    print(f"""
{BOLD}── Burst feed on {group}:{port} — 200 msg/s ─────────────{RESET}

{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Run the drop checker with the large socket buffer
{CYAN}       python3 {drop_checker}{RESET}

  2. Check current kernel buffer limits
{CYAN}       cat /proc/sys/net/core/rmem_max
       cat /proc/sys/net/core/rmem_default{RESET}

  3. Increase buffer and re-run
{CYAN}       sudo sysctl -w net.core.rmem_max=67108864
       python3 {drop_checker}{RESET}

  4. Check NIC-level drop counters
{CYAN}       ip -s link show lo
       cat /proc/net/softnet_stat{RESET}

  5. Read the full NIC tuning reference
{CYAN}       cat {ref}{RESET}

{BOLD}── Key Interview Points ───────────────────────────────{RESET}
  • Kernel drops are SILENT — no error message, just a gap in seq nums
  • rmem_max controls the max socket receive buffer the app can request
  • SO_RCVBUF must be set BEFORE bind() to take effect
  • NIC ring buffer (ethtool -G) is separate from socket buffer
  • Drop at NIC ring → kernel never sees packet → rmem won't help
  • Fix order: socket buffer → NIC ring → dedicated CPU core → DPDK
""")


def launch_scenario_11():
    header("Scenario MD-11 — Book Invalidation on Sequence Gap")
    print("  When a gap is detected the book state is UNKNOWN.")
    print("  Trading must halt on that symbol until the book is rebuilt.\n")

    from datetime import timedelta
    now = datetime.now()
    def ts(s): return (now + timedelta(seconds=s)).strftime("%H:%M:%S.%f")[:-3]

    # Stream with a deliberate gap at seq 8-10
    updates = [
        f"SEQ=1|TS={ts(0)}|SYM=AAPL|TYPE=ADD|SIDE=BUY|PX=184.90|QTY=500|OID=O001",
        f"SEQ=2|TS={ts(0)}|SYM=AAPL|TYPE=ADD|SIDE=BUY|PX=184.85|QTY=300|OID=O002",
        f"SEQ=3|TS={ts(0)}|SYM=AAPL|TYPE=ADD|SIDE=SELL|PX=185.10|QTY=400|OID=O003",
        f"SEQ=4|TS={ts(0)}|SYM=AAPL|TYPE=ADD|SIDE=SELL|PX=185.15|QTY=600|OID=O004",
        f"SEQ=5|TS={ts(1)}|SYM=AAPL|TYPE=MODIFY|SIDE=BUY|PX=184.90|QTY=250|OID=O001",
        f"SEQ=6|TS={ts(1)}|SYM=AAPL|TYPE=ADD|SIDE=BUY|PX=184.95|QTY=800|OID=O005",
        f"SEQ=7|TS={ts(2)}|SYM=AAPL|TYPE=EXECUTE|SIDE=SELL|PX=185.10|QTY=200|OID=O003",
        # GAP: SEQ 8, 9, 10 missing — book is now UNKNOWN
        f"SEQ=11|TS={ts(3)}|SYM=AAPL|TYPE=ADD|SIDE=BUY|PX=184.80|QTY=1000|OID=O006",
        f"SEQ=12|TS={ts(3)}|SYM=AAPL|TYPE=DELETE|SIDE=SELL|PX=185.15|QTY=0|OID=O004",
        f"SEQ=13|TS={ts(4)}|SYM=AAPL|TYPE=ADD|SIDE=SELL|PX=185.05|QTY=700|OID=O007",
    ]
    log = DIRS["data"] / "gap_book_updates.log"
    log.write_text("\n".join(updates) + "\n")
    ok(f"Book update log (gap at SEQ 8-10): {log}")

    script = DIRS["scripts"] / "book_with_invalidation.py"
    script.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-11: Build L2 book; mark STALE on gap; show why trading must stop.\"\"\"
from collections import defaultdict

LOG = '{log}'
LEVELS = 5

book  = defaultdict(lambda: {{'BUY': {{}}, 'SELL': {{}}}})
state = {{}}   # 'FRESH' or 'STALE' per symbol

def print_book(sym, note=""):
    status = state.get(sym, 'FRESH')
    bids = sorted(book[sym]['BUY'].items(),  reverse=True)[:LEVELS]
    asks = sorted(book[sym]['SELL'].items())[:LEVELS]
    color = "\\033[31m" if status == 'STALE' else "\\033[32m"
    reset = "\\033[0m"
    print(f"\\n  ── {{sym}} Book  [{{color}}{{status}}{{reset}}]  {{note}}")
    print(f"  {{' ASK PX':>10}}  {{' ASK QTY':>9}}")
    for px, qty in reversed(asks):
        print(f"  {{px:>10.2f}}  {{qty:>9}}")
    spread = round(asks[0][0] - bids[0][0], 4) if bids and asks else "N/A"
    print(f"  {{'─── spread: ' + str(spread) + ' ───':^23}}")
    for px, qty in bids:
        print(f"  {{px:>10.2f}}  {{qty:>9}}")
    print(f"  {{' BID PX':>10}}  {{' BID QTY':>9}}")
    if status == 'STALE':
        print(f"  ⚠  Book is STALE — do NOT use for trading decisions!")
        print(f"     Action: send RetransmitRequest or request Snapshot")

expected = 1
with open(LOG) as f:
    for line in f:
        fields = dict(p.split('=',1) for p in line.strip().split('|') if '=' in p)
        seq  = int(fields['SEQ'])
        sym  = fields['SYM']
        mtype = fields['TYPE']
        side  = fields['SIDE']
        px    = float(fields['PX'])
        qty   = int(fields['QTY'])

        if seq != expected:
            state[sym] = 'STALE'
            print(f"\\n  *** GAP DETECTED: expected SEQ={{expected}} got {{seq}} ***")
            print(f"      Marking {{sym}} book as STALE — applying remaining updates anyway")
            print(f"      In production: halt trading on {{sym}} immediately")
            expected = seq + 1
        else:
            expected += 1

        if mtype == 'ADD':
            book[sym][side][px] = book[sym][side].get(px, 0) + qty
        elif mtype == 'MODIFY':
            book[sym][side][px] = qty
        elif mtype in ('DELETE', 'EXECUTE'):
            book[sym][side][px] = max(0, book[sym][side].get(px, 0) - qty)
            if book[sym][side].get(px, 0) == 0:
                book[sym][side].pop(px, None)

        if seq in (7, 11, 13):
            print_book(sym, f"after SEQ={{seq}} {{mtype}}")

print(f\"\"\"
── Why Book Invalidation Matters ─────────────────────────────────
  After SEQ 7: book is FRESH — best bid 184.95, best ask 185.10
  SEQ 8-10 are MISSING — we don't know what happened in those msgs:
    Could be: large EXECUTE that moved the book significantly
    Could be: DELETE of the best bid or ask
    Could be: new ADD that crossed the book

  If we trade on the stale book:
    Risk engine uses wrong position → limit breach
    Order router sends to wrong price → bad fill
    Compliance sees trading on bad data → regulatory issue

  Recovery steps:
    1. Detect gap (seq jump)
    2. Mark book STALE, halt trading on that symbol
    3. Send RetransmitRequest for SEQ 8-10 (TCP recovery channel)
    4. If no reply in 5s → request full Snapshot from exchange
    5. Apply Snapshot, then replay incrementals from snapshot seq
    6. Mark book FRESH, resume trading
\"\"\")
""")
    ok(f"Book invalidation script: {script}")

    print(f"""
{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Read the raw update log — spot the gap
{CYAN}       cat {log}{RESET}

  2. Run the book builder — watch it detect the gap and go STALE
{CYAN}       python3 {script}{RESET}

  3. Use awk to extract just the sequence numbers
{CYAN}       awk -F'|' '{{print $1}}' {log}{RESET}

  4. Find the gap with awk
{CYAN}       awk -F'|' '{{n=substr($1,5)+0; if(n!=prev+1 && prev>0) print "GAP: "prev+1" to "n-1; prev=n}}' {log}{RESET}

{BOLD}── Key Interview Points ───────────────────────────────{RESET}
  • Gap = book state unknown = must halt trading on affected symbol
  • Never trade on a stale book — bad fills, limit breaches, regulatory risk
  • Invalidation is per-symbol — a gap on AAPL doesn't affect MSFT
  • Snapshot + incremental is the standard recovery pattern
""")


def launch_scenario_12():
    header("Scenario MD-12 — Snapshot + Incremental Recovery")
    print("  After a gap the exchange sends a full book Snapshot.")
    print("  Apply the snapshot, then replay incrementals to reach current state.\n")

    from datetime import timedelta
    now = datetime.now()
    def ts(s): return (now + timedelta(seconds=s)).strftime("%H:%M:%S.%f")[:-3]

    # Snapshot at SEQ=50 — full book state
    snapshot = {
        "seq":    50,
        "symbol": "AAPL",
        "bids":   {184.95: 1200, 184.90: 800, 184.85: 500, 184.80: 300},
        "asks":   {185.05: 700,  185.10: 400, 185.15: 900, 185.20: 200},
    }
    snap_file = DIRS["data"] / "book_snapshot.json"
    import json
    snap_file.write_text(json.dumps(snapshot, indent=2))
    ok(f"Snapshot file (at SEQ=50): {snap_file}")

    # Incremental updates AFTER the snapshot
    incrementals = [
        f"SEQ=51|TS={ts(0)}|SYM=AAPL|TYPE=ADD|SIDE=BUY|PX=185.00|QTY=600|OID=O100",
        f"SEQ=52|TS={ts(0)}|SYM=AAPL|TYPE=EXECUTE|SIDE=SELL|PX=185.05|QTY=300|OID=O101",
        f"SEQ=53|TS={ts(1)}|SYM=AAPL|TYPE=DELETE|SIDE=SELL|PX=185.05|QTY=0|OID=O101",
        f"SEQ=54|TS={ts(1)}|SYM=AAPL|TYPE=MODIFY|SIDE=BUY|PX=184.95|QTY=900|OID=O102",
        f"SEQ=55|TS={ts(2)}|SYM=AAPL|TYPE=ADD|SIDE=SELL|PX=185.02|QTY=500|OID=O103",
        f"SEQ=56|TS={ts(2)}|SYM=AAPL|TYPE=ADD|SIDE=BUY|PX=185.01|QTY=800|OID=O104",
    ]
    inc_file = DIRS["data"] / "book_incrementals.log"
    inc_file.write_text("\n".join(incrementals) + "\n")
    ok(f"Incremental updates (SEQ 51-56): {inc_file}")

    script = DIRS["scripts"] / "snapshot_recovery.py"
    script.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-12: Rebuild order book from snapshot then apply incrementals.\"\"\"
import json

SNAP_FILE = '{snap_file}'
INC_FILE  = '{inc_file}'
LEVELS    = 5

def print_book(bids, asks, title):
    top_bids = sorted(bids.items(), reverse=True)[:LEVELS]
    top_asks = sorted(asks.items())[:LEVELS]
    spread   = round(top_asks[0][0] - top_bids[0][0], 4) if top_bids and top_asks else "N/A"
    print(f"\\n  ── {{title}} (spread={{spread}}) ──────────────────")
    print(f"  {{' ASK PX':>10}}  {{'ASK QTY':>9}}")
    for px, qty in reversed(top_asks):
        print(f"  {{px:>10.2f}}  {{qty:>9}}")
    print(f"  {{'─── spread: ' + str(spread) + ' ───':^25}}")
    for px, qty in top_bids:
        print(f"  {{px:>10.2f}}  {{qty:>9}}")
    print(f"  {{' BID PX':>10}}  {{'BID QTY':>9}}")

# ── Step 1: Load Snapshot ──────────────────────────────
with open(SNAP_FILE) as f:
    snap = json.load(f)

bids = {{float(k): v for k, v in snap['bids'].items()}}
asks = {{float(k): v for k, v in snap['asks'].items()}}
seq  = snap['seq']
sym  = snap['symbol']

print(f"Step 1: Loaded snapshot for {{sym}} at SEQ={{seq}}")
print_book(bids, asks, f"Snapshot State (SEQ={{seq}})")

# ── Step 2: Apply Incrementals ────────────────────────
print(f"\\nStep 2: Applying incrementals from SEQ {{seq+1}}...")
with open(INC_FILE) as f:
    for line in f:
        fields = dict(p.split('=',1) for p in line.strip().split('|') if '=' in p)
        inc_seq = int(fields['SEQ'])
        if inc_seq <= seq:
            print(f"  SKIP SEQ={{inc_seq}} — already in snapshot")
            continue
        mtype = fields['TYPE']
        side  = fields['SIDE']
        px    = float(fields['PX'])
        qty   = int(fields['QTY'])
        book  = bids if side == 'BUY' else asks
        if mtype == 'ADD':
            book[px] = book.get(px, 0) + qty
        elif mtype == 'MODIFY':
            book[px] = qty
        elif mtype in ('DELETE', 'EXECUTE'):
            book[px] = max(0, book.get(px, 0) - qty)
            if book.get(px, 0) == 0:
                book.pop(px, None)
        print(f"  Applied SEQ={{inc_seq}} {{mtype:<8}} {{side:<5}} {{px:.2f}} qty={{qty}}")

print_book(bids, asks, "Final State (after snapshot + incrementals)")

print(\"\"\"
── Snapshot + Incremental Protocol ──────────────────────────────
  1. Gap detected at SEQ N → book STALE, trading halted
  2. Request Snapshot from exchange
     Exchange sends: full book state at SEQ=M (M >= N)
  3. Apply snapshot → book now FRESH at SEQ=M
  4. Buffer incrementals received DURING snapshot delivery (SEQ > M)
  5. Apply buffered incrementals in order
  6. Book is now current → trading resumes

  Key: DO NOT apply incrementals with SEQ <= snapshot SEQ
  They are already included in the snapshot state.

  This is called "Snapshot + Incremental" or "S+I" recovery.
  Used by: CME MDP 3.0, NASDAQ ITCH, NYSE XDP
\"\"\")
""")
    ok(f"Snapshot recovery script: {script}")

    print(f"""
{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Inspect the snapshot
{CYAN}       cat {snap_file}{RESET}

  2. Inspect the incremental updates
{CYAN}       cat {inc_file}{RESET}

  3. Run the recovery and watch the book rebuild
{CYAN}       python3 {script}{RESET}

  4. Use awk to verify all incrementals are after the snapshot seq
{CYAN}       awk -F'|' '{{print substr($1,5)}}' {inc_file}{RESET}

{BOLD}── Key Interview Points ───────────────────────────────{RESET}
  • Snapshot = full book state at a given sequence number
  • Incrementals with SEQ ≤ snapshot SEQ must be discarded (already included)
  • Buffer incrementals WHILE waiting for snapshot — don't miss any
  • Gap recovery is not instant — expect 100ms to 2s depending on exchange
  • Some exchanges offer: A) replay channel B) snapshot channel C) both
""")


def launch_scenario_13():
    header("Scenario MD-13 — Crossed Book Detection")
    print("  A crossed book (best bid ≥ best ask) means the data is bad.")
    print("  Detect it, alert, and mark the book invalid.\n")

    from datetime import timedelta
    now = datetime.now()
    def ts(s): return (now + timedelta(seconds=s)).strftime("%H:%M:%S.%f")[:-3]

    # Stream that produces a crossed book
    updates = [
        f"SEQ=1|TS={ts(0)}|SYM=AAPL|TYPE=ADD|SIDE=BUY|PX=184.90|QTY=500|OID=O001",
        f"SEQ=2|TS={ts(0)}|SYM=AAPL|TYPE=ADD|SIDE=SELL|PX=185.10|QTY=400|OID=O002",
        f"SEQ=3|TS={ts(1)}|SYM=AAPL|TYPE=ADD|SIDE=BUY|PX=185.00|QTY=300|OID=O003",
        # Normal — spread = 185.10 - 185.00 = 0.10
        f"SEQ=4|TS={ts(2)}|SYM=AAPL|TYPE=ADD|SIDE=BUY|PX=185.15|QTY=200|OID=O004",
        # CROSSED — best bid 185.15 > best ask 185.10 → BAD DATA
        f"SEQ=5|TS={ts(2)}|SYM=AAPL|TYPE=ADD|SIDE=SELL|PX=185.20|QTY=600|OID=O005",
        f"SEQ=6|TS={ts(3)}|SYM=AAPL|TYPE=DELETE|SIDE=BUY|PX=185.15|QTY=0|OID=O004",
        # Uncrossed after delete — book recovers
        f"SEQ=7|TS={ts(3)}|SYM=AAPL|TYPE=ADD|SIDE=BUY|PX=184.95|QTY=400|OID=O006",
    ]
    log = DIRS["data"] / "crossed_book_updates.log"
    log.write_text("\n".join(updates) + "\n")
    ok(f"Crossed book update log: {log}")

    script = DIRS["scripts"] / "crossed_book_detector.py"
    script.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-13: Build L2 book and detect when it crosses (bid >= ask).\"\"\"

LOG    = '{log}'
LEVELS = 4

book = {{'BUY': {{}}, 'SELL': {{}}}}

def best_bid(): return max(book['BUY'].keys())  if book['BUY']  else None
def best_ask(): return min(book['SELL'].keys()) if book['SELL'] else None

def check_cross(seq):
    bb, ba = best_bid(), best_ask()
    if bb is None or ba is None:
        return
    spread = round(ba - bb, 4)
    if bb >= ba:
        print(f"  *** CROSSED BOOK at SEQ={{seq}}: bid={{bb}} >= ask={{ba}} spread={{spread}} ***")
        print(f"      → Mark book INVALID, alert market data team")
        print(f"      → Do NOT use this book for pricing or routing")
        print(f"      → Likely cause: bad tick, feed error, or delayed DELETE")
    else:
        print(f"  Book OK: bid={{bb:.2f}} ask={{ba:.2f}} spread={{spread:.4f}}")

with open(LOG) as f:
    for line in f:
        fields = dict(p.split('=',1) for p in line.strip().split('|') if '=' in p)
        seq   = fields['SEQ']
        mtype = fields['TYPE']
        side  = fields['SIDE']
        px    = float(fields['PX'])
        qty   = int(fields['QTY'])
        print(f"  SEQ={{seq:>2}}  {{mtype:<8}} {{side:<5}} {{px:.2f}}  qty={{qty}}")
        if mtype == 'ADD':
            book[side][px] = book[side].get(px,0) + qty
        elif mtype == 'MODIFY':
            book[side][px] = qty
        elif mtype in ('DELETE', 'EXECUTE'):
            book[side][px] = max(0, book[side].get(px,0) - qty)
            if book[side].get(px,0) == 0: book[side].pop(px,None)
        check_cross(seq)

print(\"\"\"
── What Causes a Crossed Book? ──────────────────────────────────
  1. Feed error — bad tick with wrong price field
  2. Out-of-order messages — DELETE arrives after a new ADD at a better price
  3. Sequence gap — missed DELETE left a stale resting order in the book
  4. Exchange matching engine bug (rare)

  In a real exchange matching engine a crossed book triggers immediate
  execution — it cannot persist. If your book is crossed, it's bad data.

── Actions ─────────────────────────────────────────────────────
  1. Alert market data operations team immediately
  2. Mark book INVALID — halt pricing and routing for that symbol
  3. Request snapshot to get clean book state
  4. Log the event with the sequence number for post-trade review
  5. Check if other symbols or feeds have the same issue
\"\"\")
""")
    ok(f"Crossed book detector: {script}")

    print(f"""
{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Run the crossed book detector — watch it fire at SEQ 4
{CYAN}       python3 {script}{RESET}

  2. Use awk to find the first moment best bid exceeds best ask
{CYAN}       cat {log}{RESET}

{BOLD}── Key Interview Points ───────────────────────────────{RESET}
  • A crossed book (bid ≥ ask) NEVER happens legitimately — it's always bad data
  • Crossed = missed DELETE + new ADD, or bad tick
  • Response: invalidate book immediately, do not trade on crossed data
  • Locked book (bid == ask) = unusual but can occur briefly during fast markets
""")


def launch_scenario_14():
    header("Scenario MD-14 — Market Impact & Walking the Book")
    print("  A large order consumes multiple price levels.")
    print("  Calculate average fill price and market impact.\n")

    import json as _json
    book_file = DIRS["data"] / "deep_book.json"
    book = {
        "symbol": "AAPL",
        "bids": {
            "184.95": 1000, "184.90": 2500, "184.85": 1500,
            "184.80": 3000, "184.75": 5000, "184.70": 8000,
        },
        "asks": {
            "185.00": 800,  "185.05": 1200, "185.10": 2000,
            "185.15": 1500, "185.20": 3500, "185.25": 6000,
        },
    }
    book_file.write_text(_json.dumps(book, indent=2))
    ok(f"Deep L2 book: {book_file}")

    script = DIRS["scripts"] / "market_impact.py"
    script.write_text(f"""\
#!/usr/bin/env python3
\"\"\"MD-14: Walk the order book with a large BUY order — show market impact.\"\"\"
import json

BOOK_FILE  = '{book_file}'
ORDER_QTY  = 4500   # shares to BUY — this will walk through multiple levels

with open(BOOK_FILE) as f:
    book = json.load(f)

asks = sorted((float(px), int(qty)) for px, qty in book['asks'].items())
bids = sorted((float(px), int(qty)) for px, qty in book['bids'].items(), reverse=True)

sym        = book['symbol']
best_bid   = bids[0][0]
best_ask   = asks[0][0]
mid        = (best_bid + best_ask) / 2

print(f"── {{sym}} Order Book ──────────────────────────────────")
print(f"  Best bid: {{best_bid:.2f}}  Best ask: {{best_ask:.2f}}  Mid: {{mid:.4f}}")
print(f"  Spread  : {{best_ask - best_bid:.4f}}")
print()
print(f"  {{' ASK PX':>10}}  {{'QTY':>8}}  {{'CUM QTY':>9}}")
cum = 0
for px, qty in asks:
    cum += qty
    print(f"  {{px:>10.2f}}  {{qty:>8}}  {{cum:>9}}")

print(f"\\n── Walking book with BUY order for {{ORDER_QTY:,}} shares ──────")
remaining    = ORDER_QTY
filled_qty   = 0
total_cost   = 0.0
levels_hit   = 0
fills        = []

for px, available in asks:
    if remaining <= 0:
        break
    take   = min(remaining, available)
    cost   = take * px
    fills.append((px, take, cost))
    filled_qty  += take
    total_cost  += cost
    remaining   -= take
    levels_hit  += 1
    print(f"  Level {{levels_hit}}: fill {{take:>6}} @ {{px:.2f}}  (cost={{cost:>10,.2f}})  remaining={{remaining}}")

avg_fill_px  = total_cost / filled_qty if filled_qty else 0
slippage_bps = ((avg_fill_px - best_ask) / best_ask) * 10000
impact_bps   = ((avg_fill_px - mid) / mid) * 10000

print(f"\\n── Fill Summary ──────────────────────────────────────")
print(f"  Order qty    : {{ORDER_QTY:,}}")
print(f"  Filled qty   : {{filled_qty:,}}")
print(f"  Unfilled     : {{remaining:,}}")
print(f"  Best ask     : {{best_ask:.4f}}")
print(f"  Avg fill px  : {{avg_fill_px:.4f}}")
print(f"  Total cost   : ${{total_cost:,.2f}}")
print(f"  Slippage     : {{slippage_bps:.2f}} bps  (vs best ask)")
print(f"  Market impact: {{impact_bps:.2f}} bps  (vs mid)")
print(f"  Levels hit   : {{levels_hit}}")

print(f\"\"\"
── Concepts ──────────────────────────────────────────────────────
  Slippage     : avg fill price - best ask (cost of size vs top of book)
  Market impact: avg fill price - mid (total deviation from fair value)
  Basis points : 1 bps = 0.01% = price / 10,000

  Why this matters for a support engineer:
  - Execution algos (VWAP, TWAP, POV) try to minimize market impact
  - If book depth data is wrong (stale/gapped) → algo miscalculates impact
  - Wrong impact calc → algo sends too large an order → large slippage
  - Your job: ensure the book fed to the algo is always FRESH and correct

  VWAP  : execute evenly over a time window, target the daily VWAP price
  TWAP  : execute equal qty every time slice (ignores volume)
  POV   : participate at X% of market volume (book-depth dependent)
  IS    : implementation shortfall — minimize deviation from arrival price
\"\"\")
""")
    ok(f"Market impact script: {script}")

    print(f"""
{BOLD}── Your Tasks ─────────────────────────────────────────{RESET}
  1. Inspect the deep book
{CYAN}       cat {book_file}{RESET}

  2. Run the market impact simulation
{CYAN}       python3 {script}{RESET}

  3. Use awk on the book file to compute total ask liquidity
{CYAN}       python3 -c "import json; b=json.load(open('{book_file}')); \\
           print(sum(int(v) for v in b['asks'].values()), 'shares on ask side')"{RESET}

  4. Modify the order size in the script and re-run
{CYAN}       # Change ORDER_QTY = 4500 to 10000 — see how many levels it walks{RESET}

{BOLD}── Key Interview Points ───────────────────────────────{RESET}
  • Market impact increases non-linearly with order size
  • Stale book → algo underestimates depth → sends order too fast → more impact
  • L2 book is what execution algos use; L3 gives per-order detail (rarer)
  • Spread = bid-ask gap = transaction cost for aggressive orders
  • Large orders need to be split across time (algo) or venues (SOR)
""")


def launch_scenario_99():
    header("Scenario 99 — ALL Market Data Scenarios")
    for fn in [launch_scenario_1, launch_scenario_2, launch_scenario_3,
               launch_scenario_4, launch_scenario_5, launch_scenario_6,
               launch_scenario_7, launch_scenario_8, launch_scenario_9,
               launch_scenario_10, launch_scenario_11, launch_scenario_12,
               launch_scenario_13, launch_scenario_14]:
        fn()
        time.sleep(0.2)


def teardown():
    header("Tearing Down Market Data Lab")
    kill_pids(DIRS["pids"])
    kill_strays(["md_gap_live", "md_feed_a", "md_feed_b",
                 "md_stale", "md_latency", "md_burst"])
    remove_lab_dir(LAB_ROOT)


SCENARIO_MAP = {
    1:  (launch_scenario_1,  "MD-01  Order book L1/L2/L3"),
    2:  (launch_scenario_2,  "MD-02  ITCH protocol decode"),
    3:  (launch_scenario_3,  "MD-03  HDF5 tick data"),
    4:  (launch_scenario_4,  "MD-04  Feed handler gap detection (file)"),
    5:  (launch_scenario_5,  "MD-05  SBE binary protocol"),
    6:  (launch_scenario_6,  "MD-06  Live sequence gap detection (real UDP)"),
    7:  (launch_scenario_7,  "MD-07  Feed arbitration — A/B failover"),
    8:  (launch_scenario_8,  "MD-08  Stale feed detection — heartbeat timeout"),
    9:  (launch_scenario_9,  "MD-09  Feed latency analysis — exchange timestamp"),
    10: (launch_scenario_10, "MD-10  NIC buffer tuning & drop detection"),
    11: (launch_scenario_11, "MD-11  Book invalidation on sequence gap"),
    12: (launch_scenario_12, "MD-12  Snapshot + incremental recovery"),
    13: (launch_scenario_13, "MD-13  Crossed book detection"),
    14: (launch_scenario_14, "MD-14  Market impact & walking the book"),
    99: (launch_scenario_99, "      ALL scenarios"),
}


def main():
    parser = argparse.ArgumentParser(description="Market Data & Protocols Challenge Lab",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(f"  {k:<4} {v}" for k, (_, v) in SCENARIO_MAP.items()))
    parser.add_argument("--scenario", "-s", type=int, choices=list(SCENARIO_MAP.keys()),
                        metavar="{1-10,99}")
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
