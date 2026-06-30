"""
reversal_lag.py — how late is the agent's "trend reversed" detection?

Parses every "INVALIDATED ... tape reversed to {up|down}" event from the prod
logs, then compares each against official Dhan 1-min candles:
  • when the underlying ACTUALLY pivoted (recent swing high/low before detection)
  • when the agent DETECTED + confirmed the reversal (the log timestamp)
  • the lag between them, how far price had already moved before the agent reacted,
  • and whether price KEPT going (correct-but-late cut) or snapped back (whipsaw).

Run locally (creds in .env) or on the server.
"""
import os
import re
import sys
import glob
import json
import time
from datetime import datetime, timedelta, time as dtime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.market_data_provider import get_session, get_historical_intraday

LOGDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prod_data")
CACHE = os.path.join(LOGDIR, "_bars_cache.json")
PAT = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*Call #(\d+) INVALIDATED @ [\d.]+ \((\w+)[^)]*\).*tape reversed to (up|down)")
LOOKBACK_MIN = 150          # how far back to search for the swing pivot
SWING_W = 3                 # a local extreme = max/min of +/- this many 1-min bars
POST_MIN = 30
SESSION_OPEN, SESSION_CLOSE = dtime(9, 15), dtime(15, 30)


def recent_swing(before, kind):
    """Most recent confirmed local extreme (fractal pivot) in `before`."""
    n = len(before)
    for i in range(n - 1 - SWING_W, SWING_W - 1, -1):
        seg = before[i - SWING_W:i + SWING_W + 1]
        if kind == "high" and before[i]["high"] == max(b["high"] for b in seg):
            return before[i]
        if kind == "low" and before[i]["low"] == min(b["low"] for b in seg):
            return before[i]
    return (max(before, key=lambda b: b["high"]) if kind == "high"
            else min(before, key=lambda b: b["low"]))


def parse_events():
    evs = []
    for fn in sorted(glob.glob(os.path.join(LOGDIR, "agent-2026-*.log"))):
        for line in open(fn, encoding="utf-8", errors="ignore"):
            m = PAT.search(line)
            if m:
                evs.append({
                    "dt": datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"),
                    "call_id": int(m.group(2)), "undl": m.group(3), "to": m.group(4),
                })
    return evs


def _auth(retries=5, wait=40):
    for i in range(retries):
        try:
            return get_session()
        except Exception as e:  # noqa: BLE001
            print(f"auth {i+1}/{retries} failed ({str(e)[:50]}) - waiting {wait}s")
            time.sleep(wait)
    raise SystemExit("auth failed")


def main():
    evs = parse_events()
    print(f"parsed {len(evs)} reversal-detection events\n")

    disk = {}
    if os.path.exists(CACHE):
        disk = json.load(open(CACHE, encoding="utf-8"))  # {"UNDL|date": [[iso,o,h,l,c],...]}
    session = None
    need = {f"{e['undl']}|{e['dt'].date().isoformat()}" for e in evs}
    if not need.issubset(disk):
        session = _auth()

    def bars_for(undl, day):
        key = f"{undl}|{day.isoformat()}"
        if key not in disk:
            time.sleep(1.2)
            b = get_historical_intraday(session, undl, "1", from_date=day, to_date=day)
            rows = [[x["ts"].isoformat(), x["open"], x["high"], x["low"], x["close"]]
                    for x in b if x.get("ts") and SESSION_OPEN <= x["ts"].time() <= SESSION_CLOSE]
            disk[key] = sorted(rows)
            json.dump(disk, open(CACHE, "w", encoding="utf-8"))
        return [{"ts": datetime.fromisoformat(r[0]), "open": r[1], "high": r[2],
                 "low": r[3], "close": r[4]} for r in disk[key]]

    results = []
    for e in evs:
        day = e["dt"].date()
        bars = bars_for(e["undl"], day)
        if not bars:
            continue
        T = e["dt"]
        before = [b for b in bars if b["ts"] <= T and b["ts"] >= T - timedelta(minutes=LOOKBACK_MIN)]
        after = [b for b in bars if T < b["ts"] <= T + timedelta(minutes=POST_MIN)]
        if len(before) < 3:
            continue
        px_T = before[-1]["close"]
        if e["to"] == "down":   # market topped → pivot = recent swing HIGH
            piv = recent_swing(before, "high")
            pivot_px, pivot_t = piv["high"], piv["ts"]
            pre_move = (pivot_px - px_T) / pivot_px * 100            # how far already fallen
            cont = (px_T - min((b["low"] for b in after), default=px_T)) / px_T * 100  # kept falling
            snap = (max((b["high"] for b in after), default=px_T) - px_T) / px_T * 100  # bounced back
        else:                   # market bottomed → pivot = recent swing LOW
            piv = recent_swing(before, "low")
            pivot_px, pivot_t = piv["low"], piv["ts"]
            pre_move = (px_T - pivot_px) / pivot_px * 100
            cont = (max((b["high"] for b in after), default=px_T) - px_T) / px_T * 100
            snap = (px_T - min((b["low"] for b in after), default=px_T)) / px_T * 100
        lag = (T - pivot_t).total_seconds() / 60.0
        results.append({**e, "lag": lag, "pre_move": pre_move, "cont": cont, "snap": snap})

    if not results:
        print("no results (no Dhan bars matched — check 1-min history range).")
        return

    n = len(results)
    avg = lambda k: sum(r[k] for r in results) / n
    med = lambda k: sorted(r[k] for r in results)[n // 2]
    print(f"=== reversal-detection lag vs real Dhan pivots ({n} events) ===")
    print(f"avg LAG (pivot -> agent confirmed): {avg('lag'):.1f} min   median {med('lag'):.1f} min")
    print(f"avg move ALREADY DONE before agent reacted: {avg('pre_move'):.2f}%")
    print(f"after the cut, next {POST_MIN}min: continued {avg('cont'):.2f}%  vs snapped-back {avg('snap'):.2f}%")
    good = sum(1 for r in results if r["cont"] >= r["snap"])
    print(f"cut was CORRECT-but-late (price kept going): {good}/{n} ({100*good/n:.0f}%)  | "
          f"WHIPSAW (snapped back further): {n-good}/{n} ({100*(n-good)/n:.0f}%)")
    print(f"lag > 15min: {sum(1 for r in results if r['lag']>15)}/{n}   "
          f"lag > 30min: {sum(1 for r in results if r['lag']>30)}/{n}")
    print("\nexamples (call, undl, to, lag min, pre-move%, kept%, snapback%):")
    for r in sorted(results, key=lambda r: -r["lag"])[:10]:
        print(f"  #{r['call_id']:<5} {r['undl']:<9} ->{r['to']:<4} lag {r['lag']:5.1f}m  "
              f"pre {r['pre_move']:+.2f}%  kept {r['cont']:.2f}%  snap {r['snap']:.2f}%")


if __name__ == "__main__":
    main()
