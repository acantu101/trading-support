# Runbook: Airflow & Pipelines

## Overview

This runbook covers Apache Airflow DAG failures, SLA misses, sensor blocks, backfill operations, and market data pipeline orchestration for trading infrastructure. All scenarios map to `lab_airflow.py` (AF-01 through AF-10).

---

## Quick Reference — Key Commands

```bash
# List all DAGs
airflow dags list

# Trigger a DAG run
airflow dags trigger <dag_id>
airflow dags trigger <dag_id> --conf '{"date": "2024-01-15"}'

# Check DAG run status
airflow dags list-runs -d <dag_id>

# List task instances
airflow tasks list <dag_id>
airflow tasks states-for-dag-run <dag_id> <run_id>

# Manually run a specific task
airflow tasks run <dag_id> <task_id> <execution_date>

# Clear (reset) failed tasks to retry
airflow tasks clear <dag_id> -t <task_id> -s <start_date> -e <end_date>
airflow tasks clear <dag_id> --start-date 2024-01-15 --end-date 2024-01-16

# Backfill missed runs
airflow dags backfill <dag_id> -s 2024-01-10 -e 2024-01-15
```

---

## AF-01 — Debug a Failed DAG

### Symptoms
- DAG run shows `failed` state in the UI
- One or more tasks in `failed` state
- Downstream tasks in `upstream_failed` state

### Diagnosis

```bash
# 1. Find the failed DAG run
airflow dags list-runs -d eod_position_reconciliation | head -10

# 2. List task states for the failed run
airflow tasks states-for-dag-run eod_position_reconciliation <run_id>

# 3. Get the task logs
airflow tasks logs eod_position_reconciliation fetch_trades 2024-01-15T00:00:00+00:00
# Or in the UI: DAG → Graph View → click failed task → Logs

# 4. Check the scheduler logs
# Airflow scheduler logs: $AIRFLOW_HOME/logs/scheduler/

# 5. Inspect the DAG code for the task
airflow tasks show <dag_id> <task_id>
```

### Common Failure Causes

| Error | Likely Cause | Fix |
|-------|-------------|-----|
| `ConnectionError` / `OperationalError` | Database or API connection failed | Check connection credentials in Airflow Connections UI |
| `ModuleNotFoundError` | Python dependency missing in worker environment | Install package in worker venv |
| `AirflowTaskTimeout` | Task exceeded execution_timeout | Increase `execution_timeout` or optimise the task |
| `KeyError` in XCom | Upstream task didn't push expected XCom | Check upstream task logic and XCom key names |
| `FileNotFoundError` | S3/GCS file not available yet | Add a sensor upstream to wait for the file |

### Resolution

```bash
# Retry a failed task
airflow tasks clear <dag_id> -t <task_id> \
  --start-date 2024-01-15 --end-date 2024-01-16 -y

# Retry the entire failed DAG run
airflow dags list-runs -d <dag_id>   # get run_id
# In UI: click the run → Clear all failed tasks

# Mark a task as success (skip it) when it will never pass
airflow tasks states-for-dag-run <dag_id> <run_id>
# UI: click task → Mark Success (use carefully — skips the actual work)
```

---

## AF-02 — SLA Miss Investigation

### What is an SLA miss?
An SLA miss occurs when a DAG or task has not completed by its configured deadline. Airflow records this and can send an alert callback.

### Diagnosis

```bash
# Check SLA misses in the UI: Browse → SLA Misses

# Via CLI
airflow sla misses list --dag-id eod_position_reconciliation

# Find slow tasks — look at task duration history
# UI: DAG → Task Duration chart

# Check if the DAG itself took too long
airflow dags list-runs -d <dag_id> -o table | head -20
# Look at: duration column
```

### Common Causes

| Cause | Signal | Fix |
|-------|--------|-----|
| Upstream dependency slow | Earlier task duration spiked | Optimise or parallelize the slow task |
| Task queue congestion | Tasks waiting in `queued` state for workers | Scale up Celery/Kubernetes workers |
| Data volume spike | Same task taking 3x longer than usual | Partition the data; process incrementally |
| External API slow | Task waiting on external call | Add timeout + retry; consider async pattern |
| Sensor blocking slot | ExternalTaskSensor or FileSensor holding a worker slot | Use `mode='reschedule'` instead of `mode='poke'` |

### SLA Configuration in DAG

```python
from airflow import DAG
from datetime import datetime, timedelta

with DAG(
    dag_id='eod_position_reconciliation',
    schedule_interval='0 18 * * 1-5',   # 18:00 every weekday
    start_date=datetime(2024, 1, 1),
    sla_miss_callback=my_alert_function,
    default_args={
        'sla': timedelta(hours=1),       # task must complete within 1 hour of schedule
    }
) as dag:
    ...
```

---

## AF-03 — Sensor Stuck / Blocking

### Symptoms
- Task in `running` state for hours with no progress
- Workers are full but no actual work is happening
- Downstream tasks queued but never starting

### Sensor Modes

| Mode | Behaviour | Worker Slot Impact |
|------|-----------|-------------------|
| `poke` (default) | Keeps worker slot, polls every `poke_interval` | **Holds a worker slot the entire time** |
| `reschedule` | Releases worker slot between polls | **Releases slot** — preferred for long waits |

### Diagnosis

```bash
# Check if a task is stuck in 'running' (sensor poke mode)
airflow tasks states-for-dag-run <dag_id> <run_id>
# Look for task in 'running' state for much longer than expected

# Check worker queue depth
# Celery: celery -A airflow.executors.celery_executor inspect active
# Kubernetes: kubectl get pods -n airflow | grep worker
```

### Resolution

```python
# Change sensor to reschedule mode — fixes worker slot exhaustion
from airflow.sensors.filesystem import FileSensor

wait_for_file = FileSensor(
    task_id='wait_for_eod_file',
    filepath='/data/eod/trades_{{ ds }}.csv',
    poke_interval=60,          # check every 60 seconds
    timeout=3600,              # fail if not found within 1 hour
    mode='reschedule',         # IMPORTANT: release worker slot between checks
)
```

```bash
# Emergency: mark the sensor as failed to unblock downstream (if file will never arrive)
airflow tasks clear <dag_id> -t wait_for_eod_file \
  --start-date <date> --end-date <date> -y
# Then mark it as success to skip:
# UI: click task → Mark Success
```

---

## AF-04 — Backfill Missed Execution Windows

### When to use
- Scheduled runs were missed (scheduler was down)
- DAG logic was fixed and old runs need to be re-processed

### Procedure

```bash
# Backfill a date range (creates DagRuns for each missed schedule interval)
airflow dags backfill eod_position_reconciliation \
  --start-date 2024-01-10 \
  --end-date   2024-01-14

# Dry run first — see what would be created
airflow dags backfill eod_position_reconciliation \
  --start-date 2024-01-10 --end-date 2024-01-14 \
  --dry-run

# Backfill with max concurrent runs (avoid hammering downstream systems)
airflow dags backfill <dag_id> \
  --start-date 2024-01-10 --end-date 2024-01-14 \
  --max-active-runs 2

# Rerun only failed tasks in the backfill
airflow dags backfill <dag_id> \
  --start-date 2024-01-10 --end-date 2024-01-14 \
  --rerun-failed-tasks
```

### Important Considerations
- Backfills run with the execution date set to the scheduled time, not now — check your tasks handle historical dates correctly
- Coordinate with downstream consumers (data warehouse, reporting) before mass backfill
- Monitor resource usage during backfill — can overwhelm databases and APIs if too many concurrent runs

---

## AF-05 — DAG Dependencies and Trigger Rules

### Trigger rules

```python
from airflow.utils.trigger_rule import TriggerRule

# Default: all parents must succeed
task_c = PythonOperator(task_id='task_c', trigger_rule=TriggerRule.ALL_SUCCESS)

# Run even if some parents failed
task_c = PythonOperator(task_id='task_c', trigger_rule=TriggerRule.ALL_DONE)

# Run if at least one parent succeeded
task_c = PythonOperator(task_id='task_c', trigger_rule=TriggerRule.ONE_SUCCESS)

# Run only if all parents failed (error handler)
task_c = PythonOperator(task_id='task_c', trigger_rule=TriggerRule.ALL_FAILED)
```

### ExternalTaskSensor — cross-DAG dependency

```python
from airflow.sensors.external_task import ExternalTaskSensor

wait_for_upstream = ExternalTaskSensor(
    task_id='wait_for_trade_ingest',
    external_dag_id='trade_ingest_pipeline',
    external_task_id='load_trades',
    execution_delta=timedelta(hours=0),   # same execution date
    mode='reschedule',
    timeout=3600,
    poke_interval=60,
)
```

### Pass data between tasks with XCom

```python
# Push from a task
def push_record_count(**context):
    count = get_record_count()
    context['task_instance'].xcom_push(key='record_count', value=count)

# Pull in a downstream task
def check_count(**context):
    count = context['task_instance'].xcom_pull(
        task_ids='fetch_trades', key='record_count'
    )
    if count == 0:
        raise ValueError("No trades loaded — aborting reconciliation")
```

---

## DAG Best Practices

```python
with DAG(
    dag_id='eod_position_reconciliation',
    schedule_interval='0 18 * * 1-5',
    start_date=datetime(2024, 1, 1),
    catchup=False,              # don't run missed historical schedules on deploy
    max_active_runs=1,          # prevent overlapping runs
    default_args={
        'retries': 2,
        'retry_delay': timedelta(minutes=5),
        'execution_timeout': timedelta(hours=2),
        'on_failure_callback': alert_team,
    },
    tags=['trading', 'eod', 'positions'],
) as dag:
    ...
```

---

## Airflow Task State Reference

| State | Meaning |
|-------|---------|
| `queued` | Waiting for a worker to pick it up |
| `running` | Currently executing |
| `success` | Completed successfully |
| `failed` | Raised an exception |
| `upstream_failed` | A dependency failed — this task was skipped |
| `skipped` | Skipped due to branching logic |
| `up_for_retry` | Failed but has retries remaining |
| `up_for_reschedule` | Sensor in reschedule mode, waiting for next poke interval |

---

## Escalation Criteria

| Condition | Action |
|-----------|--------|
| Scheduler not creating DAG runs | Restart Airflow scheduler; check scheduler logs |
| All workers stuck — no tasks progressing | Check Celery broker (Redis/RabbitMQ) connectivity; restart workers |
| Metadata database connectivity lost | Airflow fully down — escalate infra |
| Backfill running longer than expected | Pause it; check downstream system impact before resuming |
| SLA misses accumulating across all DAGs | Worker capacity issue — scale the worker fleet |

---

## AF-06 — Market Data Ingestion DAG

Orchestrates the daily market data pipeline: feed handler → Kafka → tick storage → quality checks.

```python
# schedule_interval="15 16 * * 1-5"  — 16:15 EST weekdays (after market close)
# SLA: timedelta(minutes=45)          — alert if not done by 17:00

with DAG("market_data_ingestion", schedule_interval="15 16 * * 1-5", ...) as dag:

    check_feed_handler    = BashOperator(...)    # is feed handler alive?
    validate_kafka_lag    = PythonOperator(...)  # lag < threshold on market-data.ticks
    run_tick_storage      = BashOperator(...)    # consume Kafka → write HDF5
    run_quality_checks    = PythonOperator(...)  # gaps, crossed books, symbol coverage
    update_replay_index   = PythonOperator(...)  # update JSON index for replay website
    check_symbol_coverage = PythonOperator(...)  # alert if any symbol missing

    check_feed_handler >> validate_kafka_lag >> run_tick_storage
    run_tick_storage >> [run_quality_checks, update_replay_index]
    run_quality_checks >> check_symbol_coverage
```

### SLA configuration

```python
from datetime import timedelta

default_args = {
    'sla': timedelta(minutes=45),   # alert if task hasn't finished in 45 min
}

# SLA miss callback — fires when a task exceeds its SLA
def sla_miss_callback(dag, task_list, blocking_task_list, slas, blocking_tis):
    send_alert(f"SLA MISS: {[t.task_id for t in task_list]}")
```

---

## AF-07 — Historical Tick Replay DAG

On-demand DAG triggered by the data replay website. Users search for a symbol and date range; the website triggers this DAG and polls for completion.

```python
# schedule_interval=None   — triggered by external call only, never on schedule
# max_active_runs=10        — allow up to 10 concurrent user requests

with DAG("tick_replay", schedule_interval=None, max_active_runs=10, ...) as dag:

    validate_request = ShortCircuitOperator(
        task_id="validate_request",
        python_callable=validate_replay_request,
        # Returns False if: invalid date range, symbol not found in index, future date
        # ShortCircuitOperator skips all downstream tasks on False — no error
    )

    determine_storage_tier = PythonOperator(...)
    # HOT  (< 90 days):  read directly from HDF5 on local disk — milliseconds
    # WARM (90d–2yr):    restore from S3 — seconds
    # COLD (2yr–10yr):  restore from S3 Glacier — minutes to hours

    fetch_data    = PythonOperator(...)   # fetch from appropriate tier
    validate_data = PythonOperator(...)   # check quality, flag DEGRADED if gaps
    serve_replay  = PythonOperator(...)   # write to temp location, notify website

    validate_request >> determine_storage_tier >> fetch_data >> validate_data >> serve_replay
```

### Triggering from external systems

```python
# Website backend triggers Airflow via REST API:
import requests
response = requests.post(
    "http://airflow:8080/api/v1/dags/tick_replay/dagRuns",
    json={"conf": {"symbol": "AAPL", "start_date": "2024-01-01", "end_date": "2024-01-31"}},
    auth=("airflow", "password"),
)
dag_run_id = response.json()["dag_run_id"]
# Website polls GET /api/v1/dags/tick_replay/dagRuns/{dag_run_id} until state=success
```

---

## AF-08 — EOD Risk Report DAG

Runs at 17:00 EST weekdays. Depends on market_data_ingestion completing first — uses ExternalTaskSensor to wait.

```python
with DAG("eod_risk_report", schedule_interval="0 17 * * 1-5", ...) as dag:

    wait_for_market_data = ExternalTaskSensor(
        task_id="wait_for_market_data",
        external_dag_id="market_data_ingestion",
        external_task_id=None,          # wait for the entire DAG (not a specific task)
        execution_delta=timedelta(minutes=45),   # market_data_ingestion runs at 16:15
        # execution_delta: current DAG runs at 17:00, sensor looks for market_data_ingestion
        # that ran at 17:00 - 45min = 16:15. Without this, sensor looks for a 17:00 run
        # that doesn't exist and waits forever.
        mode="reschedule",              # release the worker slot while waiting
        timeout=3600,                   # fail after 1 hour
    )

    calculate_var       = PythonOperator(...)   # Value at Risk calculation
    check_position_limits = PythonOperator(...) # flag limit breaches

    generate_report     = PythonOperator(...)
    send_report         = EmailOperator(...)

    wait_for_market_data >> [calculate_var, check_position_limits]
    [calculate_var, check_position_limits] >> generate_report >> send_report
```

### ExternalTaskSensor: execution_delta trap

```
Common mistake: omitting execution_delta when two DAGs have different schedules.

market_data_ingestion runs at 16:15
eod_risk_report       runs at 17:00

Without execution_delta, ExternalTaskSensor looks for a market_data_ingestion
run with execution_date = 17:00. No such run exists → sensor waits forever.

With execution_delta=timedelta(minutes=45):
Sensor looks for run at 17:00 - 45min = 16:15. Correct.
```

---

## AF-09 — Regulatory Reporting Pipeline

Runs T+1 (next morning) to report previous day's trading activity to regulators.

```python
# schedule_interval="0 5 * * 2-6"  — 05:00 Tuesday-Saturday (covers Mon-Fri trading)
# retries=3, email_on_retry=True    — regulatory deadlines require reliable delivery

default_args = {
    'retries': 3,
    'retry_delay': timedelta(minutes=10),
    'email_on_retry': True,
    'email_on_failure': True,
}

with DAG("regulatory_reporting", schedule_interval="0 5 * * 2-6", ...) as dag:

    validate_trade_data = PythonOperator(...)     # check completeness of T-1 data

    # Run all three regulatory submissions in parallel
    format_finra   = PythonOperator(...)    # FINRA trade reporting
    format_sec_cat = PythonOperator(...)    # SEC CAT (Consolidated Audit Trail)
    format_cftc    = PythonOperator(...)    # CFTC swaps reporting

    validate_trade_data >> [format_finra, format_sec_cat, format_cftc]

    confirm_acks   = PythonOperator(...)    # wait for regulator acknowledgements
    archive        = PythonOperator(...)    # archive to S3 (7-year retention, SEC Rule 17a-4)

    [format_finra, format_sec_cat, format_cftc] >> confirm_acks >> archive
```

### Idempotent regulatory submissions

```python
# Use {{ ds }} (execution date) not datetime.now() — ensures backfills work correctly
def format_finra_report(**context):
    trade_date = context['ds']   # '2026-05-01' — the date being reported
    # NOT: datetime.now().date() — that would report today's data on a backfill run

# Use UPSERT to avoid duplicate submissions on retry
cursor.execute("""
    INSERT INTO submission_log (submission_id, trade_date, regulator, status, submitted_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT (submission_id) DO UPDATE SET status = excluded.status
""", (submission_id, trade_date, "FINRA", "SUBMITTED", datetime.utcnow()))
```

---

## AF-10 — Airflow Best Practices

### Always use `{{ ds }}` for task dates

```python
# WRONG — breaks on backfill, uses today's date instead of execution date
def wrong_task(**context):
    today = datetime.now().date()   # always "today" — incorrect on backfill

# RIGHT — uses the DAG's logical execution date
def correct_task(**context):
    trade_date = context['ds']      # '2026-05-01' — the scheduled execution date
    # Or: context['execution_date'] for a datetime object
```

### Use mode=reschedule on sensors

```python
# WRONG — poke mode holds a worker slot while waiting (starves other tasks)
ExternalTaskSensor(mode="poke", poke_interval=30)

# RIGHT — releases the worker slot between checks
ExternalTaskSensor(mode="reschedule", poke_interval=60, timeout=3600)
```

### Idempotent DB writes — use UPSERT

```python
# WRONG — fails on retry with duplicate key, or inserts duplicates
cursor.execute("INSERT INTO results VALUES (%s, %s)", (date, value))

# RIGHT — safe to run multiple times
cursor.execute("""
    INSERT INTO results (trade_date, value)
    VALUES (%s, %s)
    ON CONFLICT (trade_date) DO UPDATE SET value = EXCLUDED.value
""", (date, value))
```

### Anti-patterns to avoid

| Anti-pattern | Problem | Fix |
|---|---|---|
| `datetime.now()` in tasks | Returns current time, not execution date — breaks backfills | Use `{{ ds }}` or `context['ds']` |
| `enable_auto_commit=True` in consumers | Commits offset before processing — loses data on crash | Commit after write |
| Sensor in `poke` mode | Holds worker slot indefinitely — starves other DAGs | Use `mode=reschedule` |
| Re-running without idempotency | Inserts duplicates or fails with constraint violation | Use UPSERT / `INSERT ... ON CONFLICT` |
| Catching bare `except Exception` | Hides failures, DAG shows success when task actually failed | Let exceptions propagate or re-raise |
| Global variables for state | State not isolated across DAG runs | Use XCom or external storage |
