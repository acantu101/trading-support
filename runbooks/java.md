# Runbook: Java / JVM

## Overview

This runbook covers JVM troubleshooting for a trading system support engineer. You will not write Java code, but you will read stack traces, GC logs, thread dumps, and OOM errors regularly. All scenarios map to `lab_java.py` (J-01 through J-04).

---

## J-01 — Reading a Java Stack Trace

### Structure of a stack trace

```
2026-04-28 09:31:42.881 ERROR [order-router] Unhandled exception

java.lang.NullPointerException: Cannot invoke "Quote.getBidPrice()" because "quote" is null
    at com.drw.trading.router.OrderRouter.calculateSlippage(OrderRouter.java:287)  ← crash site
    at com.drw.trading.router.OrderRouter.routeOrder(OrderRouter.java:142)
    at com.drw.trading.router.OrderRouter.processNewOrder(OrderRouter.java:98)
    at com.drw.trading.fix.FIXSessionHandler.onMessage(FIXSessionHandler.java:201)
    ...

Caused by: com.drw.trading.market.MarketDataUnavailableException: No quote for AAPL
    at com.drw.trading.market.MarketDataCache.getQuote(MarketDataCache.java:156)  ← root cause
    at com.drw.trading.router.OrderRouter.calculateSlippage(OrderRouter.java:284)
    ... 7 more
```

### How to read it

| Part | What it means |
|---|---|
| First line | Exception type and message — what crashed |
| `at` lines | Call stack — **top = crash site**, bottom = entry point |
| `Caused by:` | The underlying exception — **always find the first `Caused by:` — that is the real root cause** |
| Line numbers | `OrderRouter.java:287` — go directly to that line in source |
| Thread name | `[order-router]` or `[main-thread]` — which thread crashed |

### Common exception types

| Exception | Meaning | Trading context |
|---|---|---|
| `NullPointerException` | Called a method on a null object | Missing market data, uninitialized cache |
| `ClassCastException` | Cast to wrong type | Wrong message type handler |
| `ArrayIndexOutOfBoundsException` | Accessed array past its end | Malformed FIX message parsing |
| `IllegalArgumentException` | Method received invalid input | Bad order parameters |
| `IllegalStateException` | Method called at wrong time | Order submitted before FIX session established |
| `ConcurrentModificationException` | Collection modified during iteration | Thread safety issue |
| `SocketException: Connection reset` | TCP connection dropped | Exchange disconnect |
| `TimeoutException` | Operation took too long | DB query, downstream service |

### Reading context lines after the stack trace

Always read the log lines immediately after the stack trace — they often contain the order ID, symbol, and what the service was doing:

```
2026-04-28 09:31:42.882 INFO  Context: order=ORD-4821 symbol=AAPL side=BUY qty=500
2026-04-28 09:31:42.883 INFO  MarketDataCache last updated: 09:29:11 (151 seconds ago)
```

These context lines tell you the business impact and confirm the root cause (stale cache).

---

## J-02 — GC Log Analysis

### GC log format (G1GC, Java 11+)

```
[2026-04-28T09:31:45.882+0000][gc] GC(9) Pause Full (System.gc()) 3891M->312M(4096M) 4821.3ms
                                          ^         ^               ^           ^        ^
                                          GC #      type            heap before heap max  pause duration
```

### GC event types

| Type | Pause | Meaning |
|---|---|---|
| `Pause Young (Normal)` | Short (10-50ms) | Cleared short-lived objects — normal, expected |
| `Pause Young (Concurrent Start)` | Short | Starting a concurrent mark cycle — normal |
| `Pause Mixed` | Short | Clearing young + some old regions — normal |
| `Pause Full` | **LONG (seconds)** | Full garbage collection — **stop the world — bad** |
| `Concurrent Mark Cycle` | None | Background marking — does not pause threads |

### Stop-the-world indicators

```
Pause Full (System.gc())    ← triggered by explicit System.gc() call in code
Pause Full (Allocation Failure) ← heap full, G1GC could not keep up
GC overhead limit exceeded  ← spending >98% time in GC
```

Any `Pause Full` during trading hours is a P1 incident signal.

### Heap growth pattern = memory leak

```
GC(0)  512M  → 498M   (14M reclaimed)    — normal
GC(3) 1280M  → 1201M  (79M reclaimed)    — growing
GC(7) 2048M  → 1987M  (61M reclaimed)    — growing, less reclaimed each time
GC(9) 3891M  → 312M   FULL GC (4821ms)   — nearly full, forced full collection
```

Heap floor rising after each GC cycle = objects not being released = memory leak.

### GC tuning flags

```bash
# Show GC logs (add to JVM startup)
-Xlog:gc*:file=/var/log/app-gc.log:time,uptime:filecount=5,filesize=20m

# Common sizing flags
-Xmx8g          # max heap 8GB
-Xms8g          # initial heap (set equal to max to avoid resizing)
-XX:+UseG1GC    # G1 garbage collector (default Java 9+)
-XX:+UseZGC     # ZGC — sub-millisecond pauses, Java 15+ (best for trading)

# Crash safety
-XX:+HeapDumpOnOutOfMemoryError
-XX:HeapDumpPath=/var/log/dumps/
-XX:+ExitOnOutOfMemoryError   # crash fast instead of thrashing
```

### GC collector comparison

| Collector | Max pause | Best for | Flag |
|---|---|---|---|
| G1GC | ~100ms (normal), seconds (Full GC) | General purpose, default | `-XX:+UseG1GC` |
| ZGC | <1ms even at 100GB heap | Low-latency trading | `-XX:+UseZGC` |
| Shenandoah | <10ms | Red Hat JDK | `-XX:+UseShenandoahGC` |
| Serial/Parallel | Seconds | Batch jobs only | Avoid for services |

---

## J-03 — Thread Dump Analysis

### Capture a thread dump

```bash
# Method 1: SIGQUIT (prints to stdout/log, non-destructive)
kill -3 <java-pid>

# Method 2: jstack (formatted output)
jstack <java-pid> > /tmp/threaddump.txt

# Method 3: via kubectl for a pod
kubectl exec -n trading <pod-name> -- sh -c 'kill -3 $(pgrep java)'

# Find the Java PID
ps aux | grep java
pgrep -f order-router
```

### Thread states

| State | Meaning | Action |
|---|---|---|
| `RUNNABLE` | Thread is executing | Normal |
| `BLOCKED` | Waiting for a `synchronized` lock held by another thread | Check for deadlock |
| `WAITING` | Waiting indefinitely (Object.wait(), LockSupport.park()) | Check what it's waiting for |
| `TIMED_WAITING` | Waiting with timeout (Thread.sleep(), wait(ms)) | Normal if short duration |

### Deadlock detection

```
=== DEADLOCK DETECTED ===
"position-update-thread" waiting to lock <0x...> (PositionCache)
  held by "risk-calc-thread"
"risk-calc-thread" waiting to lock <0x...> (OrderBook)
  held by "position-update-thread"
```

**Root cause:** inconsistent lock ordering.  
**Fix:** both threads must acquire locks in the same order (always `PositionCache` before `OrderBook`).

### Finding the deadlock in a thread dump

1. Search for `DEADLOCK DETECTED` at the top — JVM prints this automatically
2. Find threads in `BLOCKED` state
3. Match `waiting to lock <0xADDRESS>` with `locked <0xADDRESS>` in other threads
4. Trace the cycle — A waits for B's lock, B waits for A's lock

### Misleading health endpoints

A health endpoint running in its own thread (`http-health-1`) will still respond `200 OK` even when the main application threads are deadlocked. Always check the thread dump — do not trust the health check alone when investigating a hung service.

### Immediate remediation

```bash
# Force restart the pod — threads are deadlocked, graceful shutdown will also hang
kubectl delete pod <pod-name> -n trading --force --grace-period=0

# K8s will restart the pod automatically (Deployment controller)
# In-flight messages will be reprocessed from Kafka (at-least-once delivery)
```

---

## J-04 — OutOfMemoryError

### OOM types and causes

| OOM Type | Cause | Fix |
|---|---|---|
| `Java heap space` | Objects not being GC'd — most common | Fix memory leak or increase `-Xmx` |
| `GC overhead limit exceeded` | Spending >98% time in GC | Same as heap space |
| `Metaspace` | Too many classes loaded | `-XX:MaxMetaspaceSize=512m`, fix class loader leak |
| `Direct buffer memory` | Off-heap NIO buffers (Kafka, Netty, FIX engines) | `-XX:MaxDirectMemorySize=1g`, fix buffer pooling |
| `Unable to create new native thread` | OS thread limit reached | Reduce thread pool sizes, check for thread leaks |

### Heap dump analysis

```bash
# Heap dump is written automatically with -XX:+HeapDumpOnOutOfMemoryError
# File: /var/log/dumps/java_pid12847.hprof

# Copy from pod before it restarts
kubectl cp trading/<pod-name>:/var/log/dumps/java_pid12847.hprof ./

# Analyse with Eclipse MAT (Memory Analyzer Tool)
# Look for: largest retained heap objects (usually the leak)
# Common finding: ArrayList or HashMap with millions of entries, never cleared
```

### Trading OOM patterns

| Service | Common OOM cause |
|---|---|
| Market data feed handler | Unbounded tick cache — no TTL or max size |
| Risk engine | Historical position snapshots never evicted |
| FIX session handler | Message log growing indefinitely in memory |
| Order router | Pending order map — orders never removed after fill/cancel |

### OOM prevention

```java
// BAD: unbounded cache
Map<String, List<Tick>> tickCache = new HashMap<>();

// GOOD: bounded cache with TTL (using Caffeine)
Cache<String, List<Tick>> tickCache = Caffeine.newBuilder()
    .maximumSize(100_000)
    .expireAfterWrite(60, TimeUnit.SECONDS)
    .build();
```

---

## JVM Flags Quick Reference

```bash
# Heap
-Xmx8g -Xms8g                      # 8GB heap, fixed size (no resizing pauses)

# GC
-XX:+UseZGC                         # best for trading (sub-ms pauses)
-Xlog:gc*:file=/var/log/gc.log      # enable GC logging

# OOM safety
-XX:+HeapDumpOnOutOfMemoryError
-XX:HeapDumpPath=/var/log/dumps/
-XX:+ExitOnOutOfMemoryError         # crash fast, let K8s restart

# Performance
-XX:+UseStringDeduplication         # reduce duplicate String memory
-XX:+OptimizeStringConcat
```

---

## Escalation Criteria

| Condition | Action |
|---|---|
| Repeated Full GC pauses during market hours | Escalate dev — memory leak investigation needed |
| OOM with heap dump generated | Share heap dump path with dev team for analysis |
| Deadlock detected (threads permanently blocked) | Restart pod immediately, share thread dump with dev |
| GC overhead limit exceeded continuously | Increase heap as temporary fix, escalate for root cause |
| Thread count growing unbounded | Thread leak — escalate dev |
