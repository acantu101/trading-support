# Runbook: SQL & Databases

## Overview

This runbook covers SQL query writing, performance investigation, transaction management, and position reconciliation for trading databases. All scenarios map to `lab_sql.py` (S-01 through S-06).

The lab uses SQLite. Production trading systems typically run PostgreSQL. SQL syntax is compatible unless noted.

---

## Connect to the Lab Database

```bash
# Launch the lab
python3 lab_sql.py

# Connect directly with sqlite3
sqlite3 /tmp/lab_sql/db/trading.db

# Useful sqlite3 settings
.headers on
.mode column
.width 12 6 6 10 10 8

# Run a script file
sqlite3 /tmp/lab_sql/db/trading.db < /tmp/lab_sql/scripts/s01_alice_fills.sql
```

---

## Schema Reference

```sql
-- Traders
traders (trader_id TEXT PK, name TEXT, desk TEXT, email TEXT, joined_date TEXT)

-- Trades
trades (
  trade_id    INTEGER PK,
  trader_id   TEXT FK → traders,
  symbol      TEXT,
  side        TEXT  CHECK IN ('BUY','SELL'),
  quantity    INTEGER,
  price       REAL,
  trade_time  TEXT,
  status      TEXT  CHECK IN ('FILLED','REJECTED','PARTIAL','PENDING'),
  venue       TEXT
)

-- Positions (should match net of filled trades)
positions (
  trader_id  TEXT,
  symbol     TEXT,
  net_qty    INTEGER,
  avg_price  REAL,
  updated_at TEXT,
  PRIMARY KEY (trader_id, symbol)
)

-- Audit log
orders_audit (audit_id INTEGER PK, trade_id INTEGER, action TEXT,
              action_time TEXT, actor TEXT, notes TEXT)
```

---

## S-01 — Find All Fills for a Trader

### Query

```sql
-- All filled trades for a trader, ordered by time
SELECT
  trade_id,
  symbol,
  side,
  quantity,
  price,
  ROUND(quantity * price, 2)   AS notional,
  trade_time,
  venue
FROM trades
WHERE trader_id = 'T001'
  AND status    = 'FILLED'
ORDER BY trade_time ASC;

-- Summary: total fills and notional
SELECT
  trader_id,
  COUNT(*)                         AS fill_count,
  ROUND(SUM(quantity * price), 2)  AS total_notional,
  ROUND(AVG(price), 4)             AS avg_price
FROM trades
WHERE trader_id = 'T001'
  AND status    = 'FILLED';

-- Breakdown by symbol
SELECT
  symbol,
  side,
  SUM(quantity)                    AS total_qty,
  ROUND(AVG(price), 4)             AS avg_price,
  ROUND(SUM(quantity * price), 2)  AS notional
FROM trades
WHERE trader_id = 'T001'
  AND status    = 'FILLED'
GROUP BY symbol, side
ORDER BY symbol, side;
```

---

## S-02 — Rejected Orders Report

### Query

```sql
-- Rejections with trader details and audit reason
SELECT
  t.trade_id,
  tr.name                        AS trader_name,
  tr.desk,
  t.symbol,
  t.side,
  t.quantity,
  ROUND(t.quantity * t.price, 2) AS notional_attempted,
  t.trade_time,
  a.notes                        AS rejection_reason
FROM trades t
INNER JOIN traders tr ON t.trader_id = tr.trader_id
LEFT  JOIN orders_audit a
        ON t.trade_id = a.trade_id AND a.action = 'REJECT'
WHERE t.status = 'REJECTED'
ORDER BY t.quantity DESC;

-- Count rejections per trader
SELECT
  tr.name,
  tr.desk,
  COUNT(*)          AS rejection_count,
  SUM(t.quantity)   AS total_qty_rejected
FROM trades t
INNER JOIN traders tr ON t.trader_id = tr.trader_id
WHERE t.status = 'REJECTED'
GROUP BY tr.name, tr.desk
ORDER BY rejection_count DESC;
```

### JOIN Refresher

| JOIN Type | Behaviour |
|-----------|-----------|
| `INNER JOIN` | Only rows with a match in both tables |
| `LEFT JOIN` | All rows from the left table; NULL for unmatched right rows |
| `RIGHT JOIN` | All rows from the right table (use LEFT JOIN + swap tables instead) |

---

## S-03 — Net Position Reconciliation

Recalculate positions from the trades table and find discrepancies with the stored positions table.

### Query

```sql
-- Step 1: Calculate net position from trades
SELECT
  trader_id,
  symbol,
  SUM(CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END)  AS calc_net_qty,
  ROUND(
    SUM(quantity * price) / NULLIF(SUM(quantity), 0)
  , 4)                                                           AS calc_avg_price
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
  c.calc_qty                             AS from_trades,
  p.net_qty                              AS in_positions,
  (c.calc_qty - COALESCE(p.net_qty, 0))  AS discrepancy,
  CASE
    WHEN p.net_qty IS NULL THEN 'MISSING'
    WHEN c.calc_qty != p.net_qty THEN 'MISMATCH'
    ELSE 'OK'
  END AS status
FROM calc c
LEFT JOIN positions p
  ON c.trader_id = p.trader_id AND c.symbol = p.symbol
WHERE c.calc_qty != COALESCE(p.net_qty, 0)
   OR p.net_qty IS NULL
ORDER BY ABS(c.calc_qty - COALESCE(p.net_qty, 0)) DESC;
```

### Key SQL Patterns

```sql
-- CASE WHEN for conditional aggregation
SUM(CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END)

-- NULLIF prevents divide-by-zero
SUM(price) / NULLIF(SUM(quantity), 0)

-- COALESCE treats NULL as 0
COALESCE(p.net_qty, 0)

-- CTE (WITH clause) for readable subqueries
WITH calc AS (SELECT ...)
SELECT ... FROM calc LEFT JOIN positions p ...
```

---

## S-04 — Window Functions

### Query

```sql
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
  RANK()    OVER (ORDER BY total_notional DESC)                          AS global_rank,
  RANK()    OVER (PARTITION BY desk ORDER BY total_notional DESC)        AS desk_rank,
  ROUND(
    SUM(total_notional) OVER (
      ORDER BY total_notional DESC
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )
  , 2)                                                                    AS running_total
FROM trader_totals
ORDER BY global_rank;
```

### Window Function Anatomy

```sql
FUNCTION() OVER (
  PARTITION BY <column>        -- reset for each group (optional)
  ORDER BY <column> DESC       -- order within the window
  ROWS BETWEEN ... AND ...     -- frame clause (optional)
)
```

| Function | Notes |
|----------|-------|
| `RANK()` | Gaps after ties: 1, 1, 3 |
| `DENSE_RANK()` | No gaps after ties: 1, 1, 2 |
| `ROW_NUMBER()` | Unique sequential number |
| `SUM() OVER (...)` | Running total |
| `LAG(col, 1)` | Previous row's value |
| `LEAD(col, 1)` | Next row's value |

---

## S-05 — Slow Query Investigation

### EXPLAIN QUERY PLAN (SQLite)

```sql
-- Before adding an index — expect a full table scan
EXPLAIN QUERY PLAN
SELECT trader_id, symbol, SUM(quantity)
FROM big_trades
WHERE symbol = 'AAPL' AND status = 'FILLED'
GROUP BY trader_id, symbol;
-- Output: "SCAN big_trades" → O(n), slow

-- Create a composite index
CREATE INDEX idx_bigtrades_symbol_status
  ON big_trades(symbol, status);

-- After index — search instead of scan
EXPLAIN QUERY PLAN
SELECT trader_id, symbol, SUM(quantity)
FROM big_trades
WHERE symbol = 'AAPL' AND status = 'FILLED'
GROUP BY trader_id, symbol;
-- Output: "SEARCH big_trades USING INDEX" → O(log n), fast
```

### EXPLAIN ANALYZE (PostgreSQL)

```sql
-- PostgreSQL version with actual execution time
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT ...;

-- Read the output:
-- "Seq Scan" = full table scan = slow for large tables
-- "Index Scan" = using index = fast
-- "Rows Removed by Filter" = how many rows were scanned and discarded
-- actual time=start..end  rows=N  (N should be close to estimated)
```

### Index Design Rules

```sql
-- Put equality columns first, range columns second
CREATE INDEX idx_trades_trader_time ON trades(trader_id, trade_time);
-- WHY: WHERE trader_id = 'T001' AND trade_time > '...' uses both columns

-- Covering index — include all columns the query needs
CREATE INDEX idx_trades_covering
  ON trades(symbol, status)
  INCLUDE (trader_id, quantity, price);   -- PostgreSQL only

-- Check which indexes exist
SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='trades';
-- PostgreSQL:
SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'trades';
```

### Common Performance Patterns for Trading

| Query Pattern | Recommended Index |
|---------------|-------------------|
| Fills by trader in time range | `(trader_id, trade_time)` |
| All fills for a symbol | `(symbol, status)` |
| Daily P&L report | `(trade_time, status)` |
| Position reconciliation | `(trader_id, symbol, status)` |

---

## S-06 — Concurrent Writes & Locking

### PostgreSQL Locking Patterns

```sql
-- Pessimistic locking — lock the row before updating
BEGIN;
  SELECT net_qty, avg_price
  FROM positions
  WHERE trader_id = 'T001' AND symbol = 'AAPL'
  FOR UPDATE;               -- acquires row-level lock

  UPDATE positions
  SET net_qty    = net_qty + 100,
      updated_at = NOW()
  WHERE trader_id = 'T001' AND symbol = 'AAPL';
COMMIT;

-- Upsert — atomic insert-or-update
INSERT INTO positions (trader_id, symbol, net_qty, avg_price, updated_at)
VALUES ('T001', 'AAPL', 100, 185.50, NOW())
ON CONFLICT (trader_id, symbol) DO UPDATE
  SET net_qty    = positions.net_qty + EXCLUDED.net_qty,
      updated_at = NOW();

-- Detect long-running locks
SELECT pid, query, state, wait_event_type, wait_event,
       NOW() - query_start AS duration
FROM pg_stat_activity
WHERE state != 'idle'
  AND query_start < NOW() - INTERVAL '30 seconds';

-- Kill a blocking query
SELECT pg_cancel_backend(<pid>);     -- graceful
SELECT pg_terminate_backend(<pid>);  -- force
```

### Transaction Isolation Levels

| Level | Dirty Read | Non-Repeatable Read | Phantom Read | Use in Trading |
|-------|-----------|---------------------|--------------|----------------|
| READ UNCOMMITTED | Yes | Yes | Yes | Never |
| READ COMMITTED | No | Yes | Yes | Default for most queries |
| REPEATABLE READ | No | No | Yes | P&L calculations |
| SERIALIZABLE | No | No | No | Critical position updates |

### Deadlock Prevention

```sql
-- Always acquire locks in the same order (by primary key ascending)
-- Thread A: lock T001, then T002
-- Thread B: lock T001, then T002  ← consistent order = no deadlock

-- WRONG (can deadlock):
-- Thread A: lock AAPL, then GOOGL
-- Thread B: lock GOOGL, then AAPL

-- PostgreSQL detects deadlocks automatically and kills one transaction
-- Look for: ERROR: deadlock detected
-- DETAIL: Process X waits for ShareLock on transaction Y
```

---

## Quick SQL Patterns Reference

```sql
-- Count distinct values
SELECT COUNT(DISTINCT trader_id) FROM trades;

-- Conditional count
SELECT COUNT(CASE WHEN status = 'FILLED' THEN 1 END)    AS fills,
       COUNT(CASE WHEN status = 'REJECTED' THEN 1 END)  AS rejects
FROM trades;

-- Top N per group (PostgreSQL)
SELECT * FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY desk ORDER BY total_notional DESC) AS rn
  FROM trader_totals
) t WHERE rn <= 3;

-- Date filtering
SELECT * FROM trades
WHERE trade_time >= '2024-01-15 09:30:00'
  AND trade_time <  '2024-01-15 10:00:00';

-- NULL handling
SELECT COALESCE(net_qty, 0) AS net_qty FROM positions;   -- replace NULL with 0
SELECT NULLIF(quantity, 0) AS qty FROM trades;           -- return NULL if 0
```

---

## Escalation Criteria

| Condition | Action |
|-----------|--------|
| Deadlock storm (many transactions killed) | Escalate — application lock ordering bug |
| Position table diverging from trades systematically | Escalate dev — position update logic bug |
| Query that was fast is suddenly slow | Check for missing index, table stats, or plan regression |
| Database connections exhausted | Check connection pool config; may need PgBouncer |
| Replication lag growing on read replicas | Escalate infra — replica falling behind primary |
