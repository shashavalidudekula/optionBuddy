"""
opening_analysis.py — Nifty opening dynamics on OFFICIAL Dhan candles.

Pulls the last ~1 month of 5-min intraday + daily candles via the Dhan charts API
and prints, day by day: prev close, open, gap, first-15-min volatility, how the day
resolved, whether the opening direction persisted, and whether the day whipsawed
(broke both sides of the opening range). Then aggregates the trends — the data
behind "don't trade the open as an option buyer".

Run on the server (where the Dhan session authenticates):
    docker compose exec advisory-agent python scripts/opening_analysis.py
Optional arg: underlying (default NIFTY), e.g. `... opening_analysis.py BANKNIFTY`.
"""
import os
import sys
import time
from collections import defaultdict
from datetime import time as dtime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.market_data_provider import (
    get_session, get_historical_intraday, get_historical_daily,
)

SESSION_OPEN, SESSION_CLOSE = dtime(9, 15), dtime(15, 30)


def _pct(a, b):
    return (a - b) / b * 100 if b else float("nan")


def _auth(retries: int = 4):
    """get_session() with retry — the TOTP auto-token can race the 30s window."""
    for i in range(retries):
        try:
            return get_session()
        except Exception as e:  # noqa: BLE001
            print(f"auth attempt {i+1}/{retries} failed ({str(e)[:60]}) — retrying…")
            time.sleep(16)
    raise SystemExit("Dhan auth failed after retries.")


def main():
    underlying = (sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper()
    session = _auth()

    intra = get_historical_intraday(session, underlying, interval="5", days=40)
    daily = get_historical_daily(session, underlying, days=90)
    vix = get_historical_daily(session, "INDIAVIX", days=90)

    if not intra:
        print("No intraday candles returned from Dhan. Check the Data API subscription "
              "and that /charts/intraday is enabled for your account.")
        return

    # tz sanity — print the first/last bar so we can confirm the session window
    print(f"Underlying: {underlying} | intraday bars: {len(intra)} | "
          f"first {intra[0]['ts']} | last {intra[-1]['ts']}\n")

    daily_close = sorted((b["ts"].date(), b["close"]) for b in daily if b.get("ts"))
    vix_close = sorted((b["ts"].date(), b["close"]) for b in vix if b.get("ts"))

    def prev_close_for(d):
        prev = None
        for dd, c in daily_close:
            if dd < d:
                prev = c
            else:
                break
        return prev

    def vix_for(d):
        val = None
        for dd, c in vix_close:
            if dd <= d:
                val = c
            else:
                break
        return val

    by_day = defaultdict(list)
    for b in intra:
        ts = b.get("ts")
        if ts and SESSION_OPEN <= ts.time() <= SESSION_CLOSE:  # drop after-hours/flat bars
            by_day[ts.date()].append(b)

    rows = []
    for day in sorted(by_day):
        g = sorted(by_day[day], key=lambda b: b["ts"])
        if len(g) < 4:
            continue
        o = g[0]["open"]
        first15 = g[:3]
        f_hi = max(b["high"] for b in first15)
        f_lo = min(b["low"] for b in first15)
        f_close = first15[-1]["close"]
        d_close = g[-1]["close"]
        rest = g[3:]
        broke_up = any(b["high"] > f_hi for b in rest)
        broke_dn = any(b["low"] < f_lo for b in rest)
        prev_close = prev_close_for(day)
        rows.append({
            "date": str(day),
            "gap": _pct(o, prev_close) if prev_close else float("nan"),
            "or_pct": (f_hi - f_lo) / o * 100,
            "f15": _pct(f_close, o),
            "day": _pct(d_close, o),
            "persist": (_pct(f_close, o) > 0) == (_pct(d_close, o) > 0),
            "whipsaw": bool(broke_up and broke_dn),
            "vix": vix_for(day),
        })

    if not rows:
        print("Grouped candles but found no full sessions — check the bar timestamps above.")
        return

    hdr = f"{'date':<11}{'gap%':>7}{'open_rng%':>10}{'first15%':>10}{'day%':>8}{'persist':>9}{'whipsaw':>9}{'vix':>7}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        v = f"{r['vix']:.1f}" if r["vix"] is not None else "  -"
        print(f"{r['date']:<11}{r['gap']:>7.2f}{r['or_pct']:>10.2f}{r['f15']:>10.2f}"
              f"{r['day']:>8.2f}{str(r['persist']):>9}{str(r['whipsaw']):>9}{v:>7}")

    n = len(rows)
    gaps = [abs(r["gap"]) for r in rows if r["gap"] == r["gap"]]
    ors = [r["or_pct"] for r in rows]
    persist = [r["persist"] for r in rows]
    whips = [r["whipsaw"] for r in rows]
    med_or = sorted(ors)[len(ors) // 2]
    wide = [r["persist"] for r in rows if r["or_pct"] >= med_or]
    narrow = [r["persist"] for r in rows if r["or_pct"] < med_or]

    print(f"\n=== Aggregates over {n} sessions ===")
    print(f"Avg |gap| vs prev close      : {sum(gaps)/len(gaps):.2f}%")
    print(f"Avg first-15min range (vol)  : {sum(ors)/n:.2f}%   median {med_or:.2f}%   max {max(ors):.2f}%")
    print(f"First-15 dir == day dir      : {100*sum(persist)/n:.0f}%   (fails {100*(1-sum(persist)/n):.0f}%)")
    print(f"Whipsaw (broke BOTH sides)   : {100*sum(whips)/n:.0f}% of days")
    if wide and narrow:
        print(f"Persistence wide-open days   : {100*sum(wide)/len(wide):.0f}%  |  narrow-open: {100*sum(narrow)/len(narrow):.0f}%")


if __name__ == "__main__":
    main()
