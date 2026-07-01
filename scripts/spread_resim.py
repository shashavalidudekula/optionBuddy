"""
spread_resim.py — what would the opt_sell_spread book have made if HELD to EOD
instead of being insta-stopped?

For each spread trade in paper_fills, parse the legs, take the entry credit + qty,
then value the spread at the day's CLOSE using the real Dhan underlying close
(intrinsic approximation — ignores residual time value, so it slightly flatters
the OTM/favourable side). Compares the actual (buggy) P&L vs the held-to-EOD P&L,
and reports how often the writer thesis actually finished on the winning side.
"""
import os
import sys
import csv
import json
import time
from collections import defaultdict
from datetime import datetime, time as dtime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.market_data_provider import get_session, get_historical_intraday

LOGDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prod_data")
CACHE = os.path.join(LOGDIR, "_bars_cache.json")
SESSION_OPEN, SESSION_CLOSE = dtime(9, 15), dtime(15, 30)


def _auth(retries=5, wait=40):
    for i in range(retries):
        try:
            return get_session()
        except Exception as e:  # noqa: BLE001
            print(f"auth {i+1}/{retries} failed ({str(e)[:50]}) - waiting {wait}s")
            time.sleep(wait)
    raise SystemExit("auth failed")


def parse_spread(instr):
    # "NIFTY 23850/23750 PE spread" -> (undl, K1_sold, K2_bought, type)
    parts = instr.split()
    if len(parts) < 3 or "/" not in parts[1]:
        return None
    undl = parts[0].upper()
    k1, k2 = (float(x) for x in parts[1].split("/"))
    typ = parts[2].upper()
    return undl, k1, k2, typ


def short_spread_value(typ, k1, k2, s):
    """Cost to BUY BACK the short spread at underlying S (intrinsic, per unit)."""
    if typ == "PE":   # bull put: sold k1 (>k2)
        v = max(0.0, k1 - s) - max(0.0, k2 - s)
        return min(max(v, 0.0), abs(k1 - k2))
    # CE bear call: sold k1 (<k2)
    v = max(0.0, s - k1) - max(0.0, s - k2)
    return min(max(v, 0.0), abs(k2 - k1))


def main():
    fills = list(csv.DictReader(open(os.path.join(LOGDIR, "paper_fills.csv"), encoding="utf-8")))
    pos = defaultdict(lambda: {"fills": []})
    for r in fills:
        if r["strategy"] == "opt_sell_spread":
            pos[r["position_id"]]["fills"].append(r)

    disk = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    trades = []
    for pid, p in pos.items():
        fs = sorted(p["fills"], key=lambda r: r["ts"])
        entry = next((f for f in fs if f["kind"] == "entry"), fs[0])
        sp = parse_spread(entry["instrument"])
        if not sp:
            continue
        trades.append({
            "instr": entry["instrument"], "undl": sp[0], "k1": sp[1], "k2": sp[2], "typ": sp[3],
            "credit": float(entry["price"]), "qty": int(float(entry["qty"])),
            "ts": entry["ts"], "actual_pnl": sum(float(f["realized_pnl"] or 0) for f in fs),
        })

    need = {f"{t['undl']}|{t['ts'][:10]}" for t in trades}
    session = _auth() if not need.issubset(disk) else None

    def eod_close(undl, day):
        key = f"{undl}|{day}"
        if key not in disk:
            time.sleep(1.2)
            b = get_historical_intraday(session, undl, "1", from_date=day, to_date=day)
            disk[key] = sorted([x["ts"].isoformat(), x["open"], x["high"], x["low"], x["close"]]
                               for x in b if x.get("ts")
                               and SESSION_OPEN <= x["ts"].time() <= SESSION_CLOSE)
            json.dump(disk, open(CACHE, "w", encoding="utf-8"))
        return disk[key][-1][4] if disk[key] else None

    act_tot = held_tot = 0.0
    favns = 0
    out = []
    for t in trades:
        s_eod = eod_close(t["undl"], t["ts"][:10])
        if s_eod is None:
            continue
        val = short_spread_value(t["typ"], t["k1"], t["k2"], s_eod)
        held = (t["credit"] - val) * t["qty"]
        fav = val < t["credit"]
        favns += 1 if fav else 0
        act_tot += t["actual_pnl"]
        held_tot += held
        out.append((t, s_eod, val, held))

    n = len(out)
    print(f"=== spread re-sim: actual (insta-stop) vs held-to-EOD ({n} trades) ===")
    print(f"ACTUAL book P&L (buggy stops): {act_tot:+,.0f}")
    print(f"HELD-to-EOD P&L (intrinsic approx): {held_tot:+,.0f}")
    print(f"finished on the WINNING side (underlying favourable at close): {favns}/{n} ({100*favns/n:.0f}%)")
    print(f"held-to-EOD winners: {sum(1 for _,_,_,h in out if h>0)}/{n}")
    print("\nper trade (instr, S_eod, actual, held-to-EOD):")
    for t, s, v, h in sorted(out, key=lambda x: x[0]["ts"]):
        print(f"  {t['instr'][:32]:<32} S={s:>9.0f}  actual {t['actual_pnl']:>+8,.0f}  held {h:>+8,.0f}")


if __name__ == "__main__":
    main()
