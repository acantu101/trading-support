# Runbook: FIX Protocol

## Overview

FIX (Financial Information eXchange) is the industry-standard protocol for trading communication. This runbook covers session management, message parsing, sequence number issues, and connection debugging. All scenarios map to `lab_fix.py` (FX-01 through FX-06).

---

## FIX Message Structure

Every FIX message is a sequence of `tag=value` pairs delimited by SOH (`\x01`, ASCII 1):

```
8=FIX.4.4 | 9=148 | 35=D | 49=FIRM_OMS | 56=EXCHANGE_A | 34=2 | 11=ORD-001 | 55=AAPL | 54=1 | 38=100 | 44=185.50 | 10=222
```

### Critical Tags

| Tag | Name | Notes |
|-----|------|-------|
| 8 | BeginString | Protocol version: `FIX.4.2`, `FIX.4.4`, `FIXT.1.1` |
| 9 | BodyLength | Byte count of everything between tag 9 and tag 10 |
| 35 | MsgType | Message type — see table below |
| 49 | SenderCompID | Sender's identifier |
| 56 | TargetCompID | Recipient's identifier |
| 34 | MsgSeqNum | Monotonically increasing per session |
| 52 | SendingTime | UTC timestamp: `YYYYMMDD-HH:MM:SS` |
| 10 | CheckSum | 3-digit sum of all bytes mod 256 |

### Message Types (Tag 35)

| Value | Name | Direction | Description |
|-------|------|-----------|-------------|
| A | Logon | Both | Session establishment |
| 5 | Logout | Both | Clean session termination |
| 0 | Heartbeat | Both | Keepalive — sent every `HeartBtInt` seconds |
| 1 | TestRequest | Either | Solicits a Heartbeat |
| 2 | ResendRequest | Either | Requests re-transmission of missed messages |
| 4 | SequenceReset | Either | Resets or advances SeqNum |
| D | NewOrderSingle | Client→Exchange | Submit a new order |
| 8 | ExecutionReport | Exchange→Client | Order acknowledgement, fill, or rejection |
| F | OrderCancelRequest | Client→Exchange | Cancel an open order |
| 9 | OrderCancelReject | Exchange→Client | Cancel was rejected |

### Order-Specific Tags

| Tag | Name | Values |
|-----|------|--------|
| 11 | ClOrdID | Client-assigned order ID |
| 55 | Symbol | Instrument: AAPL, GOOGL, EURUSD |
| 54 | Side | `1`=BUY, `2`=SELL |
| 38 | OrderQty | Shares/contracts |
| 40 | OrdType | `1`=Market, `2`=Limit, `3`=Stop |
| 44 | Price | Limit price (omit for market orders) |
| 59 | TimeInForce | `0`=Day, `1`=GTC, `3`=IOC, `4`=FOK |
| 39 | OrdStatus | `0`=New, `1`=PartialFill, `2`=Filled, `4`=Cancelled, `8`=Rejected |
| 32 | LastQty | Quantity filled in this report |
| 31 | LastPx | Price of this fill |
| 14 | CumQty | Total quantity filled so far |
| 6 | AvgPx | Average fill price |

---

## FIX Session Lifecycle

```
CLIENT                           EXCHANGE
  |                                  |
  |-- Logon (35=A) --------------→  |    SeqNum=1
  |←-- Logon (35=A) --------------- |    SeqNum=1
  |                                  |
  |-- NewOrderSingle (35=D) ------→ |    SeqNum=2
  |←-- ExecutionReport (35=8) ------ |    SeqNum=2  (ACK / Fill)
  |                                  |
  |-- Heartbeat (35=0) -----------→ |    every HeartBtInt seconds
  |←-- Heartbeat (35=0) ----------- |
  |                                  |
  |-- Logout (35=5) --------------→ |    SeqNum=N
  |←-- Logout (35=5) -------------- |    SeqNum=N
```

### Logon Settings

```ini
[SESSION]
BeginString=FIX.4.4
SenderCompID=FIRM_OMS
TargetCompID=EXCHANGE_A
HeartBtInt=30           ; seconds between heartbeats
ResetOnLogon=Y          ; reset SeqNum to 1 on each logon
StartTime=08:00:00      ; session active window
EndTime=17:30:00
```

---

## FX-02 — Sequence Number Gap

### Symptoms
- Exchange sends `ResendRequest (35=2)` — it missed messages
- Session logs: `MsgSeqNum too low — expected N, got M`
- FIX engine logs `REJECT: SequenceReset required`

### How SeqNums work
- Both sides maintain independent, monotonically increasing counters
- If a gap is detected, the receiver sends `ResendRequest` for the missing range
- The sender replays the original messages with `PossDupFlag (43)=Y`

### Diagnosis

```bash
# Check session logs for sequence number errors
grep -i "seqnum\|sequence\|resend\|reset" fix_session.log

# Current stored SeqNums (QuickFIX/J example)
cat /var/fix/sessions/FIRM_OMS-EXCHANGE_A.seqnums
# Output: SenderSeqNum=245, TargetSeqNum=312
```

### Resolution

```bash
# Option 1: If both sides agree — let ResendRequest/replay handle it automatically
# Most FIX engines do this transparently

# Option 2: Reset sequence numbers (only during maintenance window or on reconnect)
# In QuickFIX config:
# ResetOnLogon=Y   — resets to 1 every logon
# ResetOnLogout=Y  — resets to 1 every clean logout
# ResetOnDisconnect=Y

# Option 3: Manual SeqNum reset in the FIX engine admin console
# QuickFIX/J: call Session.setNextSenderMsgSeqNum(1)

# Option 4: Send SequenceReset-GapFill (35=4, GapFillFlag=Y)
# Used when original messages are unavailable for replay
```

### Key Rule
**Never reset SeqNums unilaterally** — both sides must agree. Always coordinate with the exchange's session management team before manually resetting outside of a Logon with `ResetOnLogon=Y`.

---

## FX-03 — Session Logon/Logout Debugging

### Logon Rejected

```bash
# Check the exchange's rejection reason in the Logon response
grep "35=A" fix_session.log | grep "58="   # tag 58 = Text (rejection reason)

# Common reasons:
#   "Invalid CompID"          → wrong SenderCompID/TargetCompID
#   "Invalid password"        → check tag 554 (Password)
#   "SeqNum too low"          → reset SeqNums before logon
#   "Outside session hours"   → check StartTime/EndTime
```

### Heartbeat Timeout

```bash
# If no heartbeat received in HeartBtInt + tolerance:
# Engine sends TestRequest (35=1, tag 112=TestReqID)
# If no Heartbeat response → session declared dead → disconnect

# Diagnose: check timing between heartbeats in logs
grep "35=0\|35=1" fix_session.log | awk '{print $1, $2}'

# Common causes:
# - Network latency spike exceeding HeartBtInt
# - Exchange-side processing delay
# - Clock skew between client and exchange (SendingTime check)

# SendingTime (tag 52) must be within 2 minutes of receiver's clock
# Fix: ensure NTP is running on your servers
timedatectl status
ntpq -p
```

---

## FX-05 — Heartbeat Timeout Investigation

### Diagnosis

```bash
# Calculate time between heartbeats
grep "35=0" fix_engine.log | \
  awk '{match($0, /52=([0-9-]+:[0-9:]+)/, a); print a[1]}' | \
  head -20

# Check for network latency at same time
grep "09:30:" /var/log/network_latency.log | awk '{if ($NF > 100) print}'

# Check NTP sync
timedatectl show --property=NTPSynchronized
```

### Common Fixes

```bash
# Increase HeartBtInt if network is occasionally slow
HeartBtInt=60   # 60 seconds instead of 30

# Or tune the tolerance window (QuickFIX setting)
# LogonTimeout=30
# LogoutTimeout=5
```

---

## Parsing FIX Messages (Python)

```python
def parse_fix(raw: str) -> dict:
    """Parse a FIX message string into tag→value dict."""
    return {
        tag: val
        for pair in raw.split('\x01')
        if '=' in pair
        for tag, val in [pair.split('=', 1)]
    }

# Decode a raw FIX message
raw = "8=FIX.4.4\x019=148\x0135=D\x0149=FIRM\x0155=AAPL\x0154=1\x0138=100\x0144=185.50\x0110=222\x01"
fields = parse_fix(raw)

msg_types = {'D': 'NewOrderSingle', '8': 'ExecutionReport', 'A': 'Logon', '0': 'Heartbeat'}
sides = {'1': 'BUY', '2': 'SELL'}

print(f"MsgType: {msg_types.get(fields['35'])}")
print(f"Symbol:  {fields.get('55')}")
print(f"Side:    {sides.get(fields.get('54'))}")
print(f"Qty:     {fields.get('38')}")
print(f"Price:   {fields.get('44')}")
```

### View raw FIX traffic (replace SOH for readability)

```bash
# tcpdump — replace SOH (\x01) with | for readability
sudo tcpdump -i eth0 port 9001 -A -n 2>/dev/null | \
  tr '\001' '|' | grep "^8=FIX"

# Or from log file
cat fix_messages.log | tr '\001' '|'
```

---

## Common FIX Issues Quick Reference

| Issue | Diagnosis | Fix |
|-------|-----------|-----|
| Session not establishing | Check SenderCompID/TargetCompID match | Correct CompIDs in config |
| SeqNum gap | `35=2` ResendRequest in logs | Let engine replay, or coordinate reset |
| Order rejected with `35=8, 39=8` | Check tag 58 (Text) for reason | Fix order parameters |
| Heartbeat timeout | Check network latency, NTP sync | Increase HeartBtInt, fix NTP |
| Logon rejected | Check exchange rejection text in `35=A` reply | Correct credentials / SeqNums |
| SendingTime rejected | Clock skew > 2 minutes | Sync NTP immediately |
| CheckSum mismatch | Message corruption in transit | Check encoding layer, SSL inspection |

---

## Escalation Criteria

| Condition | Action |
|-----------|--------|
| Exchange continuously rejecting Logon with valid credentials | Escalate to exchange connectivity team |
| Messages being sent but exchange not acknowledging | Capture tcpdump and send to exchange |
| SeqNum gap that cannot be replayed | Exchange session reset required — coordinate maintenance window |
| Orders sent but no ExecutionReports arriving | Check exchange-side processing; may need exchange support ticket |
