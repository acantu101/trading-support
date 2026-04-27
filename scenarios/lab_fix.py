#!/usr/bin/env python3
"""
Support Engineer Challenge Lab — FIX Protocol Setup
====================================================
Creates the environment for all FIX Protocol challenge scenarios (F1-F5).
Spins up a real FIX 4.4 acceptor (server) and provides initiator scripts,
sample message files, and broken/malformed FIX messages to debug.

Run with: python3 lab_fix.py [--scenario N] [--teardown]

SCENARIOS:
  1   F-01  FIX session lifecycle — acceptor + heartbeat exchange
  2   F-02  Decode and parse FIX messages — sample message files
  3   F-03  Diagnose a broken FIX session — bad logon, seq gap, reject
  4   F-04  Send a NewOrderSingle — Python FIX sender script
  5   F-05  FIX tag reference & execution report walkthrough
  99        ALL scenarios
"""

import os, sys, time, signal, shutil, socket, argparse, threading, subprocess
import multiprocessing
from pathlib import Path
from datetime import datetime
from common import (
    GREEN, YELLOW, RED, CYAN, BOLD, RESET, SEP,
    ok, warn, err, info, header, lab_footer,
    create_dirs as _create_dirs,
    save_pid as _save_pid, load_pids as _load_pids,
    spawn as _spawn, kill_pids, kill_strays, remove_lab_dir,
    show_status as _show_status,
)

LAB_ROOT = Path("/tmp/lab_fix")
DIRS = {
    "messages":  LAB_ROOT / "messages",
    "logs":      LAB_ROOT / "logs",
    "scripts":   LAB_ROOT / "scripts",
    "config":    LAB_ROOT / "config",
    "pids":      LAB_ROOT / "run",
}

SOH = "\x01"   # FIX field delimiter

SEPARATOR = SEP

def create_dirs():  _create_dirs(DIRS)
def save_pid(n, p): _save_pid(DIRS["pids"], n, p)
def load_pids():    return _load_pids(DIRS["pids"])
def spawn(t, a, n): return _spawn(t, a, DIRS["pids"], n)


# ══════════════════════════════════════════════
#  FIX HELPERS
# ══════════════════════════════════════════════

def fix_checksum(msg: str) -> str:
    return f"{sum(ord(c) for c in msg) % 256:03d}"

def build_fix(fields: dict) -> str:
    """Build a FIX message string from a dict of tag→value (ordered)."""
    body_fields = {k: v for k, v in fields.items() if k not in ("8", "9", "10")}
    body = SOH.join(f"{k}={v}" for k, v in body_fields.items()) + SOH
    body_len = len(body)
    header_part = f"8={fields.get('8','FIX.4.4')}{SOH}9={body_len}{SOH}"
    full = header_part + body
    cs = fix_checksum(full)
    return full + f"10={cs}{SOH}"

def fix_to_readable(raw: str) -> str:
    """Convert SOH-delimited FIX to pipe-delimited for display."""
    return raw.replace(SOH, " | ")


# ══════════════════════════════════════════════
#  BACKGROUND WORKERS
# ══════════════════════════════════════════════

def _fix_acceptor(port: int):
    """
    Minimal FIX 4.4 acceptor:
    - Accepts TCP connection
    - Receives Logon (35=A) → responds with Logon
    - Responds to Heartbeats (35=0)
    - Responds to NewOrderSingle (35=D) with an ExecutionReport (35=8 39=2 filled)
    - Logs all messages to file
    """
    try:
        import setproctitle; setproctitle.setproctitle("fix_acceptor")
    except ImportError:
        pass

    log_path = DIRS["logs"] / "fix_acceptor.log"
    seq_num   = [1]

    def log(msg):
        with open(log_path, "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")

    def send(conn, fields):
        msg = build_fix(fields)
        conn.sendall(msg.encode())
        log(f"SENT:  {fix_to_readable(msg)}")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(5)
    srv.settimeout(2)
    log(f"FIX acceptor listening on port {port}")

    while True:
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break

        log(f"Connection from {addr}")
        buf = b""
        try:
            while True:
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data
                # Process complete FIX messages (end with 10=xxx\x01)
                while b"\x0110=" in buf:
                    end = buf.find(b"\x01", buf.find(b"\x0110=") + 1)
                    if end == -1:
                        break
                    raw = buf[:end + 1].decode(errors="replace")
                    buf = buf[end + 1:]
                    log(f"RECV:  {fix_to_readable(raw)}")

                    fields = {}
                    for pair in raw.split(SOH):
                        if "=" in pair:
                            t, v = pair.split("=", 1)
                            fields[t.strip()] = v.strip()

                    msg_type = fields.get("35", "")
                    sender   = fields.get("49", "CLIENT")
                    target   = fields.get("56", "EXCHANGE")

                    seq_num[0] += 1
                    ts = datetime.utcnow().strftime("%Y%m%d-%H:%M:%S")

                    if msg_type == "A":  # Logon → respond with Logon
                        send(conn, {
                            "8": "FIX.4.4", "35": "A",
                            "49": target, "56": sender,
                            "34": str(seq_num[0]), "52": ts,
                            "98": "0", "108": "30",
                        })

                    elif msg_type == "0":  # Heartbeat → echo back
                        send(conn, {
                            "8": "FIX.4.4", "35": "0",
                            "49": target, "56": sender,
                            "34": str(seq_num[0]), "52": ts,
                        })

                    elif msg_type == "D":  # NewOrderSingle → fill it
                        seq_num[0] += 1
                        send(conn, {
                            "8": "FIX.4.4", "35": "8",
                            "49": target, "56": sender,
                            "34": str(seq_num[0]), "52": ts,
                            "17": f"EXEC-{seq_num[0]:04d}",
                            "37": f"ORD-{seq_num[0]:04d}",
                            "11": fields.get("11", "?"),
                            "39": "2",    # OrdStatus = Filled
                            "55": fields.get("55", "AAPL"),
                            "54": fields.get("54", "1"),
                            "38": fields.get("38", "100"),
                            "32": fields.get("38", "100"),
                            "31": fields.get("44", "185.50"),
                            "14": fields.get("38", "100"),
                            "6":  fields.get("44", "185.50"),
                            "150": "2",   # ExecType = Fill
                            "151": "0",   # LeavesQty = 0
                        })

                    elif msg_type == "F":  # OrderCancelRequest → confirm cancel
                        seq_num[0] += 1
                        send(conn, {
                            "8": "FIX.4.4", "35": "8",
                            "49": target, "56": sender,
                            "34": str(seq_num[0]), "52": ts,
                            "17": f"EXEC-{seq_num[0]:04d}",
                            "37": fields.get("41", f"ORD-{seq_num[0]:04d}"),
                            "11": fields.get("11", "?"),
                            "41": fields.get("41", "?"),   # OrigClOrdID echoed back
                            "39": "4",    # OrdStatus = Cancelled
                            "55": fields.get("55", "?"),
                            "54": fields.get("54", "1"),
                            "38": fields.get("38", "0"),
                            "32": "0",    # LastQty = 0
                            "31": "0",    # LastPx  = 0
                            "14": "0",    # CumQty  = 0
                            "6":  "0",    # AvgPx   = 0
                            "150": "4",   # ExecType = Cancelled
                            "151": "0",   # LeavesQty = 0
                        })

                    elif msg_type == "G":  # OrderCancelReplaceRequest → ack replace
                        seq_num[0] += 1
                        send(conn, {
                            "8": "FIX.4.4", "35": "8",
                            "49": target, "56": sender,
                            "34": str(seq_num[0]), "52": ts,
                            "17": f"EXEC-{seq_num[0]:04d}",
                            "37": fields.get("41", f"ORD-{seq_num[0]:04d}"),
                            "11": fields.get("11", "?"),
                            "41": fields.get("41", "?"),   # OrigClOrdID echoed back
                            "39": "0",    # OrdStatus = New (replaced order now active)
                            "55": fields.get("55", "?"),
                            "54": fields.get("54", "1"),
                            "38": fields.get("38", "0"),
                            "44": fields.get("44", "0"),
                            "32": "0",
                            "31": "0",
                            "14": "0",
                            "6":  "0",
                            "150": "5",   # ExecType = Replace
                            "151": fields.get("38", "0"),  # LeavesQty = new qty
                        })

                    elif msg_type == "5":  # Logout
                        send(conn, {
                            "8": "FIX.4.4", "35": "5",
                            "49": target, "56": sender,
                            "34": str(seq_num[0]), "52": ts,
                        })
                        break

        except (ConnectionResetError, BrokenPipeError):
            log("Client disconnected")
        finally:
            conn.close()


# ══════════════════════════════════════════════
#  WRITE SAMPLE MESSAGE FILES
# ══════════════════════════════════════════════

def write_sample_messages():
    """Write a set of raw FIX messages to files for parsing exercises."""
    messages = {
        "logon.fix": build_fix({
            "8": "FIX.4.4", "35": "A",
            "49": "FIRM_OMS", "56": "EXCHANGE_A",
            "34": "1", "52": "20240115-09:30:00",
            "98": "0", "108": "30",
        }),
        "new_order_single.fix": build_fix({
            "8": "FIX.4.4", "35": "D",
            "49": "FIRM_OMS", "56": "EXCHANGE_A",
            "34": "2", "52": "20240115-09:30:01",
            "11": "ORD-001", "55": "AAPL",
            "54": "1", "60": "20240115-09:30:01",
            "38": "100", "40": "2", "44": "185.50",
            "59": "0",
        }),
        "execution_report_fill.fix": build_fix({
            "8": "FIX.4.4", "35": "8",
            "49": "EXCHANGE_A", "56": "FIRM_OMS",
            "34": "3", "52": "20240115-09:30:01",
            "17": "EXEC-001", "37": "ORD-001", "11": "ORD-001",
            "39": "2", "55": "AAPL",
            "54": "1", "38": "100", "32": "100",
            "31": "185.50", "6": "185.50", "14": "100",
            "150": "2", "151": "0",
        }),
        "execution_report_reject.fix": build_fix({
            "8": "FIX.4.4", "35": "8",
            "49": "EXCHANGE_A", "56": "FIRM_OMS",
            "34": "4", "52": "20240115-09:30:02",
            "17": "EXEC-002", "37": "ORD-002", "11": "ORD-002",
            "39": "8", "55": "TSLA",
            "54": "1", "38": "5000",
            "58": "Insufficient margin for order size",
            "150": "8", "151": "5000",
        }),
        "heartbeat.fix": build_fix({
            "8": "FIX.4.4", "35": "0",
            "49": "FIRM_OMS", "56": "EXCHANGE_A",
            "34": "5", "52": "20240115-09:30:30",
        }),
        "order_cancel_request.fix": build_fix({
            "8": "FIX.4.4", "35": "F",
            "49": "FIRM_OMS", "56": "EXCHANGE_A",
            "34": "6", "52": "20240115-09:31:00",
            "41": "ORD-001", "11": "ORD-003",
            "55": "AAPL", "54": "1", "38": "100",
        }),
        "logout.fix": build_fix({
            "8": "FIX.4.4", "35": "5",
            "49": "FIRM_OMS", "56": "EXCHANGE_A",
            "34": "100", "52": "20240115-17:30:00",
            "58": "End of trading day",
        }),
    }
    for name, msg in messages.items():
        path = DIRS["messages"] / name
        path.write_text(msg)

    # Write a broken/malformed FIX session log for scenario 3
    broken_log = DIRS["logs"] / "broken_session.log"
    broken_log.write_text(f"""\
# FIX Session Log — broken session for diagnosis
# ================================================
# Format: DIRECTION [TIME] RAW_MESSAGE
#
SEND [09:30:00] 8=FIX.4.4{SOH}9=65{SOH}35=A{SOH}49=FIRM_OMS{SOH}56=EXCHANGE_A{SOH}34=1{SOH}52=20240115-09:30:00{SOH}98=0{SOH}108=30{SOH}10=100{SOH}
RECV [09:30:00] 8=FIX.4.4{SOH}9=65{SOH}35=A{SOH}49=EXCHANGE_A{SOH}56=FIRM_OMS{SOH}34=1{SOH}52=20240115-09:30:00{SOH}98=0{SOH}108=30{SOH}10=102{SOH}
SEND [09:30:01] 8=FIX.4.4{SOH}9=148{SOH}35=D{SOH}49=FIRM_OMS{SOH}56=EXCHANGE_A{SOH}34=2{SOH}52=20240115-09:30:01{SOH}11=ORD-001{SOH}55=AAPL{SOH}54=1{SOH}38=100{SOH}40=2{SOH}44=185.50{SOH}10=200{SOH}
RECV [09:30:01] 8=FIX.4.4{SOH}9=160{SOH}35=8{SOH}49=EXCHANGE_A{SOH}56=FIRM_OMS{SOH}34=2{SOH}52=20240115-09:30:01{SOH}17=EXEC-001{SOH}37=ORD-001{SOH}39=2{SOH}55=AAPL{SOH}38=100{SOH}32=100{SOH}31=185.50{SOH}10=189{SOH}
SEND [09:30:30] 8=FIX.4.4{SOH}9=52{SOH}35=0{SOH}49=FIRM_OMS{SOH}56=EXCHANGE_A{SOH}34=3{SOH}52=20240115-09:30:30{SOH}10=077{SOH}
# --- heartbeat missed from exchange ---
SEND [09:31:00] 8=FIX.4.4{SOH}9=62{SOH}35=1{SOH}49=FIRM_OMS{SOH}56=EXCHANGE_A{SOH}34=4{SOH}52=20240115-09:31:00{SOH}112=HB-TEST-1{SOH}10=055{SOH}
# TestRequest (35=1) sent — no response after 30s → session considered dead
# PROBLEM: exchange seq jumped from 2 to 5 — sequence gap detected
RECV [09:31:35] 8=FIX.4.4{SOH}9=80{SOH}35=3{SOH}49=EXCHANGE_A{SOH}56=FIRM_OMS{SOH}34=5{SOH}52=20240115-09:31:35{SOH}45=4{SOH}58=MsgSeqNum too low, expecting 5 got 4{SOH}10=120{SOH}
# 35=3 is a Reject — exchange expected seq=5 but we sent seq=4. Gap of 1.
# Resolution: ResendRequest (35=2) to request missing seq 3 from exchange
SEND [09:31:36] 8=FIX.4.4{SOH}9=70{SOH}35=2{SOH}49=FIRM_OMS{SOH}56=EXCHANGE_A{SOH}34=5{SOH}52=20240115-09:31:36{SOH}7=3{SOH}16=3{SOH}10=133{SOH}
# 7=BeginSeqNo, 16=EndSeqNo — request retransmission of seq=3
""")
    return messages, broken_log


# ══════════════════════════════════════════════
#  SCENARIO LAUNCHERS
# ══════════════════════════════════════════════

def launch_scenario_1():
    header("Scenario F-01 — FIX Session Lifecycle")
    print("  Spin up a FIX acceptor and walk through the session lifecycle.\n")

    FIX_PORT = 9878
    pid = spawn(_fix_acceptor, (FIX_PORT,), "fix_acceptor")
    ok(f"FIX acceptor started on port {FIX_PORT}  PID={pid}")
    time.sleep(0.5)

    # Write an initiator (client) script
    initiator = DIRS["scripts"] / "fix_initiator.py"
    initiator.write_text(f"""\
#!/usr/bin/env python3
\"\"\"
F-01: FIX initiator — connects to the lab acceptor, sends Logon + order.
Usage: python3 {initiator}
\"\"\"
import socket, time
from datetime import datetime

SOH       = "\\x01"
HOST, PORT = "127.0.0.1", {FIX_PORT}
SENDER, TARGET = "FIRM_OMS", "EXCHANGE_A"
seq = [1]

def ts():
    return datetime.utcnow().strftime("%Y%m%d-%H:%M:%S")

def checksum(msg):
    return f"{{sum(ord(c) for c in msg) % 256:03d}}"

def build(fields):
    body = SOH.join(f"{{k}}={{v}}" for k, v in fields.items()) + SOH
    hdr  = f"8=FIX.4.4{{SOH}}9={{len(body)}}{{SOH}}"
    full = hdr + body
    return full + f"10={{checksum(full)}}{{SOH}}"

def send(conn, fields):
    msg = build(fields)
    conn.sendall(msg.encode())
    print(f"  → SENT: {{msg.replace(SOH,' | ')[:100]}}...")
    seq[0] += 1

def recv(conn):
    data = conn.recv(4096).decode(errors="replace")
    print(f"  ← RECV: {{data.replace(SOH,' | ')[:100]}}...")
    return data

conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
conn.settimeout(5)
conn.connect((HOST, PORT))
print(f"Connected to FIX acceptor at {{HOST}}:{{PORT}}\\n")

# 1. Logon
print("Step 1: Logon")
send(conn, {{"35":"A","49":SENDER,"56":TARGET,"34":str(seq[0]),"52":ts(),"98":"0","108":"30"}})
recv(conn)

# 2. Heartbeat
print("\\nStep 2: Heartbeat")
send(conn, {{"35":"0","49":SENDER,"56":TARGET,"34":str(seq[0]),"52":ts()}})
recv(conn)

# 3. NewOrderSingle — BUY 100 AAPL
print("\\nStep 3: NewOrderSingle (BUY 100 AAPL @ 185.50 Limit)")
send(conn, {{"35":"D","49":SENDER,"56":TARGET,"34":str(seq[0]),"52":ts(),
             "11":"ORD-001","55":"AAPL","54":"1","60":ts(),
             "38":"100","40":"2","44":"185.50","59":"0"}})
recv(conn)  # ExecutionReport (filled)

# 4. Logout
print("\\nStep 4: Logout")
send(conn, {{"35":"5","49":SENDER,"56":TARGET,"34":str(seq[0]),"52":ts(),"58":"End of test"}})
try: recv(conn)
except socket.timeout: pass

conn.close()
print("\\nSession complete. Check acceptor log:")
print(f"  cat {DIRS['logs']}/fix_acceptor.log")
""")
    ok(f"Initiator script: {initiator}")

    print(f"""
{BOLD}── FIX Acceptor running on port {FIX_PORT} ──────────────────────{RESET}

{BOLD}── Run the initiator to walk through the full session ──{RESET}
{CYAN}       python3 {initiator}{RESET}

{BOLD}── Watch the acceptor log in real time ──────────────────{RESET}
{CYAN}       tail -f {DIRS["logs"]}/fix_acceptor.log{RESET}

{BOLD}── FIX Session Lifecycle ────────────────────────────────{RESET}
  1. TCP connect (port 9878)
  2. Initiator sends Logon (35=A)     ← must be first message
  3. Acceptor responds with Logon (35=A)
  4. Both sides exchange Heartbeats (35=0) every HeartBtInt seconds
  5. Business messages: NewOrder (35=D), ExecReport (35=8), etc.
  6. Either side sends Logout (35=5) to end cleanly
  7. TCP disconnect

{BOLD}── Key rules ────────────────────────────────────────────{RESET}
  • Seq numbers start at 1 and increment every message
  • Missed heartbeat → send TestRequest (35=1), wait 30s → if no reply, reconnect
  • Seq gap detected → send ResendRequest (35=2) for missing range
  • PossDupFlag (43=Y) on resent messages to prevent duplicate processing
""")


def launch_scenario_2():
    header("Scenario F-02 — Decode and Parse FIX Messages")
    print("  Raw FIX messages to decode. Know every tag cold.\n")

    messages, _ = write_sample_messages()

    parser = DIRS["scripts"] / "fix_decoder.py"
    parser.write_text(f"""\
#!/usr/bin/env python3
\"\"\"F-02: FIX message decoder. Reads all sample messages and decodes them.\"\"\"
from pathlib import Path

SOH = "\\x01"

TAGS = {{
    "8":"BeginString","9":"BodyLength","35":"MsgType","49":"SenderCompID",
    "56":"TargetCompID","34":"MsgSeqNum","52":"SendingTime","11":"ClOrdID",
    "37":"OrderID","17":"ExecID","39":"OrdStatus","55":"Symbol","54":"Side",
    "38":"OrderQty","40":"OrdType","44":"Price","31":"LastPx","32":"LastQty",
    "14":"CumQty","6":"AvgPx","59":"TimeInForce","41":"OrigClOrdID",
    "58":"Text","150":"ExecType","151":"LeavesQty","98":"EncryptMethod",
    "108":"HeartBtInt","112":"TestReqID","45":"RefSeqNum","7":"BeginSeqNo",
    "16":"EndSeqNo","60":"TransactTime","10":"CheckSum",
}}

MSG_TYPES = {{
    "0":"Heartbeat","1":"TestRequest","2":"ResendRequest","3":"Reject",
    "5":"Logout","8":"ExecutionReport","A":"Logon","D":"NewOrderSingle",
    "F":"OrderCancelRequest","G":"OrderCancelReplaceRequest",
}}

SIDES      = {{"1":"BUY","2":"SELL"}}
ORD_STATUS = {{"0":"New","1":"PartialFill","2":"Filled","4":"Cancelled","8":"Rejected"}}
ORD_TYPES  = {{"1":"Market","2":"Limit","3":"Stop","4":"StopLimit"}}
EXEC_TYPES = {{"0":"New","1":"PartialFill","2":"Fill","4":"Cancelled","8":"Rejected"}}

def decode(raw):
    fields = {{}}
    for pair in raw.split(SOH):
        if "=" in pair:
            t, v = pair.split("=", 1)
            fields[t.strip()] = v.strip()
    return fields

def describe(path, raw):
    fields = decode(raw)
    mt = fields.get("35","?")
    type_name = MSG_TYPES.get(mt, f"Unknown({{mt}})")
    print(f"\\n{'='*50}")
    print(f"File:    {{path.name}}")
    print(f"MsgType: {{mt}} = {{type_name}}")
    print(f"Seq:     {{fields.get('34','?')}}")
    print(f"From:    {{fields.get('49','?')}} → {{fields.get('56','?')}}")
    print(f"Time:    {{fields.get('52','?')}}")
    if mt == "D":
        print(f"ORDER:   {{SIDES.get(fields.get('54'),'?')}} {{fields.get('38','?')}} {{fields.get('55','?')}} @ ${{fields.get('44','MARKET')}} [{{ORD_TYPES.get(fields.get('40',''),'?')}}]")
    elif mt == "8":
        status = ORD_STATUS.get(fields.get('39',''), fields.get('39','?'))
        print(f"EXEC:    status={{status}}  LastPx={{fields.get('31','?')}}  LastQty={{fields.get('32','?')}}  CumQty={{fields.get('14','?')}}")
        if fields.get('58'): print(f"Text:    {{fields.get('58')}}")
    elif mt == "A": print(f"LOGON:   HeartBtInt={{fields.get('108','?')}}s")
    elif mt == "3": print(f"REJECT:  RefSeq={{fields.get('45','?')}}  Reason={{fields.get('58','?')}}")
    elif mt == "2": print(f"RESEND:  BeginSeqNo={{fields.get('7','?')}} EndSeqNo={{fields.get('16','?')}}")

MSG_DIR = Path("{DIRS['messages']}")
for fix_file in sorted(MSG_DIR.glob("*.fix")):
    raw = fix_file.read_text().strip()
    describe(fix_file, raw)
""")
    ok(f"Decoder script: {parser}")

    print(f"""
{BOLD}── FIX message files ───────────────────────────────────{RESET}
{CYAN}       ls {DIRS["messages"]}/*.fix
       cat {DIRS["messages"]}/new_order_single.fix | tr '\\001' '|'{RESET}

{BOLD}── Run the decoder ──────────────────────────────────────{RESET}
{CYAN}       python3 {parser}{RESET}

{BOLD}── Key tags to know cold ────────────────────────────────{RESET}
  8  = BeginString    → always FIX.4.4
  9  = BodyLength     → byte count of body (between 9= and 10=)
  35 = MsgType        → D=NewOrder, 8=ExecReport, A=Logon, 0=Heartbeat
  49 = SenderCompID   → who sent it
  56 = TargetCompID   → who it's for
  34 = MsgSeqNum      → increments per session, gaps = problem
  55 = Symbol         → AAPL, GOOGL, etc.
  54 = Side           → 1=BUY, 2=SELL
  38 = OrderQty       → shares/contracts
  44 = Price          → limit price (for OrdType=2)
  40 = OrdType        → 1=Market, 2=Limit
  39 = OrdStatus      → 0=New, 1=PartialFill, 2=Filled, 8=Rejected
  58 = Text           → rejection reason
  10 = CheckSum       → 3-digit sum mod 256 of all preceding bytes
""")


def launch_scenario_3():
    header("Scenario F-03 — Diagnose a Broken FIX Session")
    print("  A FIX session has a sequence gap and a reject. Diagnose.\n")

    _, broken_log = write_sample_messages()

    diagnose = DIRS["scripts"] / "diagnose_session.py"
    diagnose.write_text(f"""\
#!/usr/bin/env python3
\"\"\"F-03: Analyse a broken FIX session log and identify issues.\"\"\"

SOH      = "\\x01"
LOG_FILE = "{broken_log}"

MSG_TYPES = {{
    "0":"Heartbeat","1":"TestRequest","2":"ResendRequest","3":"Reject",
    "5":"Logout","8":"ExecutionReport","A":"Logon","D":"NewOrderSingle",
    "F":"OrderCancelRequest",
}}

issues = []
prev_recv_seq = 0
prev_send_seq = 0

print(f"Analysing: {{LOG_FILE}}\\n")

with open(LOG_FILE) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"): continue

        direction = line.split()[0]
        raw_start = line.find("8=FIX")
        if raw_start == -1: continue
        raw = line[raw_start:]

        fields = {{}}
        for pair in raw.split(SOH):
            if "=" in pair:
                t, v = pair.split("=", 1)
                fields[t.strip()] = v.strip()

        seq      = int(fields.get("34", 0))
        msg_type = fields.get("35", "?")
        type_name = MSG_TYPES.get(msg_type, f"Unknown({{msg_type}})")
        ts       = line.split("[")[1].split("]")[0] if "[" in line else "?"

        print(f"  [{{ts}}] {{direction}}  seq={{seq:3d}}  35={{msg_type}} ({{type_name}})")

        if msg_type == "3":  # Reject
            print(f"          ⚠  REJECT: RefSeq={{fields.get('45','?')}}  Text={{fields.get('58','?')}}")
            issues.append(f"Reject received at seq={{seq}}: {{fields.get('58','no text')}}")

        if direction == "RECV" and prev_recv_seq > 0 and seq != prev_recv_seq + 1:
            gap = seq - prev_recv_seq - 1
            issues.append(f"SEQ GAP: expected recv seq={{prev_recv_seq+1}} got {{seq}} ({{gap}} missing)")
            print(f"          🔴 SEQ GAP: expected {{prev_recv_seq+1}}, got {{seq}}")

        if direction == "SEND": prev_send_seq = seq
        if direction == "RECV": prev_recv_seq = seq

print(f"\\n{'='*50}")
print(f"Issues found: {{len(issues)}}")
for issue in issues:
    print(f"  ⚠  {{issue}}")
print(f"\\nResolution steps:")
print("  1. Send ResendRequest (35=2) for missing sequence range")
print("  2. Exchange retransmits with PossDupFlag=Y (43=Y)")
print("  3. If unrecoverable → both sides send Logout (35=5) and reconnect")
print("     Logon with ResetOnLogon=Y resets both sides to seq=1")
""")
    ok(f"Diagnose script: {diagnose}")

    print(f"""
{BOLD}── Broken session log ──────────────────────────────────{RESET}
{CYAN}       cat {broken_log}{RESET}

{BOLD}── Run the diagnostics ─────────────────────────────────{RESET}
{CYAN}       python3 {diagnose}{RESET}

{BOLD}── Faults in this session ──────────────────────────────{RESET}
  1. Heartbeat missed from exchange (no response to TestRequest)
  2. Sequence gap: exchange jumped from seq=2 to seq=5 (missing seq=3,4)
  3. Exchange sent Reject (35=3): "MsgSeqNum too low, expecting 5 got 4"

{BOLD}── Resolution playbook ──────────────────────────────────{RESET}
  Step 1: Send ResendRequest (35=2) BeginSeqNo=3 EndSeqNo=4
  Step 2: Exchange retransmits messages 3,4 with PossDupFlag=Y (tag 43)
  Step 3: Session resynchronised — continue normally
  If unrecoverable: Logout → reconnect → Logon with ResetOnLogon=Y
""")


def launch_scenario_4():
    header("Scenario F-04 — Send a NewOrderSingle")
    print("  Send a real FIX NewOrderSingle and receive an ExecutionReport.\n")

    FIX_PORT = 9878
    # Start acceptor if not already running
    pid = spawn(_fix_acceptor, (FIX_PORT,), "fix_acceptor_f04")
    ok(f"FIX acceptor on port {FIX_PORT}  PID={pid}")
    time.sleep(0.5)

    sender = DIRS["scripts"] / "send_order.py"
    sender.write_text(f"""\
#!/usr/bin/env python3
\"\"\"
F-04: Send a FIX NewOrderSingle and decode the ExecutionReport.
Usage: python3 {sender} [symbol] [side] [qty] [price]
Example: python3 {sender} AAPL BUY 100 185.50
\"\"\"
import socket, sys
from datetime import datetime

SOH = "\\x01"
HOST, PORT = "127.0.0.1", {FIX_PORT}
SENDER, TARGET = "FIRM_OMS", "EXCHANGE_A"

SIDES      = {{"BUY":"1","SELL":"2"}}
ORD_STATUS = {{"0":"New","1":"PartialFill","2":"Filled","4":"Cancelled","8":"Rejected"}}

symbol = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
side   = sys.argv[2] if len(sys.argv) > 2 else "BUY"
qty    = sys.argv[3] if len(sys.argv) > 3 else "100"
price  = sys.argv[4] if len(sys.argv) > 4 else "185.50"
side_code = SIDES.get(side.upper(), "1")

def ts(): return datetime.utcnow().strftime("%Y%m%d-%H:%M:%S")
def checksum(m): return f"{{sum(ord(c) for c in m) % 256:03d}}"
def build(fields):
    body = SOH.join(f"{{k}}={{v}}" for k, v in fields.items()) + SOH
    hdr  = f"8=FIX.4.4{{SOH}}9={{len(body)}}{{SOH}}"
    full = hdr + body
    return full + f"10={{checksum(full)}}{{SOH}}"

def parse(raw):
    fields = {{}}
    for pair in raw.split(SOH):
        if "=" in pair:
            t, v = pair.split("=", 1)
            fields[t.strip()] = v.strip()
    return fields

conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
conn.settimeout(5)
conn.connect((HOST, PORT))
print(f"Connected to FIX acceptor at {{HOST}}:{{PORT}}")

# Logon
logon = build({{"35":"A","49":SENDER,"56":TARGET,"34":"1","52":ts(),"98":"0","108":"30"}})
conn.sendall(logon.encode())
resp = parse(conn.recv(4096).decode(errors="replace"))
print(f"Logon response: MsgType={{resp.get('35')}} from {{resp.get('49')}}")

# NewOrderSingle
print(f"\\nSending: {{side.upper()}} {{qty}} {{symbol}} @ ${{price}} (Limit)")
order = build({{
    "35":"D","49":SENDER,"56":TARGET,"34":"2","52":ts(),
    "11":"ORD-001","55":symbol,"54":side_code,"60":ts(),
    "38":qty,"40":"2","44":price,"59":"0",
}})
conn.sendall(order.encode())

# Receive ExecutionReport
raw = conn.recv(4096).decode(errors="replace")
fields = parse(raw)
status = ORD_STATUS.get(fields.get("39",""), fields.get("39","?"))
print(f"\\nExecutionReport received:")
print(f"  OrdStatus : {{status}}")
print(f"  Symbol    : {{fields.get('55','?')}}")
print(f"  LastPx    : ${{fields.get('31','?')}}")
print(f"  LastQty   : {{fields.get('32','?')}}")
print(f"  CumQty    : {{fields.get('14','?')}}")
if fields.get("58"): print(f"  Reason    : {{fields.get('58')}}")

conn.close()
""")
    ok(f"Order sender script: {sender}")

    print(f"""
{BOLD}── FIX acceptor on port {FIX_PORT} (fills all orders) ──────────────{RESET}

{BOLD}── Send orders ──────────────────────────────────────────{RESET}
{CYAN}       python3 {sender}                          # default: BUY 100 AAPL
       python3 {sender} GOOGL SELL 50 141.20     # SELL 50 GOOGL
       python3 {sender} TSLA BUY 5000 248.00     # large order{RESET}

{BOLD}── Watch the acceptor log ───────────────────────────────{RESET}
{CYAN}       tail -f {DIRS["logs"]}/fix_acceptor.log{RESET}

{BOLD}── Key ExecutionReport fields ──────────────────────────{RESET}
  35=8  → ExecutionReport
  39    → OrdStatus:  0=New, 1=PartialFill, 2=Filled, 8=Rejected
  150   → ExecType:   0=New, 1=PartialFill, 2=Fill, 8=Rejected
  31    → LastPx      (price of this execution)
  32    → LastQty     (qty filled in this execution)
  14    → CumQty      (total qty filled so far)
  151   → LeavesQty  (qty still open)
  58    → Text        (rejection reason if 39=8)
""")


def launch_scenario_5():
    header("Scenario F-05 — FIX Tag Reference & ExecReport Walkthrough")
    print("  Master the most important FIX tags and message flows.\n")

    tag_ref = DIRS["config"] / "fix_tag_reference.md"
    tag_ref.write_text("""\
# FIX 4.4 Tag Reference — Support Engineer Cheat Sheet
=======================================================

## Header Tags (every message)
  8  = BeginString       Always FIX.4.4 (or FIX.4.2 / FIX.5.0 etc.)
  9  = BodyLength        Byte count of message body (between tag 9 and tag 10)
  35 = MsgType           The most important tag — defines what the message IS
  49 = SenderCompID      Who sent this message
  56 = TargetCompID      Who this message is for
  34 = MsgSeqNum         Sequence number — increments every message
  52 = SendingTime       UTC timestamp: YYYYMMDD-HH:MM:SS
  43 = PossDupFlag       Y if this is a retransmitted message (from ResendRequest)
  10 = CheckSum          3-digit sum of all bytes mod 256

## Message Types (tag 35)
  0  = Heartbeat         Keep-alive; exchange if no message in HeartBtInt seconds
  1  = TestRequest       "Are you alive?" — expects a Heartbeat response
  2  = ResendRequest     Request retransmission of missed messages
  3  = Reject            Application-level reject (bad tag, invalid value)
  5  = Logout            Clean session termination
  8  = ExecutionReport   Response to an order (fill, reject, cancel confirm)
  A  = Logon             First message; establishes session and seq numbers
  D  = NewOrderSingle    Submit a new order
  F  = OrderCancelRequest Cancel an existing order
  G  = OrderCancelReplaceRequest Modify an existing order

## Order Tags
  11 = ClOrdID           Client order ID — your internal reference
  37 = OrderID           Exchange-assigned order ID (in ExecReport)
  17 = ExecID            Unique execution ID (in ExecReport)
  55 = Symbol            Instrument: AAPL, GOOGL, EURUSD, etc.
  54 = Side              1=Buy, 2=Sell, 5=SellShort
  38 = OrderQty          Number of shares/contracts
  40 = OrdType           1=Market, 2=Limit, 3=Stop, 4=StopLimit
  44 = Price             Limit price (required for OrdType=2)
  59 = TimeInForce       0=Day, 1=GoodTillCancel, 3=ImmOrCancel, 4=FillOrKill
  60 = TransactTime      Time of order creation

## Execution Report Tags
  39 = OrdStatus         0=New, 1=PartialFill, 2=Filled, 4=Cancelled, 8=Rejected
  150 = ExecType         Type of this execution: 0=New, 1=PartialFill, 2=Fill
  31 = LastPx            Price of THIS fill
  32 = LastQty           Qty filled in THIS execution
  14 = CumQty            Total qty filled across all executions
  151 = LeavesQty        Qty still open (OrderQty - CumQty)
  6  = AvgPx             Volume-weighted average fill price
  58 = Text              Free-text reason (e.g., rejection reason)

## Typical Flows

FULL FILL:
  Client → D (NewOrderSingle qty=100)
  Exchange → 8 (ExecReport 39=0 150=0 CumQty=0 LeavesQty=100)  New
  Exchange → 8 (ExecReport 39=2 150=2 CumQty=100 LeavesQty=0)  Filled

PARTIAL FILL:
  Client → D (qty=100)
  Exchange → 8 (39=1 150=1 LastQty=50 CumQty=50 LeavesQty=50)
  Exchange → 8 (39=2 150=2 LastQty=50 CumQty=100 LeavesQty=0)

REJECT:
  Exchange → 8 (39=8 150=8 Text="Insufficient margin")

CANCEL:
  Client → F (OrderCancelRequest OrigClOrdID=ORD-001)
  Exchange → 8 (39=4 ExecType=4)  Cancelled

## Sequence Number Rules
  Start at 1, increment every message
  Gap detected → send ResendRequest (35=2) BeginSeqNo=X EndSeqNo=Y
  Retransmitted messages have PossDupFlag=Y (43=Y) — don't process as new
  ResetOnLogon=Y → reset both sides to seq=1 on next Logon
""")
    ok(f"Tag reference: {tag_ref}")

    print(f"""
{BOLD}── Read the full tag reference ─────────────────────────{RESET}
{CYAN}       cat {tag_ref}{RESET}

{BOLD}── Key things to know for an interview ─────────────────{RESET}
  Q: What does tag 35 do?
  A: MsgType — the most important tag. Defines what every message IS.
     35=D (NewOrder), 35=8 (ExecReport), 35=A (Logon), 35=0 (Heartbeat).

  Q: How does a sequence gap happen and how do you fix it?
  A: Network issue caused messages to be dropped. Detected when received
     seq jumps (e.g., got 5 but expected 3). Fix: send ResendRequest
     (35=2) for missing range → exchange retransmits with PossDupFlag=Y.

  Q: What is the difference between OrdStatus (39) and ExecType (150)?
  A: OrdStatus is the CURRENT STATE of the order (overall status).
     ExecType describes THIS SPECIFIC execution event.
     An order can have OrdStatus=1 (PartialFill) but ExecType=2 (Fill)
     meaning this execution was a fill but the order is still partially open.

  Q: Why does CheckSum (tag 10) matter?
  A: Detects bit-flip corruption in transit. If checksum doesn't match,
     the receiving side sends a Reject (35=3) and you must retransmit.
""")


def launch_scenario_99():
    header("Scenario 99 — ALL FIX Scenarios")
    for fn in [launch_scenario_1, launch_scenario_2, launch_scenario_3,
               launch_scenario_4, launch_scenario_5]:
        fn(); time.sleep(0.3)


# ══════════════════════════════════════════════
#  TEARDOWN / STATUS / MAIN
# ══════════════════════════════════════════════

def teardown():
    header("Tearing Down FIX Lab")
    kill_pids(DIRS["pids"])
    kill_strays(["fix_acceptor"])
    remove_lab_dir(LAB_ROOT)

def show_status():
    _show_status(DIRS["pids"], "FIX Lab")

SCENARIO_MAP = {
    1:  (launch_scenario_1, "F-01  FIX session lifecycle (acceptor + initiator)"),
    2:  (launch_scenario_2, "F-02  Decode and parse FIX messages"),
    3:  (launch_scenario_3, "F-03  Diagnose a broken FIX session"),
    4:  (launch_scenario_4, "F-04  Send a NewOrderSingle"),
    5:  (launch_scenario_5, "F-05  Tag reference & ExecReport walkthrough"),
    99: (launch_scenario_99, "     ALL scenarios"),
}

def main():
    parser = argparse.ArgumentParser(description="FIX Protocol Challenge Lab Setup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(f"  {k:<4} {v}" for k, (_, v) in SCENARIO_MAP.items()))
    parser.add_argument("--scenario", "-s", type=int, choices=list(SCENARIO_MAP.keys()))
    parser.add_argument("--teardown", "-t", action="store_true")
    parser.add_argument("--status",         action="store_true")
    args = parser.parse_args()
    if args.teardown: teardown(); return
    if args.status:   show_status(); return
    create_dirs()
    write_sample_messages()
    if args.scenario:
        fn, _ = SCENARIO_MAP[args.scenario]; fn()
    else:
        header("FIX Protocol Challenge Lab")
        for num, (_, desc) in SCENARIO_MAP.items():
            print(f"    {num:<4} {desc}")
        choice = input("\n  Enter scenario number (or q): ").strip()
        if choice.lower() == "q": return
        try:
            fn, _ = SCENARIO_MAP[int(choice)]; fn()
        except (KeyError, ValueError): err(f"Invalid: {choice}")
    lab_footer("lab_fix.py")

if __name__ == "__main__":
    main()
