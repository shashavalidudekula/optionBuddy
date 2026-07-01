"""
chop_filter.py — did the opt_buy stop-outs cluster on CHOPPY entries?

For each opt_buy paper trade, measure the underlying's trend regime in the 45 min
BEFORE entry via Kaufman's Efficiency Ratio (ER = |net move| / |total path|;
~1 = clean trend, ~0 = chop). Then bucket trades by ER and show win rate, total
P&L and stop-out rate per bucket — i.e. how much a "don't enter in chop" filter
would have saved. Uses real Dhan 1-min candles (cached to prod_data/_bars_cache.json).
"""
import os
import sys
import csv
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, time as dtime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.market_data_provider import get_session, get_historical_intraday

LOGDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prod_data")
CACHE = os.path.join(LOGDIR, "_bars_cache.json")
WIN_MIN = 45
SESSION_OPEN, SESSION_CLOSE = dtime(9, 15), dtime(15, 30)


def _auth(retries=5, wait=40):
    for i in range(retries):
        try:
            return get_session()
        except Exception as e:  # noqa: BLE001
            print(f"auth {i+1}/{retries} failed ({str(e)[:50]}) - waiting {wait}s")
            time.sleep(wait)
    raise SystemExit("auth failed")


def main():
    fills = list(csv.DictReader(open(os.path.join(LOGDIR, "paper_fills.csv"), encoding="utf-8")))
    calls = {r["id"]: r for r in csv.DictReader(open(os.path.join(LOGDIR, "calls.csv"), encoding="utf-8"))}

    # opt_buy positions: entry ts + underlying + total realized pnl + call_id
    pos = defaultdict(lambda: {"pnl": 0.0, "entry": None, "instr": None, "call": None})
    for r in fills:
        if r["strategy"] != "opt_buy":
            continue
        p = pos[r["position_id"]]
        p["pnl"] += float(r["realized_pnl"] or 0)
        p["call"] = r["call_id"]
        if r["kind"] == "entry":
            p["entry"] = r["ts"]
            p["instr"] = r["instrument"]
    trades = [p for p in pos.values() if p["entry"] and p["instr"]]
    print(f"opt_buy trades: {len(trades)}")

    disk = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}

    def undl_of(instr):
        u = instr.split()[0].upper()
        return u if u in ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY") else u

    need = set()
    for t in trades:
        d = t["entry"][:10]
        need.add(f"{undl_of(t['instr'])}|{d}")
    session = _auth() if not need.issubset(disk) else None

    def bars_for(undl, day):
        key = f"{undl}|{day}"
        if key not in disk:
            time.sleep(1.2)
            b = get_historical_intraday(session, undl, "1", from_date=day, to_date=day)
            disk[key] = sorted([x["ts"].isoformat(), x["open"], x["high"], x["low"], x["close"]]
                               for x in b if x.get("ts")
                               and SESSION_OPEN <= x["ts"].time() <= SESSION_CLOSE)
            json.dump(disk, open(CACHE, "w", encoding="utf-8"))
        return disk[key]

    def er_before(undl, entry_ts):
        day = entry_ts[:10]
        T = datetime.fromisoformat(entry_ts.replace(" ", "T")[:19])
        rows = bars_for(undl, day)
        closes = [r[4] for r in rows
                  if T - timedelta(minutes=WIN_MIN) <= datetime.fromisoformat(r[0]) <= T]
        if len(closes) < 8:
            return None
        path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
        return abs(closes[-1] - closes[0]) / path if path else 0.0

    for t in trades:
        t["er"] = er_before(undl_of(t["instr"]), t["entry"])
        c = calls.get(t["call"]) or {}
        t["status"] = c.get("status", "")

    scored = [t for t in trades if t["er"] is not None]
    print(f"scored with ER: {len(scored)}\n")

    def bucket(lo, hi, label):
        sub = [t for t in scored if lo <= t["er"] < hi]
        if not sub:
            return
        n = len(sub)
        wins = sum(1 for t in sub if t["pnl"] > 0)
        tot = sum(t["pnl"] for t in sub)
        sl = sum(1 for t in sub if t["status"] == "sl_hit")
        print(f"{label:<22} n={n:>3}  win {100*wins/n:>3.0f}%  stop-out {100*sl/n:>3.0f}%  "
              f"total P&L {tot:>+9,.0f}  avg {tot/n:>+7,.0f}")

    print(f"{'ER bucket (45m before)':<22}{'':>5}{'':>9}{'':>11}")
    bucket(0.0, 0.20, "0.00-0.20 deep chop")
    bucket(0.20, 0.35, "0.20-0.35 chop")
    bucket(0.35, 0.50, "0.35-0.50 mixed")
    bucket(0.50, 1.01, "0.50+ trending")

    chop = [t for t in scored if t["er"] < 0.35]
    rest = [t for t in scored if t["er"] >= 0.35]
    print(f"\nIf we SKIP entries with ER<0.35 (chop):")
    print(f"  skipped {len(chop)} trades worth {sum(t['pnl'] for t in chop):+,.0f}  "
          f"(kept {len(rest)} worth {sum(t['pnl'] for t in rest):+,.0f})")


if __name__ == "__main__":
    main()
