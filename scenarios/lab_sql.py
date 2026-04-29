#!/usr/bin/env python3
"""
Support Engineer Challenge Lab — SQL Setup
==========================================
Creates a SQLite trading database with realistic data for all SQL scenarios (S1-S6).

Run with: python3 lab_sql.py [--scenario N] [--teardown]

SCENARIOS:
  1   S-01  Find all fills for a trader
  2   S-02  Find rejected orders and their traders
  3   S-03  Calculate net position per trader per symbol
  4   S-04  Top traders by volume — window functions
  5   S-05  Slow query investigation — EXPLAIN + index creation
  6   S-06  Deadlock / locking scenario — concurrent writes + advisory notes
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

    conn.commit()
    conn.close()
    ok(f"Database created: {DB_PATH}")
    ok("Tables: traders (5 rows), trades (15 rows), positions (6 rows), orders_audit (4 rows)")


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


def launch_scenario_99():
    header("Scenario 99 — ALL SQL Scenarios")
    for fn in [launch_scenario_1, launch_scenario_2, launch_scenario_3,
               launch_scenario_4, launch_scenario_5, launch_scenario_6]:
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
    1:  (launch_scenario_1, "S-01  Find all fills for a trader"),
    2:  (launch_scenario_2, "S-02  Rejected orders report"),
    3:  (launch_scenario_3, "S-03  Net position & discrepancy check"),
    4:  (launch_scenario_4, "S-04  Leaderboard with window functions"),
    5:  (launch_scenario_5, "S-05  Slow query + EXPLAIN + index"),
    6:  (launch_scenario_6, "S-06  Concurrent writes & locking"),
    99: (launch_scenario_99, "     ALL scenarios"),
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
