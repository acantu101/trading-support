#!/usr/bin/env python3
"""
FIX Session Log Generator
==========================
Generates a realistic FIX session log with current timestamps so that
time-based awk commands (5 minutes ago, 1 hour ago) actually match entries.

Output: /tmp/lab_fix/logs/session.log  (pipe-delimited for readability)

Usage:
    python3 generate_session_log.py
    python3 generate_session_log.py --output /tmp/mylab/session.log

Log format (one message per line):
    YYYYMMDD-HH:MM:SS DIRECTION 8=FIX.4.4|9=NNN|35=X|49=...|...

This format lets time-based awk comparisons work:
    awk -v t="$(date -d '5 minutes ago' '+%Y%m%d-%H:%M')" '$0 > t' session.log
"""

import argparse
from datetime import datetime, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────
OUTPUT   = Path("/tmp/lab_fix/logs/session.log")
SENDER   = "FIRM_OMS"
TARGET   = "EXCHANGE_A"


def ts_str(dt: datetime) -> str:
    return dt.strftime("%Y%m%d-%H:%M:%S")


def ts_prefix(dt: datetime) -> str:
    return dt.strftime("%Y%m%d-%H:%M:%S")


def checksum(msg: str) -> str:
    return f"{sum(ord(c) for c in msg.replace('|', chr(1))) % 256:03d}"


def build(fields: dict, dt: datetime) -> str:
    body_fields = {k: v for k, v in fields.items() if k not in ("8", "9", "10")}
    body  = "|".join(f"{k}={v}" for k, v in body_fields.items()) + "|"
    blen  = len(body.replace("|", chr(1)))
    full  = f"8=FIX.4.4|9={blen}|{body}"
    cs    = checksum(full)
    return f"{ts_prefix(dt)} {full}10={cs}|"


def logon(dt, seq_send, seq_recv, sender=SENDER, target=TARGET):
    lines = []
    lines.append(("SEND", build({"35":"A","49":sender,"56":target,
                                  "34":str(seq_send),"52":ts_str(dt),
                                  "98":"0","108":"30"}, dt)))
    lines.append(("RECV", build({"35":"A","49":target,"56":sender,
                                  "34":str(seq_recv),"52":ts_str(dt),
                                  "98":"0","108":"30"}, dt)))
    return lines


def heartbeat(dt, seq_send, seq_recv, sender=SENDER, target=TARGET):
    lines = []
    lines.append(("SEND", build({"35":"0","49":sender,"56":target,
                                  "34":str(seq_send),"52":ts_str(dt)}, dt)))
    lines.append(("RECV", build({"35":"0","49":target,"56":sender,
                                  "34":str(seq_recv),"52":ts_str(dt)}, dt)))
    return lines


def new_order(dt, seq_send, clord, symbol, side, qty, price, sender=SENDER, target=TARGET):
    return [("SEND", build({
        "35":"D","49":sender,"56":target,"34":str(seq_send),"52":ts_str(dt),
        "11":clord,"55":symbol,"54":side,"60":ts_str(dt),
        "38":str(qty),"40":"2","44":str(price),"59":"0",
    }, dt))]


def exec_report(dt, seq_recv, clord, symbol, side, qty, price,
                ord_status, exec_type, cum_qty, leaves_qty,
                last_px=None, last_qty=None, text=None,
                exec_id=None, sender=SENDER, target=TARGET):
    fields = {
        "35":"8","49":target,"56":sender,"34":str(seq_recv),"52":ts_str(dt),
        "17": exec_id or f"EXEC-{seq_recv:04d}",
        "37": f"ORD-{seq_recv:04d}",
        "11": clord,
        "39": ord_status,
        "55": symbol,"54": side,"38": str(qty),
        "32": str(last_qty or (qty if ord_status == "2" else 0)),
        "31": str(last_px or price),
        "14": str(cum_qty),
        "6":  str(price),
        "150": exec_type,
        "151": str(leaves_qty),
    }
    if text:
        fields["58"] = text
    return [("RECV", build(fields, dt))]


def reject(dt, seq_recv, ref_seq, text, sender=SENDER, target=TARGET):
    return [("RECV", build({
        "35":"3","49":target,"56":sender,"34":str(seq_recv),"52":ts_str(dt),
        "45":str(ref_seq),"58":text,
    }, dt))]


def seq_reset(dt, seq_recv, new_seq, sender=SENDER, target=TARGET):
    return [("RECV", build({
        "35":"4","49":target,"56":sender,"34":str(seq_recv),"52":ts_str(dt),
        "36":str(new_seq),"123":"Y",
    }, dt))]


def resend_request(dt, seq_send, begin_seq, end_seq, sender=SENDER, target=TARGET):
    return [("SEND", build({
        "35":"2","49":sender,"56":target,"34":str(seq_send),"52":ts_str(dt),
        "7":str(begin_seq),"16":str(end_seq),
    }, dt))]


def logout(dt, seq_send, seq_recv, text="End of session", sender=SENDER, target=TARGET):
    lines = []
    lines.append(("SEND", build({"35":"5","49":sender,"56":target,
                                  "34":str(seq_send),"52":ts_str(dt),
                                  "58":text}, dt)))
    lines.append(("RECV", build({"35":"5","49":target,"56":sender,
                                  "34":str(seq_recv),"52":ts_str(dt)}, dt)))
    return lines


def generate(output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    now   = datetime.now().replace(second=0, microsecond=0)
    lines = []

    def add(entries, t):
        for direction, msg in entries:
            lines.append(f"{direction} {msg}")

    # ── SESSION 1: starts 2 hours ago ─────────────────────────────────────
    t = now - timedelta(hours=2)
    ss, sr = 1, 1   # send seq, recv seq

    add(logon(t, ss, sr), t);        ss+=1; sr+=1; t += timedelta(seconds=1)

    # Normal trading block — 09:30 to 10:00 equivalent
    orders_1 = [
        ("AAPL", "1", 100, 185.50), ("GOOGL","2",  50, 141.20),
        ("MSFT", "1", 200, 380.10), ("TSLA", "1",  30, 248.00),
        ("AAPL", "2", 100, 185.75), ("NVDA", "1",  75, 495.00),
        ("AAPL", "1", 150, 185.40), ("SPY",  "1", 500,  510.25),
    ]
    for i, (sym, side, qty, price) in enumerate(orders_1):
        clord = f"ORD-{i+1:03d}"
        add(new_order(t, ss, clord, sym, side, qty, price), t); ss+=1
        t += timedelta(seconds=2)
        add(exec_report(t, sr, clord, sym, side, qty, price,
                        "2","2", qty, 0), t);                   sr+=1
        t += timedelta(seconds=1)
        if i % 4 == 3:   # heartbeat every 4 orders
            add(heartbeat(t, ss, sr), t); ss+=1; sr+=1; t += timedelta(seconds=1)

    # Partial fill on a large order
    clord = "ORD-009"
    add(new_order(t, ss, clord, "AMZN","1",1000,178.50), t); ss+=1; t += timedelta(seconds=2)
    add(exec_report(t, sr, clord,"AMZN","1",1000,178.50,"1","1",500,500,
                    last_px=178.50, last_qty=500), t);        sr+=1; t += timedelta(seconds=3)
    add(exec_report(t, sr, clord,"AMZN","1",1000,178.50,"2","2",1000,0,
                    last_px=178.48, last_qty=500), t);        sr+=1; t += timedelta(seconds=1)

    # Rejection
    clord = "ORD-010"
    add(new_order(t, ss, clord, "TSLA","1",5000,248.00), t); ss+=1; t += timedelta(seconds=2)
    add(exec_report(t, sr, clord,"TSLA","1",5000,248.00,"8","8",0,5000,
                    text="Insufficient margin for order size"), t); sr+=1; t += timedelta(seconds=1)

    add(heartbeat(t, ss, sr), t); ss+=1; sr+=1; t += timedelta(seconds=1)

    # ── SEQUENCE GAP at ~1h45m ago ─────────────────────────────────────────
    t = now - timedelta(minutes=105)
    clord = "ORD-011"
    add(new_order(t, ss, clord,"AAPL","1",200,185.60), t); ss+=1; t += timedelta(seconds=2)

    # Gap: skip recv seq, jump forward
    bad_seq = sr + 3
    add(reject(t, bad_seq, ss-1, "MsgSeqNum too low, expected "+str(sr)+" got "+str(bad_seq)), t)
    sr = bad_seq + 1
    t += timedelta(seconds=1)

    add(resend_request(t, ss, sr-4, sr-2), t); ss+=1; t += timedelta(seconds=1)
    # Retransmit with PossDupFlag
    lines.append(f"RECV {ts_prefix(t)} 8=FIX.4.4|9=80|35=8|49={TARGET}|56={SENDER}|34={sr}|52={ts_str(t)}|43=Y|17=EXEC-RXTX|11={clord}|39=2|55=AAPL|38=200|32=200|31=185.60|14=200|150=2|151=0|10=088|")
    sr+=1; t += timedelta(seconds=1)

    add(heartbeat(t, ss, sr), t); ss+=1; sr+=1; t += timedelta(seconds=30)

    # ── LOGOUT + RECONNECT at ~1h30m ago ─────────────────────────────────
    t = now - timedelta(minutes=90)
    add(logout(t, ss, sr, "Network timeout"), t); ss+=1; sr+=1; t += timedelta(seconds=5)

    # ── SESSION 2: reconnect ──────────────────────────────────────────────
    ss, sr = 1, 1
    add(logon(t, ss, sr), t); ss+=1; sr+=1; t += timedelta(seconds=1)

    orders_2 = [
        ("AAPL","1",300,185.30), ("MSFT","2",100,380.50),
        ("GOOGL","1",75,141.00), ("NVDA","2",50,494.50),
        ("SPY","1",200,510.10),  ("TSLA","2",40,247.80),
        ("AAPL","1",100,185.20), ("AMZN","1",150,178.20),
        ("MSFT","1",250,380.20), ("GOOGL","2",60,141.10),
    ]
    for i, (sym, side, qty, price) in enumerate(orders_2):
        clord = f"ORD-{i+100:03d}"
        add(new_order(t, ss, clord, sym, side, qty, price), t); ss+=1
        t += timedelta(seconds=2)
        add(exec_report(t, sr, clord, sym, side, qty, price,"2","2",qty,0), t); sr+=1
        t += timedelta(seconds=1)
        if i % 5 == 4:
            add(heartbeat(t, ss, sr), t); ss+=1; sr+=1; t += timedelta(seconds=1)

    # Cancel + replace
    clord = "ORD-110"
    add(new_order(t, ss, clord,"AAPL","1",500,185.00), t); ss+=1; t += timedelta(seconds=2)
    lines.append(f"SEND {ts_prefix(t)} 8=FIX.4.4|9=90|35=F|49={SENDER}|56={TARGET}|34={ss}|52={ts_str(t)}|41={clord}|11=ORD-111|55=AAPL|54=1|38=500|10=044|")
    ss+=1; t += timedelta(seconds=1)
    lines.append(f"RECV {ts_prefix(t)} 8=FIX.4.4|9=120|35=8|49={TARGET}|56={SENDER}|34={sr}|52={ts_str(t)}|17=EXEC-CNX|11=ORD-111|41={clord}|39=4|55=AAPL|54=1|38=500|32=0|31=0|14=0|150=4|151=0|10=055|")
    sr+=1; t += timedelta(seconds=2)

    # ── SEQUENCE RESET (35=4) — red flag event ────────────────────────────
    t = now - timedelta(minutes=35)
    lines.append(f"RECV {ts_prefix(t)} 8=FIX.4.4|9=60|35=4|49={TARGET}|56={SENDER}|34={sr}|52={ts_str(t)}|36=1|123=Y|10=077|")
    sr = 1
    t += timedelta(seconds=2)
    add(logon(t, ss, sr), t); ss+=1; sr+=1; t += timedelta(seconds=1)

    # ── RECENT ACTIVITY: last 30 minutes ──────────────────────────────────
    t = now - timedelta(minutes=28)
    recent_orders = [
        ("AAPL","1",100,185.50), ("NVDA","1",50,495.00),
        ("SPY","2",300,510.20),  ("MSFT","1",100,380.00),
        ("TSLA","1",20,248.50),
    ]
    for i, (sym, side, qty, price) in enumerate(recent_orders):
        clord = f"ORD-{i+200:03d}"
        add(new_order(t, ss, clord, sym, side, qty, price), t); ss+=1
        t += timedelta(seconds=2)
        add(exec_report(t, sr, clord, sym, side, qty, price,"2","2",qty,0), t); sr+=1
        t += timedelta(seconds=1)

    add(heartbeat(t, ss, sr), t); ss+=1; sr+=1; t += timedelta(seconds=30)

    # Rejection in last 10 mins
    t = now - timedelta(minutes=8)
    clord = "ORD-205"
    add(new_order(t, ss, clord,"NVDA2","1",100,495.00), t); ss+=1; t += timedelta(seconds=2)
    add(exec_report(t, sr, clord,"NVDA2","1",100,495.00,"8","8",0,100,
                    text="Symbol not found: NVDA2"), t); sr+=1; t += timedelta(seconds=1)

    # Another reject
    t = now - timedelta(minutes=5, seconds=30)
    clord = "ORD-206"
    add(new_order(t, ss, clord,"AAPL","1",10000,185.50), t); ss+=1; t += timedelta(seconds=2)
    add(exec_report(t, sr, clord,"AAPL","1",10000,185.50,"8","8",0,10000,
                    text="Order size exceeds position limit"), t); sr+=1

    # ── LAST 5 MINUTES: recent burst ──────────────────────────────────────
    t = now - timedelta(minutes=4)
    add(heartbeat(t, ss, sr), t); ss+=1; sr+=1; t += timedelta(seconds=30)

    fresh_orders = [
        ("AAPL","1",100,185.45), ("GOOGL","1",50,141.30),
        ("MSFT","2",75,380.15),  ("SPY","1",200,510.30),
    ]
    for i, (sym, side, qty, price) in enumerate(fresh_orders):
        clord = f"ORD-{i+210:03d}"
        add(new_order(t, ss, clord, sym, side, qty, price), t); ss+=1
        t += timedelta(seconds=15)
        add(exec_report(t, sr, clord, sym, side, qty, price,"2","2",qty,0), t); sr+=1
        t += timedelta(seconds=10)

    # Final logon event in last 2 mins (35=A — notable)
    t = now - timedelta(minutes=2)
    add(logon(t, ss, sr), t); ss+=1; sr+=1

    # ── Write output ──────────────────────────────────────────────────────
    output.write_text("\n".join(lines) + "\n")
    print(f"Written {len(lines)} messages to {output}")
    print(f"\nTry these commands:")
    print(f'  awk -v t="$(date -d \'5 minutes ago\' \'+%Y%m%d-%H:%M\')" \'$0 > t\' {output} | wc -l')
    print(f"  grep '35=3' {output}")
    print(f"  grep '35=4' {output}   # sequence resets — red flag")
    print(f"  grep '35=A' {output} | wc -l   # how many logons?")
    print(f"  grep '39=8' {output}   # all rejections")
    print(f"  grep '43=Y' {output}   # retransmitted messages")


def main():
    parser = argparse.ArgumentParser(description="Generate FIX session log")
    parser.add_argument("--output", default=str(OUTPUT),
                        help=f"Output path (default: {OUTPUT})")
    args = parser.parse_args()
    generate(Path(args.output))


if __name__ == "__main__":
    main()
