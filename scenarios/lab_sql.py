#!/usr/bin/env python3
"""
Support Engineer Challenge Lab — SQL Setup
==========================================
Creates a SQLite trading database with realistic data for all SQL scenarios (S1-S8).

Run with: python3 lab_sql.py [--scenario N] [--teardown]

SCENARIOS:
  1   S-01  Find all fills for a trader
  2   S-02  Find rejected orders and their traders
  3   S-03  Calculate net position per trader per symbol
  4   S-04  Top traders by volume — window functions
  5   S-05  Slow query investigation — EXPLAIN + index creation
  6   S-06  Deadlock / locking scenario — concurrent writes + advisory notes
  7   S-07  Market data gap investigation — find missing symbols and sequence gaps
  8   S-08  Feed completeness check — compare primary vs secondary feed coverage
  99        ALL (creates full DB + all scripts)
"""

import os, sys, time, signal, shutil, sqlite3, argparse, subprocess
from pathlib import Path
from datetime import datetime, timedelta
import random

LAB_ROOT = Path("/tmp/lab_sql")
DIRS = {
    "db":      LAB_ROOT / "db",
    "scripts": LAB_ROOT / "scripts",
    "logs":    LAB_ROOT / "logs",
    "pids":    LAB_ROOT / "run",
}

DB_PATH = None   # set in create_dirs()

from common import (
    GREEN, YELLOW, RED, CYAN, BOLD, RESET, SEP,
    ok, warn, err, header, lab_footer,
    create_dirs as _create_dirs,
)
SEPARATOR = SEP
DB_PATH = DIRS["db"] / "trading.db"

def create_dirs():
    _create_dirs(DIRS)


# ══════════════════════════════════════════════
#  DATABASE SETUP
# ══════════════════════════════════════════════

def setup_database():
    conn = sqlite3.connect(str(DB_PATH))
    c    = conn.cursor()

    c.executescript("""
        DROP TABLE IF EXISTS traders;
        DROP TABLE IF EXISTS trades;
        DROP TABLE IF EXISTS positions;
        DROP TABLE IF EXISTS orders_audit;

        CREATE TABLE traders (
            trader_id   TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            desk        TEXT NOT NULL,
            email       TEXT,
            joined_date TEXT
        );

        CREATE TABLE trades (
            trade_id    INTEGER PRIMARY KEY,
            trader_id   TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            side        TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
            quantity    INTEGER NOT NULL,
            price       REAL NOT NULL,
            trade_time  TEXT NOT NULL,
            status      TEXT NOT NULL CHECK(status IN ('FILLED','REJECTED','PARTIAL','PENDING')),
            venue       TEXT,
            FOREIGN KEY (trader_id) REFERENCES traders(trader_id)
        );

        CREATE TABLE positions (
            trader_id   TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            net_qty     INTEGER NOT NULL DEFAULT 0,
            avg_price   REAL,
            updated_at  TEXT,
            PRIMARY KEY (trader_id, symbol),
            FOREIGN KEY (trader_id) REFERENCES traders(trader_id)
        );

        CREATE TABLE orders_audit (
            audit_id    INTEGER PRIMARY KEY,
            trade_id    INTEGER,
            action      TEXT,
            action_time TEXT,
            actor       TEXT,
            notes       TEXT
        );

        DROP TABLE IF EXISTS tick_data;
        CREATE TABLE tick_data (
            id       INTEGER PRIMARY KEY,
            symbol   TEXT    NOT NULL,
            ts       TEXT    NOT NULL,
            price    REAL    NOT NULL,
            volume   INTEGER NOT NULL,
            seq_num  INTEGER NOT NULL,
            feed     TEXT    NOT NULL CHECK(feed IN ('PRIMARY','SECONDARY'))
        );
        CREATE INDEX idx_tick_symbol_ts   ON tick_data(symbol, ts);
        CREATE INDEX idx_tick_feed_symbol ON tick_data(feed, symbol, ts);
    """)

    # Traders
    c.executemany("INSERT INTO traders VALUES (?,?,?,?,?)", [
        ('T001', 'Alice Chen',   'Equities',     'alice@firm.com',  '2021-03-15'),
        ('T002', 'Bob Singh',    'Derivatives',  'bob@firm.com',    '2020-07-22'),
        ('T003', 'Carol Wu',     'Equities',     'carol@firm.com',  '2022-01-10'),
        ('T004', 'Dave Kim',     'FX',           'dave@firm.com',   '2019-11-05'),
        ('T005', 'Emma Torres',  'Derivatives',  'emma@firm.com',   '2023-06-01'),
    ])

    # Trades
    c.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)", [
        (1,  'T001','AAPL', 'BUY', 100,  185.50, '2024-01-15 09:30:01', 'FILLED',   'NYSE'),
        (2,  'T001','AAPL', 'BUY', 200,  185.55, '2024-01-15 09:30:45', 'FILLED',   'NYSE'),
        (3,  'T001','AAPL', 'SELL',150,  186.00, '2024-01-15 10:15:22', 'FILLED',   'NYSE'),
        (4,  'T002','TSLA', 'BUY',  50,  248.00, '2024-01-15 09:31:00', 'FILLED',   'NASDAQ'),
        (5,  'T002','TSLA', 'BUY',5000,  249.00, '2024-01-15 09:32:00', 'REJECTED', 'NASDAQ'),
        (6,  'T003','GOOGL','SELL', 75,  141.20, '2024-01-15 09:45:00', 'FILLED',   'NASDAQ'),
        (7,  'T003','MSFT', 'BUY', 120,  380.10, '2024-01-15 10:00:00', 'FILLED',   'NASDAQ'),
        (8,  'T001','AAPL', 'BUY', 300,  185.20, '2024-01-15 11:00:00', 'FILLED',   'NYSE'),
        (9,  'T004','EURUSD','BUY',1000000,1.0850,'2024-01-15 09:30:00','FILLED',   'FX'),
        (10, 'T002','TSLA', 'SELL', 50,  251.00, '2024-01-15 14:00:00', 'FILLED',   'NASDAQ'),
        (11, 'T001','NVDA', 'BUY',  60,  495.00, '2024-01-15 09:35:00', 'FILLED',   'NASDAQ'),
        (12, 'T003','AAPL', 'BUY',  80,  185.80, '2024-01-15 09:50:00', 'PARTIAL',  'NYSE'),
        (13, 'T005','SPY',  'BUY', 500,  477.20, '2024-01-15 09:30:00', 'FILLED',   'NYSE'),
        (14, 'T005','QQQ',  'SELL',200,  403.50, '2024-01-15 10:30:00', 'FILLED',   'NASDAQ'),
        (15, 'T002','TSLA', 'BUY', 100,  250.00, '2024-01-15 15:00:00', 'REJECTED', 'NASDAQ'),
    ])

    # Positions (deliberately has one discrepancy for scenario 3)
    c.executemany("INSERT INTO positions VALUES (?,?,?,?,?)", [
        ('T001','AAPL',  450,  185.45, '2024-01-15 11:00:00'),  # matches trades
        ('T001','NVDA',   60,  495.00, '2024-01-15 09:35:00'),
        ('T002','TSLA',    0,    0.00, '2024-01-15 14:00:00'),
        ('T003','GOOGL', -75,  141.20, '2024-01-15 09:45:00'),
        ('T003','MSFT',  120,  380.10, '2024-01-15 10:00:00'),
        ('T004','EURUSD',1000000,1.0850,'2024-01-15 09:30:00'),
        # T003 AAPL position is MISSING from positions table — discrepancy!
        # T005 positions not in table at all — discrepancy!
    ])

    # Audit log
    c.executemany("INSERT INTO orders_audit VALUES (?,?,?,?,?,?)", [
        (1, 5,  'REVIEW',  '2024-01-15 09:32:05', 'risk-system', 'Margin check triggered'),
        (2, 5,  'REJECT',  '2024-01-15 09:32:06', 'risk-system', 'Insufficient margin: required $1.24M, available $0.5M'),
        (3, 15, 'REVIEW',  '2024-01-15 15:00:05', 'risk-system', 'Position limit check'),
        (4, 15, 'REJECT',  '2024-01-15 15:00:06', 'risk-system', 'Would exceed max position 150 contracts'),
    ])

    # ── tick_data: market data for S-07 (gap investigation) and S-08 (feed comparison)
    # PRIMARY feed:   AAPL full day | MSFT full day | TSLA full day | NVDA MISSING 14:30-15:00
    # SECONDARY feed: AAPL full day | MSFT MISSING 14:30-15:00 | TSLA seq gap at noon | NVDA full day
    # GOOGL: seq gap on PRIMARY (seqs jump 1001→1009, missing 1002-1008)
    import random as _rnd
    _rnd.seed(42)
    _tick_rows = []
    _seq = 1

    def _make_ticks(symbol, feed, times, base_price, seq_offset=0, seq_skip_after=None):
        nonlocal _seq
        rows = []
        for i, ts in enumerate(times):
            px = round(base_price + _rnd.uniform(-0.5, 0.5), 2)
            vol = _rnd.randint(200, 8000)
            s = _seq
            if seq_skip_after and i == seq_skip_after:
                _seq += 8          # create a gap of 7 seq nums
            rows.append((symbol, ts, px, vol, s, feed))
            _seq += 1
        return rows

    # Build minute-bar timestamps for a trading day
    _all_times = []
    for _h in range(9, 16):
        for _m in range(0, 60):
            if _h == 9 and _m < 30: continue
            if _h == 15 and _m > 0: continue
            _all_times.append(f"2026-05-01 {_h:02d}:{_m:02d}:00")

    _window_times = [t for t in _all_times if "14:3" in t or "14:4" in t or "14:5" in t]  # 30 ticks
    _pre_window   = [t for t in _all_times if t not in _window_times]                       # 360 ticks

    # PRIMARY feed
    _tick_rows += _make_ticks("AAPL",  "PRIMARY",   _all_times,  185.00)                     # full day
    _tick_rows += _make_ticks("MSFT",  "PRIMARY",   _all_times,  420.00)                     # full day
    _tick_rows += _make_ticks("TSLA",  "PRIMARY",   _all_times,  248.00)                     # full day
    _tick_rows += _make_ticks("GOOGL", "PRIMARY",   _all_times,  175.00, seq_skip_after=50)  # seq gap
    _tick_rows += _make_ticks("NVDA",  "PRIMARY",   _pre_window, 495.00)                     # missing 14:30-15:00

    # SECONDARY feed
    _tick_rows += _make_ticks("AAPL",  "SECONDARY", _all_times,  185.00)                     # full day
    _tick_rows += _make_ticks("MSFT",  "SECONDARY", _pre_window, 420.00)                     # missing 14:30-15:00
    _tick_rows += _make_ticks("TSLA",  "SECONDARY", _all_times,  248.00, seq_skip_after=30)  # seq gap
    _tick_rows += _make_ticks("GOOGL", "SECONDARY", _all_times,  175.00)                     # full day
    _tick_rows += _make_ticks("NVDA",  "SECONDARY", _all_times,  495.00)                     # full day

    c.executemany(
        "INSERT INTO tick_data (symbol, ts, price, volume, seq_num, feed) VALUES (?,?,?,?,?,?)",
        _tick_rows,
    )

    conn.commit()
    conn.close()
    ok(f"Database created: {DB_PATH}")
    ok("Tables: traders(5) trades(15) positions(6) orders_audit(4) tick_data(see S-07/08)")


# ══════════════════════════════════════════════
#  WRITE SQL SOLUTION SCRIPTS
# ══════════════════════════════════════════════

def write_solution(name: str, sql: str) -> Path:
    path = DIRS["scripts"] / name
    path.write_text(f"-- {name}\n-- Run: sqlite3 {DB_PATH} < {path}\n\n{sql}\n")
    ok(f"Solution script: {path}")
    return path

def run_query(sql: str) -> None:
    """Execute a query and print results for preview."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(sql)
        rows = c.fetchall()
        if rows:
            cols = [d[0] for d in c.description]
            print(f"  {' | '.join(f'{c:<16}' for c in cols[:6])}")
            print(f"  {'-'*min(80,16*len(cols[:6]))}")
            for row in rows[:5]:
                print(f"  {' | '.join(str(row[i])[:16]+'...' if len(str(row[i]))>16 else f'{str(row[i]):<16}' for i in range(min(6,len(cols))))}")
            if len(rows) > 5: print(f"  ... ({len(rows)} rows total)")
        conn.close()
    except Exception as e:
        warn(f"Query preview failed: {e}")


# ══════════════════════════════════════════════
#  SCENARIO LAUNCHERS
# ══════════════════════════════════════════════

def launch_scenario_1():
    header("Scenario S-01 — Find All Fills for a Trader")
    print("  Alice (T001) says her P&L looks wrong. Pull all her fills.\n")

    sol = write_solution("s01_alice_fills.sql", """\
-- All filled trades for Alice, ordered by time
SELECT
  trade_id,
  symbol,
  side,
  quantity,
  price,
  ROUND(quantity * price, 2)  AS notional,
  trade_time,
  venue
FROM trades
WHERE trader_id = 'T001'
  AND status    = 'FILLED'
ORDER BY trade_time ASC;

-- Grand total traded value
SELECT
  trader_id,
  COUNT(*)                          AS fill_count,
  ROUND(SUM(quantity * price), 2)   AS total_notional,
  ROUND(AVG(price), 4)              AS avg_price
FROM trades
WHERE trader_id = 'T001'
  AND status    = 'FILLED';

-- Breakdown by symbol
SELECT
  symbol,
  side,
  SUM(quantity)                     AS total_qty,
  ROUND(AVG(price), 4)              AS avg_price,
  ROUND(SUM(quantity * price), 2)   AS notional
FROM trades
WHERE trader_id = 'T001'
  AND status    = 'FILLED'
GROUP BY symbol, side
ORDER BY symbol, side;
""")

    print(f"""
{BOLD}── Connect to the database ─────────────────────────────{RESET}
{CYAN}       sqlite3 {DB_PATH}{RESET}

{BOLD}── Run the solution ────────────────────────────────────{RESET}
{CYAN}       sqlite3 {DB_PATH} < {sol}{RESET}

{BOLD}── Or type queries interactively ───────────────────────{RESET}
{CYAN}       sqlite3 {DB_PATH}
       .headers on
       .mode column
       SELECT * FROM trades WHERE trader_id='T001' AND status='FILLED';{RESET}

{BOLD}── Preview (first 5 rows): ─────────────────────────────{RESET}""")
    run_query("SELECT trade_id,symbol,side,quantity,price,ROUND(quantity*price,2) AS notional FROM trades WHERE trader_id='T001' AND status='FILLED' ORDER BY trade_time")


def launch_scenario_2():
    header("Scenario S-02 — Rejected Orders Report")
    print("  Risk management wants all rejections with trader details.\n")

    sol = write_solution("s02_rejections.sql", """\
-- Rejected orders with trader info, sorted by size
SELECT
  t.trade_id,
  tr.name                         AS trader_name,
  tr.desk,
  t.symbol,
  t.side,
  t.quantity,
  ROUND(t.quantity * t.price, 2)  AS notional_attempted,
  t.trade_time,
  t.venue
FROM trades t
INNER JOIN traders tr ON t.trader_id = tr.trader_id
WHERE t.status = 'REJECTED'
ORDER BY t.quantity DESC;

-- Count rejections per trader
SELECT
  tr.name,
  tr.desk,
  COUNT(*)                         AS rejection_count,
  SUM(t.quantity)                  AS total_qty_rejected
FROM trades t
INNER JOIN traders tr ON t.trader_id = tr.trader_id
WHERE t.status = 'REJECTED'
GROUP BY tr.name, tr.desk
ORDER BY rejection_count DESC;

-- Join with audit log to see rejection reasons
SELECT
  t.trade_id,
  tr.name,
  t.symbol,
  t.quantity,
  a.notes AS rejection_reason
FROM trades t
INNER JOIN traders tr ON t.trader_id = tr.trader_id
LEFT  JOIN orders_audit a ON t.trade_id = a.trade_id AND a.action = 'REJECT'
WHERE t.status = 'REJECTED'
ORDER BY t.trade_time;
""")

    print(f"""
{BOLD}── Run the solution ────────────────────────────────────{RESET}
{CYAN}       sqlite3 {DB_PATH} < {sol}{RESET}

{BOLD}── Key SQL concepts ────────────────────────────────────{RESET}
  INNER JOIN: only rows with matching trader_id in both tables
  LEFT JOIN: all trades even if no audit record (NULL for audit cols)
  GROUP BY + COUNT + SUM: aggregate rejection stats per trader
""")
    run_query("SELECT t.trade_id,tr.name,t.symbol,t.quantity,t.status FROM trades t INNER JOIN traders tr ON t.trader_id=tr.trader_id WHERE t.status='REJECTED'")


def launch_scenario_3():
    header("Scenario S-03 — Net Position Calculation & Discrepancy Check")
    print("  Recalculate positions from trades and compare to positions table.\n")

    sol = write_solution("s03_net_positions.sql", """\
-- Step 1: Calculate net position from trades (CASE WHEN for side)
SELECT
  trader_id,
  symbol,
  SUM(CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END)  AS calc_net_qty,
  ROUND(
    SUM(quantity * price) / NULLIF(SUM(quantity), 0)
  , 4)                                                           AS calc_avg_price,
  COUNT(*)                                                       AS fill_count
FROM trades
WHERE status = 'FILLED'
GROUP BY trader_id, symbol
ORDER BY trader_id, symbol;

-- Step 2: Compare calculated vs stored — find discrepancies
WITH calc AS (
  SELECT
    trader_id,
    symbol,
    SUM(CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END) AS calc_qty
  FROM trades
  WHERE status = 'FILLED'
  GROUP BY trader_id, symbol
)
SELECT
  c.trader_id,
  c.symbol,
  c.calc_qty                            AS calculated_from_trades,
  p.net_qty                             AS stored_in_positions,
  (c.calc_qty - COALESCE(p.net_qty,0))  AS discrepancy,
  CASE
    WHEN p.net_qty IS NULL THEN 'MISSING FROM POSITIONS TABLE'
    WHEN c.calc_qty != p.net_qty THEN 'QTY MISMATCH'
    ELSE 'OK'
  END AS status
FROM calc c
LEFT JOIN positions p
  ON c.trader_id = p.trader_id
  AND c.symbol   = p.symbol
WHERE c.calc_qty != COALESCE(p.net_qty, 0)
   OR p.net_qty IS NULL
ORDER BY ABS(c.calc_qty - COALESCE(p.net_qty, 0)) DESC;
""")

    print(f"""
{BOLD}── Run the solution ────────────────────────────────────{RESET}
{CYAN}       sqlite3 {DB_PATH} < {sol}{RESET}

{BOLD}── Key SQL concepts ────────────────────────────────────{RESET}
  CASE WHEN: flip SELL qty to negative for net calculation
  NULLIF: avoid divide-by-zero in weighted average price
  WITH (CTE): name a subquery for clarity and reuse
  LEFT JOIN: include calc rows even if no matching positions row
  COALESCE: treat NULL (missing) position as 0

{BOLD}── Expected discrepancies in this dataset ────────────────{RESET}
  T003 / AAPL: PARTIAL fill not in positions table
  T005 / SPY and QQQ: trader T005 missing from positions entirely
""")


def launch_scenario_4():
    header("Scenario S-04 — Trader Leaderboard with Window Functions")
    print("  Rank traders by notional volume, globally and within desk.\n")

    sol = write_solution("s04_leaderboard.sql", """\
-- Trader leaderboard with window functions
WITH trader_totals AS (
  SELECT
    t.trader_id,
    tr.name,
    tr.desk,
    ROUND(SUM(t.quantity * t.price), 2)  AS total_notional,
    COUNT(*)                             AS fill_count
  FROM trades t
  INNER JOIN traders tr ON t.trader_id = tr.trader_id
  WHERE t.status = 'FILLED'
  GROUP BY t.trader_id, tr.name, tr.desk
)
SELECT
  name,
  desk,
  total_notional,
  fill_count,
  RANK()    OVER (ORDER BY total_notional DESC)             AS global_rank,
  RANK()    OVER (PARTITION BY desk ORDER BY total_notional DESC) AS desk_rank,
  ROUND(
    SUM(total_notional) OVER (ORDER BY total_notional DESC
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
  , 2)                                                       AS running_total
FROM trader_totals
ORDER BY global_rank;
""")

    print(f"""
{BOLD}── Run the solution ────────────────────────────────────{RESET}
{CYAN}       sqlite3 {DB_PATH} < {sol}{RESET}

{BOLD}── Window function anatomy ─────────────────────────────{RESET}
  RANK() OVER (ORDER BY total_notional DESC)
    ↑                ↑
    window function  how to order within the window
  
  RANK() OVER (PARTITION BY desk ORDER BY total_notional DESC)
                   ↑
                   reset ranking for each desk
  
  SUM(total_notional) OVER (ORDER BY total_notional DESC
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
    → running total from first row to current row
""")
    run_query("SELECT tr.name,tr.desk,ROUND(SUM(t.quantity*t.price),2) AS notional FROM trades t INNER JOIN traders tr ON t.trader_id=tr.trader_id WHERE t.status='FILLED' GROUP BY tr.name,tr.desk ORDER BY notional DESC")


def launch_scenario_5():
    header("Scenario S-05 — Slow Query Investigation & Index Creation")
    print("  A report query is timing out. Use EXPLAIN to diagnose and fix.\n")

    # Create a larger table for meaningful query plans
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("DROP TABLE IF EXISTS big_trades")
    c.execute("""
        CREATE TABLE big_trades (
            trade_id   INTEGER PRIMARY KEY,
            trader_id  TEXT,
            symbol     TEXT,
            side       TEXT,
            quantity   INTEGER,
            price      REAL,
            trade_time TEXT,
            status     TEXT,
            venue      TEXT
        )
    """)
    symbols = ["AAPL","GOOGL","MSFT","TSLA","NVDA","AMZN","META","SPY","QQQ"]
    rows = []
    base = datetime(2024, 1, 15, 9, 30, 0)
    for i in range(50000):
        rows.append((
            i+1, f"T{(i%5)+1:03d}",
            random.choice(symbols),
            random.choice(["BUY","SELL"]),
            random.randint(50,5000),
            round(random.uniform(100,500),2),
            (base + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S"),
            random.choice(["FILLED"]*8+["REJECTED","PARTIAL"]),
            random.choice(["NYSE","NASDAQ","FX"]),
        ))
    c.executemany("INSERT INTO big_trades VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    ok(f"Created big_trades table with 50,000 rows for query plan exercises")

    sol = write_solution("s05_slow_query.sql", f"""\
-- Step 1: Show query plan BEFORE index (expect FULL TABLE SCAN)
EXPLAIN QUERY PLAN
SELECT trader_id, symbol, SUM(quantity) AS total_qty
FROM big_trades
WHERE symbol = 'AAPL' AND status = 'FILLED'
GROUP BY trader_id, symbol;
-- Look for: "SCAN big_trades" — that is a full table scan = slow

-- Step 2: Create an index to speed up the query
CREATE INDEX IF NOT EXISTS idx_bigtrades_symbol_status
  ON big_trades(symbol, status);

-- Step 3: Show query plan AFTER index
EXPLAIN QUERY PLAN
SELECT trader_id, symbol, SUM(quantity) AS total_qty
FROM big_trades
WHERE symbol = 'AAPL' AND status = 'FILLED'
GROUP BY trader_id, symbol;
-- Look for: "SEARCH big_trades USING INDEX" — much faster!

-- Step 4: Run the actual query (should be fast with index)
SELECT trader_id, symbol, SUM(quantity) AS total_qty
FROM big_trades
WHERE symbol = 'AAPL' AND status = 'FILLED'
GROUP BY trader_id, symbol
ORDER BY total_qty DESC;

-- Check all indexes on the table
SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='big_trades';
""")

    explain_script = DIRS["scripts"] / "s05_explain_demo.py"
    explain_script.write_text(f"""\
#!/usr/bin/env python3
\"\"\"S-05: Demonstrate query plan before and after index.\"\"\"
import sqlite3, time

DB = "{DB_PATH}"

def explain(conn, label, sql):
    t0 = time.perf_counter()
    c = conn.cursor()
    c.execute(f"EXPLAIN QUERY PLAN {{sql}}")
    plan = c.fetchall()
    c.execute(sql)
    rows = c.fetchall()
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"\\n{{label}} ({{elapsed:.1f}}ms, {{len(rows)}} rows)")
    for row in plan:
        print(f"  PLAN: {{row}}")

conn = sqlite3.connect(DB)

QUERY = "SELECT trader_id, symbol, SUM(quantity) FROM big_trades WHERE symbol='AAPL' AND status='FILLED' GROUP BY trader_id, symbol"

explain(conn, "BEFORE INDEX", QUERY)
conn.execute("CREATE INDEX IF NOT EXISTS idx_bt ON big_trades(symbol,status)")
explain(conn, "AFTER INDEX", QUERY)
conn.close()
print("\\nIndex created. Run EXPLAIN QUERY PLAN to see the difference.")
""")
    ok(f"Explain demo:  {explain_script}")

    print(f"""
{BOLD}── Run EXPLAIN before and after index ─────────────────{RESET}
{CYAN}       python3 {explain_script}{RESET}

{BOLD}── Or manually in sqlite3 ──────────────────────────────{RESET}
{CYAN}       sqlite3 {DB_PATH} < {sol}{RESET}

{BOLD}── Key concepts ────────────────────────────────────────{RESET}
  EXPLAIN QUERY PLAN: shows whether DB does SCAN (slow) or SEARCH (fast)
  SCAN = full table read → O(n), slow for millions of rows
  SEARCH USING INDEX → O(log n), fast
  
  Index on (symbol, status): covers the WHERE clause exactly
  Composite index order matters: put equality column first (symbol='AAPL')
  then range/filter column (status='FILLED')
  
  For trading: index on (trader_id, trade_time) for P&L queries
               index on (symbol, status) for fill lookups
               index on (trade_time) for time-window reports
""")


def launch_scenario_6():
    header("Scenario S-06 — Concurrent Writes & Locking")
    print("  Understand locking, transaction isolation, and deadlocks.\n")

    concurrent_script = DIRS["scripts"] / "s06_concurrent_writes.py"
    concurrent_script.write_text(f"""\
#!/usr/bin/env python3
\"\"\"
S-06: Simulate concurrent write scenario in SQLite.
Demonstrates: transactions, locking, retry logic, and isolation levels.
\"\"\"
import sqlite3, threading, time, random

DB = "{DB_PATH}"

def add_trade(trader_id: str, symbol: str, qty: int, price: float, thread_id: int):
    \"\"\"Write a trade atomically. Retries on lock.\"\"\"
    retries = 0
    max_retries = 5
    while retries < max_retries:
        try:
            conn = sqlite3.connect(DB, timeout=5)
            conn.execute("BEGIN IMMEDIATE")  # acquire write lock immediately
            
            # Simulate processing time (increases lock contention window)
            time.sleep(random.uniform(0.01, 0.05))
            
            conn.execute(
                "INSERT INTO trades (trader_id,symbol,side,quantity,price,trade_time,status,venue) "
                "VALUES (?,?,?,?,?,datetime('now'),?,?)",
                (trader_id, symbol, 'BUY', qty, price, 'FILLED', 'NYSE')
            )
            conn.commit()
            conn.close()
            print(f"  Thread-{{thread_id}}: Inserted trade {{trader_id}} {{symbol}} {{qty}}@{{price}}")
            return
        except sqlite3.OperationalError as e:
            conn.close()
            retries += 1
            print(f"  Thread-{{thread_id}}: Lock conflict (attempt {{retries}}/{{max_retries}}): {{e}}")
            time.sleep(0.1 * retries)  # exponential backoff
    print(f"  Thread-{{thread_id}}: FAILED after {{max_retries}} retries — would page on-call")

# Launch 5 concurrent writer threads
threads = []
orders = [
    ('T001', 'AAPL',  100, 185.50),
    ('T002', 'TSLA',   50, 248.00),
    ('T003', 'GOOGL',  75, 141.20),
    ('T004', 'MSFT',  200, 380.10),
    ('T005', 'NVDA',   60, 495.00),
]

print("Starting 5 concurrent write threads...")
for i, (tid, sym, qty, price) in enumerate(orders):
    t = threading.Thread(target=add_trade, args=(tid, sym, qty, price, i+1))
    threads.append(t)

for t in threads: t.start()
for t in threads: t.join()

# Verify all trades committed
conn = sqlite3.connect(DB)
c = conn.cursor()
c.execute("SELECT COUNT(*) FROM trades")
print(f"\\nTotal trades in DB: {{c.fetchone()[0]}}")
conn.close()
print("\\nKey takeaways:")
print("  BEGIN IMMEDIATE: acquires write lock at transaction start, no deadlocks")
print("  Retry with backoff: handles transient lock conflicts gracefully")
print("  In PostgreSQL: SELECT ... FOR UPDATE, advisory locks, SERIALIZABLE isolation")
""")
    ok(f"Concurrent writes script: {concurrent_script}")

    deadlock_notes = DIRS["scripts"] / "s06_deadlock_notes.sql"
    deadlock_notes.write_text("""\
-- S-06: Deadlock and Locking Notes
-- ===================================
-- This file is a REFERENCE only — not all statements run in SQLite.
-- Sections are clearly marked: [SQLite] or [PostgreSQL only].

-- ─── PATTERN 1: Consistent lock ordering prevents deadlocks ──────────────
-- [Both] — concept applies to all databases
-- Always acquire locks in the same order (e.g., by primary key ascending)
-- Thread A: lock trader T001, then T002
-- Thread B: lock trader T001, then T002  ← same order = no deadlock
--
-- If threads lock in different orders:
-- Thread A: lock T001, wait for T002
-- Thread B: lock T002, wait for T001  ← deadlock: both wait forever

-- ─── PATTERN 2: Pessimistic locking ─────────────────────────────────────

-- [SQLite] — BEGIN IMMEDIATE acquires write lock at transaction start
BEGIN IMMEDIATE;
  SELECT net_qty, avg_price
  FROM positions
  WHERE trader_id = 'T001' AND symbol = 'AAPL';

  UPDATE positions
  SET net_qty    = net_qty + 100,
      updated_at = datetime('now')
  WHERE trader_id = 'T001' AND symbol = 'AAPL';
COMMIT;

-- [PostgreSQL only] — SELECT FOR UPDATE locks the row until COMMIT/ROLLBACK
-- FOR UPDATE does not exist in SQLite
-- BEGIN;
--   SELECT net_qty, avg_price
--   FROM positions
--   WHERE trader_id = 'T001' AND symbol = 'AAPL'
--   FOR UPDATE;
--
--   UPDATE positions
--   SET net_qty    = net_qty + 100,
--       updated_at = NOW()
--   WHERE trader_id = 'T001' AND symbol = 'AAPL';
-- COMMIT;

-- ─── PATTERN 3: Upsert (insert or update atomically) ────────────────────
-- [SQLite] — uses datetime('now'), not NOW()
INSERT INTO positions (trader_id, symbol, net_qty, avg_price, updated_at)
VALUES ('T001', 'AAPL', 100, 185.50, datetime('now'))
ON CONFLICT (trader_id, symbol) DO UPDATE
  SET net_qty    = positions.net_qty + 100,
      updated_at = datetime('now');

-- ─── PATTERN 4: Detect long-running locks ───────────────────────────────
-- [PostgreSQL only] — pg_stat_activity does not exist in SQLite
-- SELECT pid, query, state, wait_event_type, wait_event,
--        now() - pg_stat_activity.query_start AS duration
-- FROM pg_stat_activity
-- WHERE state != 'idle'
--   AND query_start < now() - interval '30 seconds';

-- ─── PATTERN 5: Kill a blocking query ───────────────────────────────────
-- [PostgreSQL only] — these functions do not exist in SQLite
-- SELECT pg_cancel_backend(pid);      -- graceful stop
-- SELECT pg_terminate_backend(pid);   -- force kill
""")
    ok(f"Locking notes: {deadlock_notes}")

    print(f"""
{BOLD}── Run concurrent writes simulation ────────────────────{RESET}
{CYAN}       python3 {concurrent_script}{RESET}

{BOLD}── Read locking reference ───────────────────────────────{RESET}
{CYAN}       cat {deadlock_notes}{RESET}

{BOLD}── Key concepts ────────────────────────────────────────{RESET}
  DEADLOCK: Thread A holds lock on row 1, waits for row 2.
            Thread B holds lock on row 2, waits for row 1.
            → Both wait forever. DB detects and kills one.
  
  Prevention: always acquire locks in the same order (by PK ascending)
  SQLite: single-writer, BEGIN IMMEDIATE avoids deadlocks entirely
  PostgreSQL: row-level locks, SELECT FOR UPDATE, advisory locks
  
  TRANSACTION ISOLATION LEVELS:
  READ UNCOMMITTED → can read dirty (uncommitted) data — never for trading
  READ COMMITTED   → sees committed data at time of each statement
  REPEATABLE READ  → sees committed data at transaction start
  SERIALIZABLE     → transactions appear to execute one at a time (slowest, safest)
""")


# ══════════════════════════════════════════════
#  SCENARIO 7 — Market Data Gap Investigation
# ══════════════════════════════════════════════

def launch_scenario_7():
    header("Scenario S-07 — Market Data Gap Investigation")
    print("  A researcher opens a ticket: 'NVDA data looks wrong for yesterday afternoon.'")
    print("  Use SQL to identify which symbols have gaps in the tick_data table.\n")

    setup_database()   # ensure DB exists with tick_data

    write_solution("s07_01_zero_tick_window.sql", """\
-- S-07a: Find symbols with ZERO ticks in the 14:30-15:00 window (PRIMARY feed)
-- This is the first query to run when a researcher reports missing afternoon data.

SELECT   symbol,
         COUNT(*) AS tick_count
FROM     tick_data
WHERE    feed = 'PRIMARY'
  AND    ts BETWEEN '2026-05-01 14:30:00' AND '2026-05-01 14:59:00'
GROUP BY symbol
ORDER BY tick_count ASC;

-- Expected: NVDA shows 0 ticks (or does not appear), others show ~30 ticks.
-- If a symbol is MISSING entirely from results, use:

SELECT DISTINCT symbol FROM tick_data WHERE feed = 'PRIMARY'
EXCEPT
SELECT symbol FROM tick_data
WHERE  feed = 'PRIMARY'
  AND  ts BETWEEN '2026-05-01 14:30:00' AND '2026-05-01 14:59:00';
-- Returns symbols that have data elsewhere but NOTHING in this window.
""")

    write_solution("s07_02_last_good_tick.sql", """\
-- S-07b: Find the last good timestamp before the gap (per symbol, PRIMARY feed)
-- Run after identifying NVDA has zero ticks in the 14:30 window.

SELECT   symbol,
         MAX(ts)    AS last_tick_before_gap,
         MAX(price) AS last_price,
         MAX(seq_num) AS last_seq_num
FROM     tick_data
WHERE    feed   = 'PRIMARY'
  AND    symbol = 'NVDA'
  AND    ts     < '2026-05-01 14:30:00'
GROUP BY symbol;

-- This tells you: the feed was alive up to X, then went silent.
-- Useful for Kafka lag correlation: did the consumer stop at that time too?
""")

    write_solution("s07_03_sequence_gaps.sql", """\
-- S-07c: Find sequence number gaps per symbol (indicates dropped ticks)
-- A gap in seq_num means ticks were DROPPED between the exchange and storage.

WITH numbered AS (
    SELECT symbol,
           feed,
           ts,
           seq_num,
           LAG(seq_num) OVER (PARTITION BY symbol, feed ORDER BY seq_num) AS prev_seq
    FROM   tick_data
    WHERE  feed = 'PRIMARY'
)
SELECT symbol,
       feed,
       prev_seq          AS gap_starts_after,
       seq_num           AS gap_ends_at,
       (seq_num - prev_seq - 1) AS ticks_missing,
       ts                AS resumed_at
FROM   numbered
WHERE  prev_seq IS NOT NULL
  AND  (seq_num - prev_seq) > 1
ORDER BY ticks_missing DESC;

-- GOOGL PRIMARY should show a gap of 7 missing ticks.
-- A gap means: either the Kafka consumer dropped messages, or the feed handler
-- did not receive them (UDP multicast packet loss).
""")

    write_solution("s07_04_tick_count_by_hour.sql", """\
-- S-07d: Tick count by symbol and hour — spot the dead period visually
-- Good for a quick sanity check across the whole trading day.

SELECT   symbol,
         feed,
         SUBSTR(ts, 1, 13) AS hour,
         COUNT(*)           AS tick_count
FROM     tick_data
WHERE    feed = 'PRIMARY'
GROUP BY symbol, feed, hour
ORDER BY symbol, hour;

-- Look for symbols where tick_count drops to 0 for one or more hours.
-- NVDA will show 0 for the 14:xx hour.
""")

    print(f"""
{BOLD}── DB path ───────────────────────────────────────────────────{RESET}
{CYAN}  sqlite3 {DB_PATH}{RESET}

{BOLD}── S-07a: Which symbols have zero ticks in the 14:30 window? ──{RESET}
{CYAN}  sqlite3 {DB_PATH} < {DIRS["scripts"]}/s07_01_zero_tick_window.sql{RESET}
""")
    run_query("""
        SELECT symbol, COUNT(*) AS tick_count
        FROM   tick_data
        WHERE  feed = 'PRIMARY'
          AND  ts BETWEEN '2026-05-01 14:30:00' AND '2026-05-01 14:59:00'
        GROUP BY symbol ORDER BY tick_count ASC
    """)

    print(f"""
{BOLD}── S-07b: Last good tick before the gap ──────────────────{RESET}
{CYAN}  sqlite3 {DB_PATH} < {DIRS["scripts"]}/s07_02_last_good_tick.sql{RESET}

{BOLD}── S-07c: Sequence number gaps (dropped ticks) ───────────{RESET}
{CYAN}  sqlite3 {DB_PATH} < {DIRS["scripts"]}/s07_03_sequence_gaps.sql{RESET}
""")
    run_query("""
        WITH numbered AS (
            SELECT symbol, feed, seq_num,
                   LAG(seq_num) OVER (PARTITION BY symbol, feed ORDER BY seq_num) AS prev_seq
            FROM   tick_data WHERE feed = 'PRIMARY'
        )
        SELECT symbol, prev_seq AS gap_starts_after, seq_num AS gap_ends_at,
               (seq_num - prev_seq - 1) AS ticks_missing
        FROM   numbered
        WHERE  prev_seq IS NOT NULL AND (seq_num - prev_seq) > 1
        ORDER BY ticks_missing DESC
    """)

    print(f"""
{BOLD}── S-07d: Tick count by hour ─────────────────────────────{RESET}
{CYAN}  sqlite3 {DB_PATH} < {DIRS["scripts"]}/s07_04_tick_count_by_hour.sql{RESET}

{BOLD}── Key Interview Points ─────────────────────────────────{RESET}
  STEP 1: COUNT(*) grouped by symbol in the suspect window → find the zero
  STEP 2: EXCEPT query → symbols present elsewhere but missing from window
  STEP 3: MAX(ts) WHERE ts < window_start → last good timestamp before gap
  STEP 4: LAG(seq_num) OVER PARTITION BY symbol → find sequence gaps
  STEP 5: Correlate gap timestamp with Kafka lag logs / Argo Workflow logs

  Escalation note should include:
    - Affected symbol(s) and exact time window
    - Last known good seq_num before the gap
    - Whether gap is on PRIMARY, SECONDARY, or both
    - Corresponding Kafka consumer lag at that timestamp
""")


# ══════════════════════════════════════════════
#  SCENARIO 8 — Feed Completeness Cross-Check
# ══════════════════════════════════════════════

def launch_scenario_8():
    header("Scenario S-08 — Feed Completeness Cross-Check (Primary vs Secondary)")
    print("  Your firm receives tick data from two redundant feeds.")
    print("  Use SQL to find symbols where one feed has data the other is missing.\n")
    print("  SECONDARY feed is missing MSFT data for the 14:30-15:00 window.")
    print("  TSLA has a sequence gap on SECONDARY but not PRIMARY.\n")

    setup_database()

    write_solution("s08_01_feed_coverage_comparison.sql", """\
-- S-08a: Find symbols with ticks on PRIMARY but NOT on SECONDARY in a window
-- Run this when validating feed redundancy or after a secondary feed incident.

SELECT   p.symbol,
         COUNT(p.id)                    AS primary_ticks,
         COALESCE(COUNT(s.id), 0)       AS secondary_ticks,
         COUNT(p.id) - COALESCE(COUNT(s.id), 0) AS delta
FROM     tick_data p
LEFT JOIN tick_data s
       ON s.symbol = p.symbol
      AND s.ts     = p.ts
      AND s.feed   = 'SECONDARY'
WHERE    p.feed = 'PRIMARY'
  AND    p.ts BETWEEN '2026-05-01 14:30:00' AND '2026-05-01 14:59:00'
GROUP BY p.symbol
ORDER BY delta DESC;

-- delta > 0 means PRIMARY has ticks that SECONDARY is missing.
-- MSFT should show the largest delta.
""")

    write_solution("s08_02_secondary_only_gaps.sql", """\
-- S-08b: Timestamps on PRIMARY with no matching tick on SECONDARY
-- Drill into MSFT to find exactly which minutes are missing on SECONDARY.

SELECT   p.ts,
         p.symbol,
         p.seq_num        AS primary_seq,
         p.price          AS primary_price,
         s.seq_num        AS secondary_seq    -- NULL if missing
FROM     tick_data p
LEFT JOIN tick_data s
       ON s.symbol = p.symbol
      AND s.ts     = p.ts
      AND s.feed   = 'SECONDARY'
WHERE    p.feed   = 'PRIMARY'
  AND    p.symbol = 'MSFT'
  AND    p.ts BETWEEN '2026-05-01 14:30:00' AND '2026-05-01 14:59:00'
  AND    s.id IS NULL
ORDER BY p.ts;

-- Every row returned = a minute-bar missing on the secondary feed.
""")

    write_solution("s08_03_seq_gap_comparison.sql", """\
-- S-08c: Compare sequence gaps between PRIMARY and SECONDARY
-- A gap on both feeds = exchange/network issue.
-- A gap on one feed only = that feed handler dropped packets.

WITH gaps AS (
    SELECT feed,
           symbol,
           seq_num,
           LAG(seq_num) OVER (PARTITION BY feed, symbol ORDER BY seq_num) AS prev_seq,
           ts
    FROM   tick_data
    WHERE  symbol = 'TSLA'
)
SELECT feed,
       symbol,
       prev_seq           AS gap_after_seq,
       seq_num            AS gap_before_seq,
       (seq_num - prev_seq - 1) AS missing_count,
       ts                 AS gap_detected_at
FROM   gaps
WHERE  prev_seq IS NOT NULL
  AND  (seq_num - prev_seq) > 1
ORDER BY feed, gap_after_seq;

-- TSLA SECONDARY should show a gap; PRIMARY should not.
-- This tells you the issue is in the secondary feed handler, not the exchange.
""")

    write_solution("s08_04_summary_report.sql", """\
-- S-08d: Full feed completeness summary for the trading day
-- Run at EOD to validate both feeds captured all symbols.

SELECT   symbol,
         feed,
         COUNT(*)           AS total_ticks,
         MIN(ts)            AS first_tick,
         MAX(ts)            AS last_tick,
         MAX(seq_num) - MIN(seq_num) + 1 AS expected_seq_range,
         COUNT(*)           AS actual_count,
         (MAX(seq_num) - MIN(seq_num) + 1) - COUNT(*) AS seq_gaps
FROM     tick_data
GROUP BY symbol, feed
ORDER BY symbol, feed;

-- seq_gaps > 0 means sequence numbers are not contiguous — ticks were dropped.
-- Cross-reference with expected_seq_range vs actual_count.
""")

    print(f"""
{BOLD}── S-08a: Which symbols are PRIMARY-only in the 14:30 window? ─{RESET}
{CYAN}  sqlite3 {DB_PATH} < {DIRS["scripts"]}/s08_01_feed_coverage_comparison.sql{RESET}
""")
    run_query("""
        SELECT p.symbol,
               COUNT(p.id) AS primary_ticks,
               COALESCE(COUNT(s.id), 0) AS secondary_ticks,
               COUNT(p.id) - COALESCE(COUNT(s.id), 0) AS delta
        FROM   tick_data p
        LEFT JOIN tick_data s ON s.symbol=p.symbol AND s.ts=p.ts AND s.feed='SECONDARY'
        WHERE  p.feed='PRIMARY'
          AND  p.ts BETWEEN '2026-05-01 14:30:00' AND '2026-05-01 14:59:00'
        GROUP BY p.symbol ORDER BY delta DESC
    """)

    print(f"""
{BOLD}── S-08b: Exact missing minutes for MSFT on SECONDARY ───────{RESET}
{CYAN}  sqlite3 {DB_PATH} < {DIRS["scripts"]}/s08_02_secondary_only_gaps.sql{RESET}

{BOLD}── S-08c: Seq gap comparison PRIMARY vs SECONDARY (TSLA) ────{RESET}
{CYAN}  sqlite3 {DB_PATH} < {DIRS["scripts"]}/s08_03_seq_gap_comparison.sql{RESET}
""")
    run_query("""
        WITH gaps AS (
            SELECT feed, symbol, seq_num,
                   LAG(seq_num) OVER (PARTITION BY feed, symbol ORDER BY seq_num) AS prev_seq,
                   ts
            FROM   tick_data WHERE symbol = 'TSLA'
        )
        SELECT feed, symbol, prev_seq AS gap_after_seq, seq_num AS gap_before_seq,
               (seq_num - prev_seq - 1) AS missing_count
        FROM   gaps WHERE prev_seq IS NOT NULL AND (seq_num - prev_seq) > 1
        ORDER BY feed
    """)

    print(f"""
{BOLD}── S-08d: Full EOD completeness summary ─────────────────────{RESET}
{CYAN}  sqlite3 {DB_PATH} < {DIRS["scripts"]}/s08_04_summary_report.sql{RESET}

{BOLD}── Key Interview Points ─────────────────────────────────────{RESET}
  LEFT JOIN feeds on (symbol, ts) → find timestamps present on one but not the other
  LAG(seq_num) OVER PARTITION BY (feed, symbol) → sequence gap detection
  Gap on BOTH feeds = exchange/network issue (escalate to vendor)
  Gap on ONE feed only = that feed handler dropped packets (internal fix)

  The escalation note to dev should answer:
    - Which feed is affected (PRIMARY, SECONDARY, or both)?
    - Which symbols and what time window?
    - Are the seq_nums on the other feed continuous (confirming single-feed issue)?
    - What is the tick count delta between feeds?
""")


def launch_scenario_99():
    header("Scenario 99 — ALL SQL Scenarios")
    for fn in [launch_scenario_1, launch_scenario_2, launch_scenario_3,
               launch_scenario_4, launch_scenario_5, launch_scenario_6,
               launch_scenario_7, launch_scenario_8]:
        fn(); time.sleep(0.1)


# ══════════════════════════════════════════════
#  TEARDOWN / STATUS / MAIN
# ══════════════════════════════════════════════

def teardown():
    header("Tearing Down SQL Lab")
    shutil.rmtree(LAB_ROOT, ignore_errors=True)
    ok(f"Removed {LAB_ROOT}")

def show_status():
    header("SQL Lab Status")
    if DB_PATH and DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        c = conn.cursor()
        for tbl in ["traders","trades","positions","orders_audit"]:
            try:
                c.execute(f"SELECT COUNT(*) FROM {tbl}")
                ok(f"Table {tbl}: {c.fetchone()[0]} rows")
            except: warn(f"Table {tbl}: not found")
        conn.close()
    else:
        warn("Database not found — run a scenario first")

SCENARIO_MAP = {
    1:  (launch_scenario_1,  "S-01  Find all fills for a trader"),
    2:  (launch_scenario_2,  "S-02  Rejected orders report"),
    3:  (launch_scenario_3,  "S-03  Net position & discrepancy check"),
    4:  (launch_scenario_4,  "S-04  Leaderboard with window functions"),
    5:  (launch_scenario_5,  "S-05  Slow query + EXPLAIN + index"),
    6:  (launch_scenario_6,  "S-06  Concurrent writes & locking"),
    7:  (launch_scenario_7,  "S-07  Market data gap investigation"),
    8:  (launch_scenario_8,  "S-08  Feed completeness cross-check (primary vs secondary)"),
    99: (launch_scenario_99, "      ALL scenarios"),
}

def main():
    parser = argparse.ArgumentParser(description="SQL Challenge Lab Setup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(f"  {k:<4} {v}" for k, (_, v) in SCENARIO_MAP.items()))
    parser.add_argument("--scenario", "-s", type=int, choices=list(SCENARIO_MAP.keys()))
    parser.add_argument("--teardown", "-t", action="store_true")
    parser.add_argument("--status",         action="store_true")
    args = parser.parse_args()
    if args.teardown: teardown(); return
    if args.status: show_status(); return
    create_dirs()
    setup_database()
    if args.scenario:
        fn, _ = SCENARIO_MAP[args.scenario]; fn()
    else:
        header("SQL Challenge Lab")
        print(f"  Database: {DB_PATH}\n")
        for num, (_, desc) in SCENARIO_MAP.items():
            print(f"    {num:<4} {desc}")
        choice = input("\n  Enter scenario number (or q): ").strip()
        if choice.lower() == "q": return
        try:
            fn, _ = SCENARIO_MAP[int(choice)]; fn()
        except (KeyError, ValueError): err(f"Invalid: {choice}")
    print(f"\n{BOLD}{SEPARATOR}{RESET}")
    print(f"{GREEN}{BOLD}  Lab is live. Work through the tasks above.{RESET}")
    print(f"{CYAN}    sqlite3 {DB_PATH}               # connect directly")
    print(f"    python3 lab_sql.py --status")
    print(f"    python3 lab_sql.py --teardown{RESET}")
    print(f"{BOLD}{SEPARATOR}{RESET}\n")

if __name__ == "__main__":
    main()
