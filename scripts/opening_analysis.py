"""
opening_analysis.py — Nifty opening dynamics on OFFICIAL Dhan candles.

Pulls intraday (5-min) + daily candles via the Dhan charts API over a window
(default ~6 months, intraday fetched in chunks since Dhan caps per-request range)
and reports the opening behaviour: gap, first-15-min volatility, how the day
resolved, opening-direction persistence, whipsaw, the gap-fade tendency, plus
splits by VIX regime and by month.

Run on the server (or locally — prod creds are in .env):
    docker compose exec advisory-agent python scripts/opening_analysis.py
Args: [underlying] [days]   e.g.  `... opening_analysis.py NIFTY 190`
"""
import os
import sys
import time
from collections import defaultdict
from datetime import date, time as dtime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.market_data_provider import (
    get_session, get_historical_intraday, get_historical_daily,
)

SESSION_OPEN, SESSION_CLOSE = dtime(9, 15), dtime(15, 30)


def _pct(a, b):
    return (a - b) / b * 100 if b else float("nan")


def _auth(retries: int = 5, wait: int = 40):
    """get_session() with patient retry — Dhan's TOTP auto-token rate-limits rapid
    repeats, so wait a full window+ between attempts (don't hammer it)."""
    for i in range(retries):
        try:
            return get_session()
        except Exception as e:  # noqa: BLE001
            print(f"auth attempt {i+1}/{retries} failed ({str(e)[:60]}) - waiting {wait}s...")
            time.sleep(wait)
    raise SystemExit("Dhan auth failed after retries.")


def _corr(a, b):
    n = len(a)
    if n < 2:
        return float("nan")
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a) ** 0.5
    vb = sum((y - mb) ** 2 for y in b) ** 0.5
    return cov / (va * vb) if va and vb else float("nan")


_THROTTLE = 1.2  # seconds between Dhan /charts calls — the endpoint rate-limits bursts


def fetch_intraday_window(session, underlying, total_days, chunk=60):
    """Fetch intraday 5-min over total_days, in <=chunk-day requests, deduped."""
    end = date.today()
    start = end - timedelta(days=total_days)
    seen, bars = set(), []
    d = start
    while d <= end:
        cend = min(d + timedelta(days=chunk), end)
        part = get_historical_intraday(session, underlying, "5", from_date=d, to_date=cend)
        for b in part:
            ts = b.get("ts")
            if ts and ts.isoformat() not in seen:
                seen.add(ts.isoformat())
                bars.append(b)
        d = cend + timedelta(days=1)
        time.sleep(_THROTTLE)
    bars.sort(key=lambda b: b["ts"])
    return bars


def fetch_daily_retry(session, underlying, days, tries=4):
    """Daily candles with backoff — survives the occasional 429 on /charts."""
    for i in range(tries):
        time.sleep(_THROTTLE)
        bars = get_historical_daily(session, underlying, days=days)
        if bars:
            return bars
        time.sleep(2.0 * (i + 1))
    return []


def main():
    underlying = (sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper()
    total_days = int(sys.argv[2]) if len(sys.argv) > 2 else 190
    session = _auth()

    intra = fetch_intraday_window(session, underlying, total_days)
    daily = fetch_daily_retry(session, underlying, days=total_days + 10)
    vix = fetch_daily_retry(session, "INDIAVIX", days=total_days + 10)
    if not vix:
        print("(note: India VIX history unavailable — VIX-regime split will be empty)")
    if not intra:
        print("No intraday candles from Dhan — check the Data API subscription / range limits.")
        return
    print(f"{underlying} | {len(intra)} intraday bars | {intra[0]['ts']} to {intra[-1]['ts']}\n")

    daily_close = sorted((b["ts"].date(), b["close"]) for b in daily if b.get("ts"))
    vix_close = sorted((b["ts"].date(), b["close"]) for b in vix if b.get("ts"))

    def prev_close_for(dd):
        prev = None
        for x, c in daily_close:
            if x < dd:
                prev = c
            else:
                break
        return prev

    def vix_for(dd):
        val = None
        for x, c in vix_close:
            if x <= dd:
                val = c
            else:
                break
        return val

    by_day = defaultdict(list)
    for b in intra:
        ts = b.get("ts")
        if ts and SESSION_OPEN <= ts.time() <= SESSION_CLOSE:
            by_day[ts.date()].append(b)

    rows = []
    for day in sorted(by_day):
        g = sorted(by_day[day], key=lambda b: b["ts"])
        if len(g) < 4:
            continue
        o = g[0]["open"]
        f = g[:3]
        f_hi, f_lo, f_close = max(b["high"] for b in f), min(b["low"] for b in f), f[-1]["close"]
        d_close = g[-1]["close"]
        rest = g[3:]
        pc = prev_close_for(day)
        rows.append({
            "month": str(day)[:7], "gap": _pct(o, pc) if pc else float("nan"),
            "or": (f_hi - f_lo) / o * 100, "f15": _pct(f_close, o), "day": _pct(d_close, o),
            "persist": (_pct(f_close, o) > 0) == (_pct(d_close, o) > 0),
            "whip": any(b["high"] > f_hi for b in rest) and any(b["low"] < f_lo for b in rest),
            "vix": vix_for(day),
        })
    rows = [r for r in rows if r["gap"] == r["gap"]]  # drop days w/o prev close
    n = len(rows)

    def rate(sub, key):
        return 100 * sum(1 for r in sub if r[key]) / len(sub) if sub else 0.0

    def faded(sub):
        return 100 * sum(1 for r in sub if (r["gap"] > 0) != (r["f15"] > 0) and r["f15"] != 0) / len(sub) if sub else 0.0

    print(f"=== OVERALL ({n} sessions) ===")
    print(f"Avg |gap|: {sum(abs(r['gap']) for r in rows)/n:.2f}%  | avg first-15 range: {sum(r['or'] for r in rows)/n:.2f}%")
    print(f"Persist (open dir = day dir): {rate(rows,'persist'):.0f}%   Whipsaw: {rate(rows,'whip'):.0f}%")
    print(f"corr(gap, first15): {_corr([r['gap'] for r in rows],[r['f15'] for r in rows]):+.2f}   "
          f"corr(gap, day): {_corr([r['gap'] for r in rows],[r['day'] for r in rows]):+.2f}")

    print("\n=== GAP-FADE BUCKETS (does the open fade the gap?) ===")
    print(f"{'bucket':<18}{'n':>4}{'faded@15%':>11}{'held-close%':>12}{'whipsaw%':>10}")
    def show(lo, hi, label):
        sub = [r for r in rows if lo <= r["gap"] < hi]
        if not sub:
            return
        held = 100 * sum(1 for r in sub if (r["gap"] > 0) == (r["day"] > 0)) / len(sub)
        print(f"{label:<18}{len(sub):>4}{faded(sub):>11.0f}{held:>12.0f}{rate(sub,'whip'):>10.0f}")
    show(0.6, 9, "gap-up >0.6%"); show(0.3, 0.6, "gap-up 0.3-0.6%"); show(0.0, 0.3, "gap-up 0-0.3%")
    show(-0.3, 0.0, "gap-dn 0-0.3%"); show(-0.6, -0.3, "gap-dn 0.3-0.6%"); show(-9, -0.6, "gap-dn >0.6%")

    print("\n=== BY VIX REGIME ===")
    print(f"{'regime':<14}{'n':>4}{'persist%':>10}{'whipsaw%':>10}{'gapfade%':>10}")
    for label, lo, hi in [("low <14", 0, 14), ("mid 14-17", 14, 17), ("high >=17", 17, 99)]:
        sub = [r for r in rows if r["vix"] is not None and lo <= r["vix"] < hi]
        if sub:
            print(f"{label:<14}{len(sub):>4}{rate(sub,'persist'):>10.0f}{rate(sub,'whip'):>10.0f}{faded(sub):>10.0f}")

    print("\n=== BY MONTH ===")
    print(f"{'month':<9}{'n':>4}{'avg|gap|':>10}{'persist%':>10}{'whipsaw%':>10}{'avgVIX':>8}")
    for m in sorted({r["month"] for r in rows}):
        sub = [r for r in rows if r["month"] == m]
        vx = [r["vix"] for r in sub if r["vix"] is not None]
        print(f"{m:<9}{len(sub):>4}{sum(abs(r['gap']) for r in sub)/len(sub):>10.2f}"
              f"{rate(sub,'persist'):>10.0f}{rate(sub,'whip'):>10.0f}{(sum(vx)/len(vx) if vx else 0):>8.1f}")


if __name__ == "__main__":
    main()
