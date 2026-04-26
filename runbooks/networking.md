# Runbook: Networking

## Overview

This runbook covers network connectivity, traffic capture, DNS, multicast, and latency triage for trading systems. All scenarios map to `lab_networking.py` (N-01 through N-05).

---

## Diagnostic First Steps

Always triage bottom-up through the OSI stack:

```
L3 → ping / ip route    Is the host reachable at all?
L4 → nc / ss            Is the port open?
L7 → curl / telnet      Is the service responding?
DNS → dig / nslookup    Does the name resolve?
```

---

## N-01 — No Connectivity to Exchange

### Symptoms
- Trading application cannot connect to exchange
- FIX session logs show `Connection refused` or timeout
- Orders not being sent

### Diagnosis

```bash
# Step 1: L3 — is the host reachable?
ping -c 4 <exchange-ip>
traceroute -n <exchange-ip>

# Step 2: L4 — is the port open?
nc -zv <exchange-ip> 4001          # exits 0 if open, 1 if refused/timeout
ss -tlnp | grep :4001              # confirm something is listening

# Step 3: check local routing table
ip route
ip route get <exchange-ip>         # which interface would packets use?

# Step 4: DNS — can we resolve the hostname?
dig exchange-a.trading.internal
nslookup exchange-a.trading.internal

# Step 5: check local hosts file overrides
grep "trading.internal" /etc/hosts
```

### Common Causes and Fixes

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| ping fails, traceroute drops | Firewall or route missing | Check iptables rules; add route |
| ping works, nc refused | Nothing listening on port | Wrong port; app not started |
| ping works, nc times out | Firewall blocking port | Check iptables / security groups |
| nc works, app can't connect | DNS resolution failure | Fix /etc/resolv.conf or /etc/hosts |

### Firewall Check

```bash
# Check iptables rules
sudo iptables -L -n -v | grep <port>
sudo iptables -L INPUT -n -v

# Check for DROP rules
sudo iptables -L -n -v | grep DROP

# Temporarily allow a port (test only)
sudo iptables -I INPUT -p tcp --dport 4001 -j ACCEPT
```

---

## N-02 — Capture and Inspect Traffic

### When to use
- Application reports sending orders but exchange not receiving them
- Need to confirm packets are actually leaving the host
- Debugging SSL/TLS or protocol-level issues

### tcpdump Quick Reference

```bash
# Capture on loopback, port 4001
sudo tcpdump -i lo port 4001 -n

# Capture only TCP SYN (connection attempts)
sudo tcpdump -i lo 'tcp[tcpflags] & tcp-syn != 0' -n

# Capture any interface, filter by destination IP
sudo tcpdump -i any dst host 10.0.1.50 -n

# Save to file for offline analysis
sudo tcpdump -i eth0 port 4001 -w /tmp/capture.pcap

# Read back from file
sudo tcpdump -r /tmp/capture.pcap -n -A    # -A = ASCII output

# FIX protocol traffic (port 9001)
sudo tcpdump -i eth0 port 9001 -A -n | grep "8=FIX"
```

### What to look for

```
SYN               → client is trying to connect
SYN + SYN-ACK     → server received the SYN
SYN + no reply    → server not listening, or firewall blocking
RST               → connection refused by server
Data + FIN        → clean session close
Retransmit        → packet loss, congestion, or dead server
```

### Checking connection states

```bash
# All established connections
ss -tn state established

# Connections to a specific port
ss -tn dst :4001

# Time-wait (lingering closed connections)
ss -tn state time-wait | wc -l    # high count = connection churn
```

---

## N-03 — UDP Multicast (Market Data)

### Symptoms
- Market data feed stops updating
- Application not receiving price ticks
- Other subscribers receiving data fine (isolates to this host)

### Diagnosis

```bash
# Check multicast group memberships on this host
ip maddr show
netstat -g

# Is traffic arriving at all?
sudo tcpdump -i any 'dst host 224.1.1.1' -n
sudo tcpdump -i lo multicast -n

# Check for dropped packets on interface
ip -s link show eth0
# Look for: RX dropped > 0

# Check receive buffer size (may be too small for burst traffic)
cat /proc/sys/net/core/rmem_max
cat /proc/sys/net/core/rmem_default
```

### Join a multicast group (Python)

```python
import socket, struct

GROUP = '224.1.1.1'
PORT  = 5007

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(('', PORT))

mreq = struct.pack('4sL', socket.inet_aton(GROUP), socket.INADDR_ANY)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

while True:
    data, addr = sock.recvfrom(1024)
    print(f"TICK: {data.decode()}")
```

### Why UDP multicast in trading
- No per-subscriber connection overhead — one sender reaches thousands of receivers
- Lower latency than TCP (no handshake, no retransmit delay)
- A 10ms TCP retransmit is worse than a missed tick for market data
- Missed ticks are handled by sequence number gap detection + replay requests

### Common Fixes

```bash
# Increase receive buffer to reduce drops
sudo sysctl -w net.core.rmem_max=26214400
sudo sysctl -w net.core.rmem_default=26214400

# Verify interface has multicast enabled
ip link show eth0 | grep MULTICAST

# Check IGMP membership reported to router
cat /proc/net/igmp
```

---

## N-04 — Broken DNS Resolution

### Symptoms
- Application cannot connect by hostname but IP address works
- `nslookup` / `dig` returns SERVFAIL or times out
- Only started failing after a network change

### Diagnosis

```bash
# What DNS servers are configured?
cat /etc/resolv.conf

# Test current DNS
dig db-primary.trading.internal
nslookup db-primary.trading.internal

# Test a specific DNS server directly
dig @10.0.0.53 db-primary.trading.internal    # internal DNS
dig @8.8.8.8 google.com                       # public DNS (sanity check)

# What is the resolution order?
cat /etc/nsswitch.conf | grep hosts
# "files dns" means /etc/hosts is checked BEFORE DNS

# Check /etc/hosts for overrides
grep "trading.internal" /etc/hosts

# Is the nameserver actually reachable?
ping -c 2 <nameserver-ip>
nc -zvu <nameserver-ip> 53
```

### Common resolv.conf Problems

```bash
# Broken — unreachable DNS server
nameserver 192.168.99.99        # this IP is not reachable

# Fixed — internal DNS with fallback
nameserver 10.0.0.53
nameserver 8.8.8.8
search trading.internal         # allows short names like "db-primary"
```

### Short-term workaround

```bash
# Add static entries to /etc/hosts
echo "10.0.1.20  db-primary.trading.internal" | sudo tee -a /etc/hosts
echo "10.0.1.10  kafka-broker-1.trading.internal" | sudo tee -a /etc/hosts

# Verify
ping -c 1 db-primary.trading.internal
```

### Permanent Fix

```bash
# Edit resolv.conf (may be overwritten by DHCP — see below)
sudo vim /etc/resolv.conf

# To prevent DHCP from overwriting resolv.conf on Ubuntu/Debian
sudo systemctl disable systemd-resolved
# Or configure the correct DNS via netplan/NetworkManager
```

---

## N-05 — Latency Spike Investigation

### Symptoms
- Trader reports RTT spikes at market open (09:30)
- Normal latency ~200µs, spikes to 5ms+
- Order acknowledgement time degrades during peak throughput

### Diagnosis

```bash
# Baseline RTT measurement
ping -c 100 -i 0.1 <exchange-ip>   # look at min/avg/max/mdev

# TCP connect latency to exchange
python3 - <<'EOF'
import socket, time, statistics
latencies = []
for _ in range(20):
    t0 = time.perf_counter()
    s = socket.socket(); s.settimeout(2)
    s.connect(('exchange-ip', 4001)); s.recv(256); s.close()
    latencies.append((time.perf_counter() - t0) * 1000)
    time.sleep(0.1)
print(f"Min: {min(latencies):.2f}ms  Max: {max(latencies):.2f}ms  P99: {sorted(latencies)[int(len(latencies)*0.99)]:.2f}ms")
EOF

# CPU contention at market open?
sar -u 1 10    # install sysstat if missing

# NIC interrupt coalescing
ethtool -c eth0   # rx-usecs > 0 = batching interrupts (adds latency)

# Check for dropped packets
ip -s link show eth0   # RX dropped > 0

# OS scheduler latency (requires RT kernel tools)
cyclictest -t1 -p80 -n -i200 -l10000
```

### Root Cause Hypotheses

| Hypothesis | Check | Fix |
|-----------|-------|-----|
| CPU contention at market open | `sar -u 1 10` between 09:28-09:32 | Isolate trading threads: `taskset`, `cgroups` |
| JVM GC pause | `grep GC /var/log/app/gc.log` | Tune heap: `-XX:MaxGCPauseMillis=1`, G1GC |
| NIC interrupt coalescing | `ethtool -c eth0` (rx-usecs > 0) | `ethtool -C eth0 rx-usecs 0` |
| Switch microburst | Pull switch port counters with network team | QoS queue priority for trading traffic |
| OS scheduler | `cyclictest` p99 > 100µs | PREEMPT_RT kernel, `isolcpus=` boot param |

### Manager summary template
> "RTT 200µs → 5ms spike at 09:30. Top suspects: CPU contention from order burst,
> JVM GC pause, NIC interrupt coalescing. Next: pull `sar` CPU data and GC logs
> from 09:29-09:31 to confirm."

---

## Key Concepts Cheat Sheet

| Concept | Notes |
|---------|-------|
| TCP three-way handshake | SYN → SYN-ACK → ACK. No SYN-ACK = server not listening |
| TIME_WAIT | Normal after close; large counts = high connection churn |
| UDP multicast TTL | `IP_MULTICAST_TTL=2` allows packets to cross 2 router hops |
| IGMP | Protocol routers use to track multicast group membership |
| NIC coalescing | Batches interrupts to reduce CPU overhead — adds latency |
| `ss` vs `netstat` | `ss` is the modern replacement; faster on large connection tables |
| resolv.conf `search` | Allows `db-primary` to resolve as `db-primary.trading.internal` |

---

## Escalation Criteria

| Condition | Action |
|-----------|--------|
| Packet loss between hosts | Escalate to network team — switch/cable issue |
| Latency spikes correlate with switch port counter drops | Network team microburst investigation |
| DNS resolution works but app still can't connect | Application-level investigation needed |
| Multicast packets visible on wire but app not receiving | Socket/application configuration bug |
