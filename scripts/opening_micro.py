"""
opening_micro.py — minute-by-minute open micro-structure on big-gap days (Dhan 1-min).

Tests the observed pattern: on a gap-up the first minute follows the gap (up),
then it fades down for ~3-4 minutes, then reconsolidates — so a single FADE scalp
(PE on gap-up / CE on gap-down) entered ~minute 1 and closed within ~5 minutes has
edge. Prints the average minute-by-minute path and the fade-scalp favourable/adverse
for several entry/exit minutes.

Run (creds are in .env, runs locally or on the server):
    python scripts/opening_micro.py            # NIFTY, ~90 days
    python scripts/opening_micro.py NIFTY 120
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

OPEN_T = dtime(9, 15)
GAP_MIN = 0.4          # "big" gap threshold (%)
N_MIN = 10             # how many opening minutes to examine
_THROTTLE = 1.2


def _auth(retries=5, wait=40):
    for i in range(retries):
        try:
            return get_session()
        except Exception as e:  # noqa: BLE001
            print(f"auth {i+1}/{retries} failed ({str(e)[:50]}) - waiting {wait}s...")
            time.sleep(wait)
    raise SystemExit("Dhan auth failed.")


def fetch_1min(session, underlying, total_days, chunk=5):
    end = date.today()
    d = end - timedelta(days=total_days)
    seen, bars = set(), []
    while d <= end:
        cend = min(d + timedelta(days=chunk), end)
        for b in get_historical_intraday(session, underlying, "1", from_date=d, to_date=cend):
            ts = b.get("ts")
            if ts and ts.isoformat() not in seen:
                seen.add(ts.isoformat())
                bars.append(b)
        d = cend + timedelta(days=1)
        time.sleep(_THROTTLE)
    bars.sort(key=lambda b: b["ts"])
    return bars


def fetch_daily(session, underlying, days, tries=4):
    for i in range(tries):
        time.sleep(_THROTTLE)
        bars = get_historical_daily(session, underlying, days=days)
        if bars:
            return bars
        time.sleep(2.0 * (i + 1))
    return []


def main():
    underlying = (sys.argv[1] if len(sys.argv) > 1 else "NIFTY").upper()
    total_days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    session = _auth()

    bars = fetch_1min(session, underlying, total_days)
    daily = fetch_daily(session, underlying, days=total_days + 10)
    if not bars:
        print("No 1-min candles from Dhan (1-min history may be limited). Try fewer days.")
        return
    print(f"{underlying} | {len(bars)} 1-min bars | {bars[0]['ts']} to {bars[-1]['ts']}\n")

    daily_close = sorted((b["ts"].date(), b["close"]) for b in daily if b.get("ts"))

    def prev_close(dd):
        prev = None
        for x, c in daily_close:
            if x < dd:
                prev = c
            else:
                break
        return prev

    by_day = defaultdict(list)
    for b in bars:
        ts = b.get("ts")
        if ts and ts.time() >= OPEN_T:
            by_day[ts.date()].append(b)

    up_days, dn_days = [], []          # each: first N minute bars
    for day in sorted(by_day):
        g = sorted(by_day[day], key=lambda b: b["ts"])
        if len(g) < N_MIN + 1 or g[0]["ts"].time() != OPEN_T:
            continue
        pc = prev_close(day)
        if not pc:
            continue
        gap = (g[0]["open"] - pc) / pc * 100
        seg = g[:N_MIN]
        if gap >= GAP_MIN:
            up_days.append(seg)
        elif gap <= -GAP_MIN:
            dn_days.append(seg)

    print(f"big gap-up days: {len(up_days)} | big gap-down days: {len(dn_days)} (|gap|>={GAP_MIN}%)\n")

    def avg_path(days):
        # avg close at minute i relative to the 9:15 open (% ), i = 0..N-1
        out = []
        for i in range(N_MIN):
            vals = [(seg[i]["close"] - seg[0]["open"]) / seg[0]["open"] * 100 for seg in days]
            out.append(sum(vals) / len(vals) if vals else 0.0)
        return out

    if up_days:
        p = avg_path(up_days)
        print("GAP-UP avg path (close vs 9:15 open, % per minute):")
        print("  min:   " + " ".join(f"{i:>6}" for i in range(N_MIN)))
        print("  move:  " + " ".join(f"{v:>+6.2f}" for v in p))
    if dn_days:
        p = avg_path(dn_days)
        print("GAP-DOWN avg path (close vs 9:15 open, % per minute):")
        print("  min:   " + " ".join(f"{i:>6}" for i in range(N_MIN)))
        print("  move:  " + " ".join(f"{v:>+6.2f}" for v in p))

    # Fade scalp: enter at the close of minute `e_in` in the fade direction,
    # exit at the close of minute `e_out`; favourable/adverse within the window.
    def scalp(days, direction):
        print(f"\n{('GAP-UP -> PE (fade down)' if direction=='up' else 'GAP-DOWN -> CE (bounce up)')} scalp:")
        for e_in in (1, 2):
            for e_out in (e_in + 2, e_in + 3, e_in + 4):
                rows = []
                for seg in days:
                    if e_out >= len(seg):
                        continue
                    entry = seg[e_in]["close"]
                    win = seg[e_in + 1:e_out + 1]
                    hi = max(b["high"] for b in win)
                    lo = min(b["low"] for b in win)
                    end = seg[e_out]["close"]
                    if direction == "up":   # PE: favourable = down
                        fav, adv, ex = (entry - lo) / entry * 100, (hi - entry) / entry * 100, (entry - end) / entry * 100
                    else:                    # CE: favourable = up
                        fav, adv, ex = (hi - entry) / entry * 100, (entry - lo) / entry * 100, (end - entry) / entry * 100
                    rows.append((fav, adv, ex))
                if not rows:
                    continue
                nn = len(rows)
                fv = sum(r[0] for r in rows) / nn
                ad = sum(r[1] for r in rows) / nn
                win = 100 * sum(1 for r in rows if r[2] > 0) / nn
                print(f"  enter min {e_in} -> exit min {e_out}  n={nn:>2}  fav {fv:.2f}%  adv {ad:.2f}%  "
                      f"fav/adv {fv/ad if ad else 0:.2f}  win {win:.0f}%")

    if up_days:
        scalp(up_days, "up")
    if dn_days:
        scalp(dn_days, "dn")


if __name__ == "__main__":
    main()
