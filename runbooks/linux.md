# Runbook: Linux & Systems

## Overview

This runbook covers triage and resolution for common Linux process and systems issues in a trading environment. All scenarios map to `lab_linux.py` (L-01 through L-10).

---

## Triage Priority Order

When multiple alerts fire simultaneously, work in this order:

1. **CPU hogs** → unblocks order processing immediately
2. **Memory leaks** → reduces latency and swap pressure
3. **Zombie processes** → frees PID table slots before exhaustion
4. **Runaway log files** → clears disk alert before /tmp fills

---

## L-01 — Hung Process / CPU Spike

### Symptoms
- CPU load average above core count
- Traders report OMS unresponsive or slow order acknowledgement
- `top` shows a single process near 100% CPU

### Diagnosis

```bash
# 1. Confirm high CPU load
uptime
top -b -n 1 | head -20

# 2. Find the offending process by name
pgrep -la oms_client
ps aux --sort=-%cpu | head -10

# 3. Inspect the specific PID
ps -p <PID> -o pid,ppid,%cpu,%mem,stat,etime,cmd

# 4. Check if it is truly hung (not just busy)
strace -p <PID> -c -e trace=all   # what syscalls is it making?
ls -la /proc/<PID>/fd             # open file descriptors
```

### Resolution

```bash
# Graceful shutdown first — gives app chance to flush/close
kill -15 <PID>
sleep 5

# If still alive, force kill
kill -9 <PID>

# Confirm gone
pgrep oms_client || echo "Clear"
```

### Escalate if
- Process restarts and immediately pegs CPU again → application bug, not infra
- Multiple unrelated processes pegging CPU → kernel issue or hardware fault
- `strace` shows tight loop on a syscall → application deadlock, escalate to dev

---

## L-02 — Memory Leak

### Symptoms
- `free -h` shows available memory shrinking over time
- Swap usage increasing
- Application latency degrading (GC pressure / paging)

### Diagnosis

```bash
# 1. System-wide memory state
free -h
vmstat 1 5         # si/so columns: swap in/out — non-zero is bad

# 2. Find the leaking process
ps aux --sort=-%mem | head -10
ps -p <PID> -o pid,%mem,rss,vsz,cmd

# 3. Watch it grow
watch -n 1 'ps -p <PID> -o pid,%mem,rss,vsz'

# 4. Detailed virtual memory breakdown
awk '/Vm/ {printf "%-15s %8.1f MB\n", $1, $2/1024}' /proc/<PID>/status

# 5. Check for memory-mapped files / anonymous mappings
cat /proc/<PID>/smaps | grep -E "^(Rss|Private_Dirty|Swap):" | awk '{sum+=$2} END {print sum/1024 " MB"}'
```

### Resolution

```bash
# Kill the leaking process
kill -15 <PID>
sleep 3
kill -9 <PID> 2>/dev/null   # safety net

# Confirm memory reclaimed
free -h
```

### Key Concepts
- **RSS** (Resident Set Size): physical RAM actually in use
- **VSZ** (Virtual Size): all mapped memory including not-yet-paged pages — can be misleading
- Memory is only reclaimed once the process exits; `free` will not drop until then

### Escalate if
- Memory grows without bound even after restart → persistent leak, needs dev fix
- OOM killer has already fired: `dmesg | grep -i "oom killer"`

---

## L-03 — Log File Analysis

### Common grep/awk patterns

```bash
# Count errors
grep -c "ERROR" /path/to/app.log

# Error frequency ranked
grep "ERROR" app.log | sort | uniq -c | sort -rn

# Errors in a time window
grep "09:3[0-9]" app.log | grep "ERROR"

# Log level distribution
awk '{print $1}' app.log | sort | uniq -c | sort -rn

# Extract all OrderIDs that appear in errors
grep "ERROR" app.log | grep -oP 'OrderID=\K\S+'

# Show context around errors
grep -B2 -A3 "ERROR" app.log

# Follow live
tail -f app.log | grep --line-buffered "ERROR\|CRITICAL"
```

---

## L-04 — Port Already in Use

### Symptoms
- Deployment fails: `Address already in use` on bind
- New service cannot start

### Diagnosis

```bash
# Find what is listening on the port
ss -tlnp | grep :8080
lsof -i :8080

# Get the PID from ss output
ss -tlnp | grep :8080   # look for pid=NNNN in the last column

# Check how long it has been running
ps -p <PID> -o pid,etime,cmd

# List ALL listening ports
ss -tlnp
```

### Resolution

```bash
# Confirm it is safe to kill (check process name against deployments)
ps -p <PID> -o comm=

# Kill it
kill -15 <PID>
sleep 2

# Verify port is free
ss -tlnp | grep :8080 || echo "Port free"
```

---

## L-05 — Broken File Permissions

### Symptom
- App fails with `Permission denied` reading config or executing start script

### Diagnosis

```bash
ls -la /opt/trading/
stat config.yml          # shows octal permissions and owner
```

### Resolution

```bash
# Config: owner read/write, group read, others nothing
chmod 640 /opt/trading/config.yml

# Start script: owner full, group/others nothing, must be executable
chmod 700 /opt/trading/start.sh

# Fix ownership
sudo chown trader:trading /opt/trading/config.yml
sudo chown trader:trading /opt/trading/start.sh

# Verify
ls -la /opt/trading/
```

### Permission Cheat Sheet

| Octal | Symbolic | Use case |
|-------|----------|----------|
| 640 | rw-r----- | Config files with secrets |
| 700 | rwx------ | Private executable scripts |
| 755 | rwxr-xr-x | Public executables |
| 644 | rw-r--r-- | Public readable files |

---

## L-06 — Large Files / Disk Pressure

### Diagnosis

```bash
# How bad is it?
df -h /tmp /var/log

# Find the biggest files
find /var/log -type f -printf '%s %p\n' | sort -rn | head -10

# Directory sizes
du -sh /var/log/*/ | sort -rh | head -10

# Find log files over 100 MB
find /var/log -name "*.log" -size +100M -exec ls -lh {} \;

# Who is writing to the file right now?
lsof /var/log/trading/fix_engine_debug.log
```

### Resolution

```bash
# Stop the process writing the log
kill -15 <PID>

# Truncate without deleting (preserves open file descriptor)
truncate -s 0 /var/log/trading/fix_engine_debug.log

# WHY truncate instead of rm:
# rm removes the name but the process keeps an open fd to the inode.
# Disk usage NEVER drops until the process exits.
# truncate zeros the content while keeping the inode — disk freed immediately.

# Delete old rotated logs
find /var/log/trading -name "*.log.[0-9]*" -mtime +7 -delete
```

---

## L-07 — Failed systemd Service

### Diagnosis

```bash
# Current status + last few log lines
systemctl status riskengine.service

# Full journal output
journalctl -u riskengine.service -n 100

# Follow journal in real time
journalctl -u riskengine.service -f

# Check restart configuration
grep -i restart /etc/systemd/system/riskengine.service

# How many times has it restarted?
systemctl show riskengine.service --property=NRestarts
```

### Resolution

```bash
# DO NOT restart blindly — read the journal first
# Common root causes:
#   SIGSEGV  → segfault, needs code fix or core dump analysis
#   exit 1   → application error, check app logs
#   "too many connections" → upstream DB pool exhausted

# Fix root cause, THEN restart
systemctl restart riskengine.service

# Confirm it stays up
systemctl status riskengine.service
journalctl -u riskengine.service -f
```

### Key Insight
Blind restart loops without fixing the root cause = more failures. Always identify why it crashed before restarting.

---

## L-08 — Full One-Box Triage

### 5-minute triage checklist

```bash
# 1. How bad is load?
uptime                           # load avg vs core count

# 2. What is the bottleneck?
top -b -n 1 | head -20
# %us = user CPU   %sy = kernel   %wa = I/O wait (high = disk problem)

# 3. Top CPU consumers
ps aux --sort=-%cpu | head -10

# 4. Top memory consumers
ps aux --sort=-%mem | head -10

# 5. Any zombie processes?
ps aux | awk '$8 == "Z"'

# 6. Disk pressure?
df -h
iostat -x 1 3   # await (ms) + %util

# 7. Network errors?
ip -s link      # RX-ERR / TX-ERR non-zero = problem
netstat -i

# 8. Open file descriptor exhaustion?
ulimit -n
ls /proc/<PID>/fd | wc -l
```

### Manager summary template
> "Server load avg is X on Y cores. Top process is `Z` using X% CPU.
> I/O wait is X% — [disk issue / no disk issue].
> Recommend: kill Z first to unblock trading, then investigate root cause."

---

## L-09 — Zombie Processes

### Symptoms
- `ps aux` shows processes in `Z` (zombie) state
- PID table approaching limit (`/proc/sys/kernel/pid_max`)

### Diagnosis

```bash
# Find zombies
ps aux | awk '$8 == "Z"'
ps -eo pid,ppid,stat,cmd | grep ' Z '

# Find the parent of a zombie
ps -p <ZOMBIE_PID> -o ppid=
pgrep -la risk_engine   # find and confirm the parent
```

### Key Concept
A zombie holds a PID slot but uses **zero CPU and zero memory**. It exists only because the parent has not called `wait()`. The fix is to signal the parent, not kill the zombie directly.

### Resolution

```bash
# Option 1: Kill the parent — OS reaps all its zombie children
kill -15 <PARENT_PID>

# Option 2: Send SIGCHLD to prompt the parent to call wait()
kill -17 <PARENT_PID>

# Verify
ps aux | awk '$8 == "Z"' | wc -l   # should be 0
```

---

## L-10 — Runaway Log File

See [L-06 Disk Pressure](#l-06---large-files--disk-pressure) for the full procedure.

Key addition: if the process must keep running, rotate the log without restart:

```bash
# Send SIGHUP to most daemons — triggers log file re-open
kill -1 <PID>

# Or use logrotate with copytruncate for processes that don't support SIGHUP
```

---

## Escalation Criteria

| Condition | Action |
|-----------|--------|
| Process restarts and re-crashes immediately | Escalate to dev — application bug |
| OOM killer fired | Page on-call — production memory sizing issue |
| Multiple unrelated processes crashing | Escalate — possible kernel panic or hardware fault |
| Disk 100% full and truncate does not help | Escalate infra — need disk expansion |
| Zombie count growing unbounded | Escalate dev — parent process has wait() bug |
