#!/usr/bin/env python3
"""
Support Engineer Challenge Lab - Environment Setup
===================================================
Replicates realistic trading infrastructure scenarios for interview prep.
Run with: python3 lab_setup.py [--scenario N] [--teardown]

Scenarios:
  1. Hung OMS client consuming 100% CPU
  2. Memory leak in market data feed handler
  3. Zombie processes from crashed risk engine
  4. Log directory filling up disk (runaway fix_engine logger)
  5. Full incident — all faults active simultaneously
"""

import os
import sys
import time
import signal
import shutil
import argparse
import resource
import subprocess
import multiprocessing
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────
#  RESOURCE LIMITS
#  Keeps processes alive but safe — OOM killer
#  won't touch them, VM stays responsive.
# ─────────────────────────────────────────────
CPU_LIMIT_PERCENT  = 50       # oms_client burns at most 50% of one core
MEM_LIMIT_MB       = 256      # mdf_feed_handler caps at 256 MB
LOG_LIMIT_MB       = 50       # fix_engine log capped at 50 MB on disk

# ─────────────────────────────────────────────
#  LAB ROOT DIRECTORY STRUCTURE
# ─────────────────────────────────────────────
LAB_ROOT      = Path("/tmp/lab")
DIRS = {
    "oms":        LAB_ROOT / "oms"        / "bin",
    "oms_logs":   LAB_ROOT / "oms"        / "logs",
    "oms_config": LAB_ROOT / "oms"        / "config",
    "mdf":        LAB_ROOT / "mdf"        / "bin",
    "mdf_logs":   LAB_ROOT / "mdf"        / "logs",
    "fix":        LAB_ROOT / "fix_engine" / "bin",
    "fix_logs":   LAB_ROOT / "fix_engine" / "logs",
    "risk":       LAB_ROOT / "risk"       / "bin",
    "risk_logs":  LAB_ROOT / "risk"       / "logs",
    "var_log":    LAB_ROOT / "var"        / "log" / "trading",
    "pids":       LAB_ROOT / "run",
}

SEPARATOR = "=" * 60

# ─────────────────────────────────────────────
#  COLOUR HELPERS
# ─────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):    print(f"{GREEN}  ✓ {msg}{RESET}")
def warn(msg):  print(f"{YELLOW}  ⚠ {msg}{RESET}")
def err(msg):   print(f"{RED}  ✗ {msg}{RESET}")
def info(msg):  print(f"{CYAN}  → {msg}{RESET}")
def header(msg):
    print(f"\n{BOLD}{SEPARATOR}{RESET}")
    print(f"{BOLD}  {msg}{RESET}")
    print(f"{BOLD}{SEPARATOR}{RESET}")

# ─────────────────────────────────────────────
#  FILESYSTEM SETUP
# ─────────────────────────────────────────────
def create_directory_structure():
    header("Creating Lab Directory Structure")
    for name, path in DIRS.items():
        path.mkdir(parents=True, exist_ok=True)
        ok(f"Created {path}")

def write_config_files():
    header("Writing Config & Service Files")

    # OMS config
    oms_cfg = DIRS["oms_config"] / "oms.conf"
    oms_cfg.write_text("""\
[oms]
host            = 10.0.1.50
port            = 8080
max_connections = 200
heartbeat_ms    = 500
reconnect_delay = 5

[database]
host     = db-primary.trading.internal
port     = 5432
name     = oms_prod
pool_min = 5
pool_max = 20

[logging]
level = INFO
file  = /tmp/lab/oms/logs/oms.log
""")
    ok(f"Written {oms_cfg}")

    # FIX engine session config
    fix_cfg = DIRS["fix"] / "fix_sessions.cfg"
    fix_cfg.write_text("""\
[SESSION]
BeginString=FIX.4.4
SenderCompID=FIRM_OMS
TargetCompID=EXCHANGE_A
HeartBtInt=30
StartTime=08:00:00
EndTime=17:30:00
ResetOnLogon=Y

[SESSION]
BeginString=FIX.4.4
SenderCompID=FIRM_OMS
TargetCompID=EXCHANGE_B
HeartBtInt=30
StartTime=08:00:00
EndTime=17:30:00
""")
    ok(f"Written {fix_cfg}")

    # Populate log files with realistic trading noise
    _write_sample_logs()


def _write_sample_logs():
    log_entries = [
        "INFO  [08:01:00] OMS started, listening on port 8080",
        "INFO  [08:01:01] Connected to database pool (5 connections)",
        "INFO  [08:15:33] Order received: AAPL BUY 5000 @ LIMIT 189.50",
        "INFO  [08:15:33] Order routed to EXCHANGE_A",
        "WARN  [09:02:11] Heartbeat missed from TargetCompID=EXCHANGE_B",
        "INFO  [09:02:14] Reconnected to EXCHANGE_B",
        "ERROR [09:47:52] Order rejected: insufficient margin (OrderID=TRD-8821)",
        "WARN  [10:15:00] CPU usage above 80% threshold",
        "ERROR [10:15:03] oms_client thread not responding to heartbeat",
        "ERROR [10:15:08] oms_client thread not responding to heartbeat",
        "ERROR [10:15:13] oms_client HUNG — no response in 10s",
    ]
    oms_log = DIRS["oms_logs"] / "oms.log"
    oms_log.write_text("\n".join(log_entries) + "\n")
    ok(f"Written {oms_log}")

    # Large rotated logs to simulate disk pressure
    for i in range(1, 4):
        rotated = DIRS["var_log"] / f"trading.log.{i}"
        rotated.write_text("WARN  " * 20000 + "\n")   # ~120 KB each
        ok(f"Written rotated log {rotated.name}")


# ─────────────────────────────────────────────
#  PROCESS WORKERS  (run in background)
# ─────────────────────────────────────────────

def _cpu_spin(name: str, limit_percent: int = CPU_LIMIT_PERCENT):
    """
    Throttled CPU burn — simulates a hung oms_client pegging the CPU
    without going to 100% and triggering the OOM killer.
    Burns for (limit_percent)ms then sleeps for (100-limit_percent)ms
    per 100ms window, keeping usage at exactly limit_percent%.
    """
    try:
        import setproctitle
        setproctitle.setproctitle(name)
    except ImportError:
        pass
    burn  = limit_percent / 100        # fraction of each cycle to burn
    sleep = 1.0 - burn                 # fraction to sleep
    cycle = 0.1                        # 100ms window
    while True:
        deadline = time.time() + cycle * burn
        while time.time() < deadline:
            pass                       # busy-wait (CPU burn)
        time.sleep(cycle * sleep)      # yield (CPU idle)


def _memory_leak(name: str, chunk_mb: int = 2, interval: float = 0.5,
                 limit_mb: int = MEM_LIMIT_MB):
    """
    Capped memory leak — allocates RAM in chunks until it hits limit_mb,
    then holds steady. Simulates a leaking mdf_feed handler without
    exhausting VM memory and triggering the OOM killer.
    """
    try:
        import setproctitle
        setproctitle.setproctitle(name)
    except ImportError:
        pass
    # Set a hard virtual memory limit via the OS so the kernel enforces it
    limit_bytes = limit_mb * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes * 2, limit_bytes * 2))
    except Exception:
        pass
    held = []
    total = 0
    while True:
        if total + chunk_mb <= limit_mb:
            held.append(b"X" * (chunk_mb * 1024 * 1024))
            total += chunk_mb
        # Once capped, just hold the memory and sleep — stays visible in ps/top
        time.sleep(interval)


def _zombie_factory(name: str, count: int = 5):
    """
    Spawns children that exit immediately while the parent stays alive
    and never calls wait() — this is what creates real zombie entries.

    Key fix: we use SIG_DFL (default) NOT SIG_IGN for SIGCHLD.
    SIG_IGN on Linux tells the kernel to auto-reap children (no zombies).
    SIG_DFL leaves children in Z state until the parent calls wait().
    """
    try:
        import setproctitle
        setproctitle.setproctitle(name)
    except ImportError:
        pass

    # Use default SIGCHLD handler — do NOT ignore it or Linux auto-reaps
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)

    zombie_pids = []
    for i in range(count):
        pid = os.fork()
        if pid == 0:
            # Child: exit immediately without cleanup → becomes zombie
            os._exit(0)
        else:
            # Parent: record the child PID, never call wait()
            zombie_pids.append(pid)
            time.sleep(0.5)   # stagger so each zombie gets its own PID slot

    # Parent stays alive forever — zombies persist until parent is killed
    while True:
        time.sleep(60)


def _log_spammer(name: str, path: str, interval: float = 0.05,
                 limit_mb: int = LOG_LIMIT_MB):
    """
    Size-capped log spammer — writes rapidly until the log hits limit_mb,
    then pauses. Simulates a runaway fix_engine logger without filling
    the disk entirely. Resumes if the file is truncated (as the student
    is expected to do).
    """
    try:
        import setproctitle
        setproctitle.setproctitle(name)
    except ImportError:
        pass
    limit_bytes = limit_mb * 1024 * 1024
    counter = 0
    while True:
        try:
            if os.path.getsize(path) < limit_bytes:
                with open(path, "a") as f:
                    f.write(
                        f"[{datetime.now().isoformat()}] DEBUG market tick #{counter}: "
                        f"AAPL=189.{counter % 100:02d} GOOGL=175.{counter % 100:02d}\n"
                    )
                counter += 1
                time.sleep(interval)
            else:
                # Cap reached — pause until truncated by the student
                time.sleep(1)
        except FileNotFoundError:
            time.sleep(1)


# ─────────────────────────────────────────────
#  PID FILE HELPERS
# ─────────────────────────────────────────────
def _save_pid(name: str, pid: int):
    pid_file = DIRS["pids"] / f"{name}.pid"
    pid_file.write_text(str(pid))


def _load_pids() -> dict:
    pids = {}
    for pid_file in DIRS["pids"].glob("*.pid"):
        try:
            pids[pid_file.stem] = int(pid_file.read_text().strip())
        except Exception:
            pass
    return pids


# ─────────────────────────────────────────────
#  SCENARIO LAUNCHERS
# ─────────────────────────────────────────────

def launch_scenario_1():
    """Hung OMS client — throttled CPU spike."""
    header("Scenario 1 — Hung oms_client (CPU Spike)")
    print("  A trader reports their OMS is unresponsive.")
    print(f"  Suspected: oms_client has hung and is pegging ~{CPU_LIMIT_PERCENT}% CPU.\n")

    for i in range(2):   # Launch 2 instances for realism
        p = multiprocessing.Process(
            target=_cpu_spin, args=("oms_client", CPU_LIMIT_PERCENT), daemon=False
        )
        p.start()
        _save_pid(f"oms_client_{i}", p.pid)
        ok(f"oms_client spawned  PID={p.pid}  (capped at {CPU_LIMIT_PERCENT}% CPU)")

    print(f"""
{BOLD}── Your Tasks ──────────────────────────────────────{RESET}
  1. Find all processes named oms_client and their PIDs
       pgrep -la oms_client
       ps aux | grep oms_client

  2. Check CPU and memory usage of those PIDs
       top -p <PID>
       ps -p <PID> -o pid,ppid,%cpu,%mem,stat,cmd

  3. Kill gracefully first, then forcefully
       kill -15 <PID>          # SIGTERM
       kill -9  <PID>          # SIGKILL (if still alive after 5s)

  4. Verify the process is gone
       pgrep oms_client || echo "All clear"
       ps aux | grep [o]ms_client
""")


def launch_scenario_2():
    """Leaking mdf_feed — memory grows over time."""
    header("Scenario 2 — Memory Leak in mdf_feed_handler")
    print("  Market data latency is spiking. Engineers suspect the")
    print("  mdf_feed_handler is not releasing memory between sessions.\n")

    p = multiprocessing.Process(
        target=_memory_leak, args=("mdf_feed_handler", 2, 0.3, MEM_LIMIT_MB), daemon=False
    )
    p.start()
    _save_pid("mdf_feed_handler", p.pid)
    ok(f"mdf_feed_handler spawned  PID={p.pid}  (grows to {MEM_LIMIT_MB}MB then holds)")

    print(f"""
{BOLD}── Your Tasks ──────────────────────────────────────{RESET}
  1. Find the process and its memory footprint
       pgrep -la mdf_feed_handler
       ps -p <PID> -o pid,%mem,rss,vsz,cmd

  2. Watch memory grow in real time
       watch -n 1 'ps -p <PID> -o pid,%mem,rss,vsz'

  3. Check system-wide memory pressure
       free -h
       vmstat 1 5
       cat /proc/<PID>/status | grep -i vm

  4. Terminate the leaking process
       kill -15 <PID>

  5. Confirm memory is reclaimed
       free -h
""")


def launch_scenario_3():
    """Zombie processes from crashed risk engine."""
    header("Scenario 3 — Zombie Processes from risk_engine")
    print("  The risk calculation engine crashed mid-session.")
    print("  Child worker processes are now zombies.\n")

    p = multiprocessing.Process(target=_zombie_factory, args=("risk_engine", 5), daemon=False)
    p.start()
    _save_pid("risk_engine", p.pid)
    ok(f"risk_engine (zombie factory) spawned  PID={p.pid}")
    time.sleep(2)   # Give zombies time to appear

    print(f"""
{BOLD}── Your Tasks ──────────────────────────────────────{RESET}
  1. Find all zombie processes
       ps aux | awk '$8 == "Z"'
       ps -eo pid,ppid,stat,cmd | grep ' Z '

  2. Identify the parent (risk_engine)
       ps -p <ZOMBIE_PID> -o ppid=
       pgrep -la risk_engine

  3. Understand why zombies exist
       # A zombie means the parent hasn't called wait()
       # The zombie holds a PID slot but uses no real resources

  4. Reap the zombies by killing the parent
       kill -15 <PPID>     # parent is risk_engine
       # OR send SIGCHLD to force wait()
       kill -17 <PPID>

  5. Verify zombies are gone
       ps aux | grep ' Z '
""")


def launch_scenario_4():
    """Log directory filling up disk."""
    header("Scenario 4 — Runaway Log File (Disk Pressure)")
    print(f"  Ops alert: {LAB_ROOT}/fix_engine/logs is filling fast.")
    print("  A fix_engine process is spamming debug logs.\n")

    log_path = str(DIRS["fix_logs"] / "fix_engine_debug.log")
    p = multiprocessing.Process(
        target=_log_spammer, args=("fix_engine", log_path, 0.01, LOG_LIMIT_MB), daemon=False
    )
    p.start()
    _save_pid("fix_engine_logger", p.pid)
    ok(f"fix_engine log spammer spawned  PID={p.pid}  (capped at {LOG_LIMIT_MB}MB)")
    ok(f"Log file: {log_path}")

    print(f"""
{BOLD}── Your Tasks ──────────────────────────────────────{RESET}
  1. Check disk usage of the log directory
       du -sh {LAB_ROOT}/
       du -sh {DIRS["fix_logs"]}/
       df -h /tmp

  2. Identify the process writing to the file
       lsof {log_path}
       fuser {log_path}

  3. Watch the file grow in real time
       watch -n 1 'du -sh {log_path}'
       tail -f {log_path}

  4. Stop the runaway logger
       kill -15 <PID>

  5. Truncate the log (without deleting — service may reopen it)
       truncate -s 0 {log_path}
       > {log_path}          # bash shorthand

  6. Verify disk is recovered
       du -sh {DIRS["fix_logs"]}/
""")


def launch_scenario_5_all():
    """Full combined scenario — everything running simultaneously."""
    header("Scenario 5 — FULL INCIDENT (All Faults Active)")
    print("  Trading desk escalation: OMS down, MD feed lagging,")
    print("  risk engine crashed, disk alert firing. Triage now.\n")

    launch_scenario_1()
    time.sleep(1)
    launch_scenario_2()
    time.sleep(1)
    launch_scenario_3()
    time.sleep(1)
    launch_scenario_4()

    print(f"""
{BOLD}── Triage Order ───────────────────────────────────{RESET}
  Priority 1: Kill the CPU-hogging oms_client    → trading unblocked
  Priority 2: Free memory from mdf_feed_handler  → MD latency drops
  Priority 3: Reap zombie risk_engine children   → PID table cleaned
  Priority 4: Stop log spammer + truncate log    → disk alert clears

{BOLD}── Useful One-Liners ───────────────────────────────{RESET}
  # Full process snapshot
  ps auxf

  # Sort by CPU descending
  ps aux --sort=-%cpu | head -15

  # Sort by MEM descending
  ps aux --sort=-%mem | head -15

  # Open file handles across all lab PIDs
  lsof -p $(pgrep -d, 'oms_client|mdf_feed|risk_engine|fix_engine')

  # Interactive process explorer
  top      # then press 'M' for memory sort, 'P' for CPU sort
  htop     # if installed
""")


# ─────────────────────────────────────────────
#  TEARDOWN
# ─────────────────────────────────────────────
def teardown():
    header("Tearing Down Lab")
    pids = _load_pids()
    if not pids:
        warn("No PID files found — lab may already be clean")

    for name, pid in pids.items():
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.3)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            ok(f"Killed {name} (PID {pid})")
        except ProcessLookupError:
            warn(f"{name} (PID {pid}) already gone")
        except PermissionError:
            err(f"No permission to kill {name} (PID {pid})")

    # Also sweep any stray processes by name
    for proc_name in ["oms_client", "mdf_feed_handler", "risk_engine", "fix_engine"]:
        result = subprocess.run(["pgrep", "-f", proc_name], capture_output=True, text=True)
        for pid_str in result.stdout.strip().splitlines():
            try:
                os.kill(int(pid_str), signal.SIGKILL)
                ok(f"Force-killed stray {proc_name} PID {pid_str}")
            except Exception:
                pass

    shutil.rmtree(LAB_ROOT, ignore_errors=True)
    ok(f"Removed {LAB_ROOT}")
    print(f"\n{GREEN}{BOLD}  Lab torn down successfully.{RESET}\n")


# ─────────────────────────────────────────────
#  STATUS / CHEAT SHEET
# ─────────────────────────────────────────────
def show_status():
    header("Current Lab Status")
    pids = _load_pids()
    if not pids:
        warn("No lab processes running (or PID files missing)")
        return
    print(f"  {'NAME':<30} {'PID':<10} {'STATUS'}")
    print(f"  {'-'*30} {'-'*10} {'-'*10}")
    for name, pid in pids.items():
        try:
            os.kill(pid, 0)
            status = f"{GREEN}running{RESET}"
        except ProcessLookupError:
            status = f"{RED}dead{RESET}"
        print(f"  {name:<30} {pid:<10} {status}")
    print()


def print_cheatsheet():
    header("Quick Reference — Commands You'll Need")
    print(f"""
  {BOLD}FIND PROCESSES{RESET}
    pgrep -la <name>                      list PIDs + full cmdline
    ps aux | grep <name>                  snapshot with stats
    ps -p <PID> -o pid,ppid,%cpu,%mem,stat,cmd

  {BOLD}MONITOR IN REAL TIME{RESET}
    top -p <PID>                          watch single PID
    watch -n 1 'ps -p <PID> -o %cpu,%mem,rss'
    vmstat 1 5                            system-wide CPU/mem/io
    iostat 1 5                            disk I/O

  {BOLD}MEMORY DEEP DIVE{RESET}
    cat /proc/<PID>/status | grep -i vm   VmRSS, VmSize, VmPeak
    cat /proc/<PID>/smaps_rollup          detailed memory map
    pmap -x <PID>                         memory map

  {BOLD}SIGNALS{RESET}
    kill -15 <PID>   SIGTERM   graceful shutdown request
    kill -9  <PID>   SIGKILL   immediate kill (no cleanup)
    kill -17 <PID>   SIGCHLD   force parent to reap children
    kill -18 <PID>   SIGCONT   resume a stopped process

  {BOLD}ZOMBIE HUNTING{RESET}
    ps aux | awk '$8=="Z"'                find zombies
    ps -eo pid,ppid,stat,cmd | grep ' Z '

  {BOLD}DISK / FILES{RESET}
    df -h                                 disk usage by filesystem
    du -sh <dir>                          dir size
    lsof <file>                           who has the file open
    lsof -p <PID>                         all files a process has open
    truncate -s 0 <logfile>               zero out without deleting

  {BOLD}PROCESS TREE{RESET}
    pstree -p <PID>                       visual tree
    ps auxf                               forest view
""")


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Support Engineer Challenge Lab Setup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Scenarios:
  1  Hung oms_client consuming 100% CPU
  2  Memory leak in mdf_feed_handler
  3  Zombie processes from crashed risk_engine
  4  Runaway log file from fix_engine (disk pressure)
  5  Full incident — all faults active simultaneously
"""
    )
    parser.add_argument("--scenario", "-s", type=int, choices=[1, 2, 3, 4, 5],
                        help="Launch a specific scenario (default: show menu)")
    parser.add_argument("--teardown", "-t", action="store_true",
                        help="Kill all lab processes and remove lab files")
    parser.add_argument("--status", action="store_true",
                        help="Show status of running lab processes")
    parser.add_argument("--cheatsheet", "-c", action="store_true",
                        help="Print command cheat sheet")
    args = parser.parse_args()

    if args.teardown:
        teardown()
        return

    if args.status:
        show_status()
        return

    if args.cheatsheet:
        print_cheatsheet()
        return

    # Setup filesystem first
    create_directory_structure()
    write_config_files()

    scenario_map = {
        1: launch_scenario_1,
        2: launch_scenario_2,
        3: launch_scenario_3,
        4: launch_scenario_4,
        5: launch_scenario_5_all,
    }

    if args.scenario:
        scenario_map[args.scenario]()
    else:
        # Interactive menu
        header("Support Engineer Challenge Lab")
        print("  Select a scenario to launch:\n")
        print("    1  Hung oms_client consuming 100% CPU")
        print("    2  Memory leak in mdf_feed_handler")
        print("    3  Zombie processes from crashed risk_engine")
        print("    4  Runaway log file / disk pressure (fix_engine)")
        print("    5  FULL INCIDENT — all faults simultaneously\n")
        choice = input("  Enter scenario number (or q to quit): ").strip()
        if choice.lower() == "q":
            return
        try:
            scenario_map[int(choice)]()
        except (KeyError, ValueError):
            err("Invalid choice")
            return

    print(f"""
{BOLD}{SEPARATOR}{RESET}
{GREEN}{BOLD}  Lab is running. Work through your tasks above.{RESET}

  Useful commands while lab runs:
    python3 lab_setup.py --status       see running processes
    python3 lab_setup.py --cheatsheet   command reference
    python3 lab_setup.py --teardown     clean everything up
{BOLD}{SEPARATOR}{RESET}
""")


if __name__ == "__main__":
    main()
