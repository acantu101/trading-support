# Runbook: Airflow & Pipelines

## Overview

This runbook covers Apache Airflow DAG failures, SLA misses, sensor blocks, and backfill operations in a trading data pipeline context. All scenarios map to `lab_airflow.py` (AF-01 through AF-05).

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
