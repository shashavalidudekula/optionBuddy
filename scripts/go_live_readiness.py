"""go_live_readiness.py — measured PASS/FAIL gate for flipping to live execution.

Reads the durable paper ledger (logs/paper_history.jsonl) and checks it against the
PRE-COMMITTED thresholds below. The numbers only mean something now that paper fills
are realistic (slippage + stop-books-at-observed-price; see PAPER_SLIPPAGE_PCT) — a
pass on the idealised pre-fix simulator would have been a mirage.

A round trip = one position (rows sharing `position_id`; legacy rows without one each
count as a single position). Everything is NET of costs.

The thresholds are deliberately fixed. Do NOT relax them to make a struggling
strategy "pass" — that defeats the entire point of pre-committing. See
GO_LIVE_CRITERIA.md.

Usage:
    python -m scripts.go_live_readiness                 # whole history
    python -m scripts.go_live_readiness --since 2026-07-01   # out-of-sample window
    python -m scripts.go_live_readiness --since 2026-07-01 --start-capital 100000

Exit code: 0 = READY, 1 = NOT READY (so it can gate CI / a deploy step).
"""
import argparse
import json
import math
import os
import statistics
import sys

# Work whether invoked as `python -m scripts.go_live_readiness` or `python
# scripts/go_live_readiness.py` — ensure the repo root is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import PAPER_START_CAPITAL  # noqa: E402
from data.paper_history import _read_records  # noqa: E402  (internal but stable parse helper)

# ── PRE-COMMITTED thresholds (change only with explicit sign-off) ─────────────
MIN_TRADES = 100        # round trips — enough that the edge isn't a small-sample fluke
MIN_DAYS = 20           # distinct trading sessions — span multiple days/regimes
MIN_T_STAT = 2.0        # expectancy must be >0 with ~95% confidence it isn't noise
MIN_PROFIT_FACTOR = 1.3  # net winnings / net losses
MAX_DD_PCT = 15.0       # peak-to-trough net drawdown, as % of start capital


def _positions(records: list[dict]) -> list[dict]:
    """Collapse rows into round trips; return them ordered oldest→newest by exit ts."""
    groups: dict = {}
    for i, r in enumerate(records):
        pid = r.get("position_id")
        gid = pid if pid is not None else f"_legacy{i}"
        g = groups.setdefault(gid, {"net": 0.0, "ts": "", "date": ""})
        g["net"] += float(r.get("pnl") or 0)
        ts = str(r.get("ts") or "")
        if ts >= g["ts"]:           # keep the latest row's ts/date as the close
            g["ts"], g["date"] = ts, r.get("date") or g["date"]
    return sorted(groups.values(), key=lambda g: g["ts"])


def _read_jsonl(path: str) -> list[dict]:
    """Parse a paper_history.jsonl at an explicit path (oldest first)."""
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _max_drawdown(nets: list[float]) -> float:
    """Largest peak-to-trough drop of cumulative net P&L (in rupees)."""
    peak = cum = dd = 0.0
    for n in nets:
        cum += n
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def evaluate(records: list[dict], start_capital: float) -> tuple[bool, list[str]]:
    positions = _positions(records)
    nets = [p["net"] for p in positions]
    n = len(nets)
    out: list[str] = []

    if n == 0:
        return False, ["No closed paper trades in the selected window — nothing to evaluate."]

    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    days = len({p["date"] for p in positions if p["date"]})
    total = sum(nets)
    expectancy = total / n
    stdev = statistics.stdev(nets) if n > 1 else 0.0
    t_stat = (expectancy / (stdev / math.sqrt(n))) if stdev > 0 and n > 1 else 0.0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else math.inf
    win_rate = 100.0 * len(wins) / n
    max_dd = _max_drawdown(nets)
    max_dd_pct = (max_dd / start_capital * 100) if start_capital else math.inf

    out.append(f"Round trips      : {n}")
    out.append(f"Trading days     : {days}")
    out.append(f"Win rate         : {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    out.append(f"Net P&L          : Rs {total:,.0f}")
    out.append(f"Expectancy/trade : Rs {expectancy:,.0f}   (t-stat {t_stat:.2f})")
    out.append(f"Profit factor    : {profit_factor:.2f}")
    out.append(f"Max drawdown     : Rs {max_dd:,.0f}  ({max_dd_pct:.1f}% of Rs {start_capital:,.0f})")
    out.append("")

    checks = [
        (f"trades >= {MIN_TRADES}", n >= MIN_TRADES),
        (f"trading days >= {MIN_DAYS}", days >= MIN_DAYS),
        ("expectancy > 0", expectancy > 0),
        (f"t-stat >= {MIN_T_STAT}", t_stat >= MIN_T_STAT),
        (f"profit factor >= {MIN_PROFIT_FACTOR}", profit_factor >= MIN_PROFIT_FACTOR),
        (f"max drawdown <= {MAX_DD_PCT}%", max_dd_pct <= MAX_DD_PCT),
    ]
    ready = all(ok for _, ok in checks)
    for label, ok in checks:
        out.append(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ready, out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Measured go-live readiness gate.")
    ap.add_argument("--since", help="only count trades on/after this date (YYYY-MM-DD) — "
                                    "use the parameter-FREEZE date for an out-of-sample check")
    ap.add_argument("--strategy", help="evaluate only this strategy (e.g. opt_buy, opt_sell_spread, "
                                       "equity_cash); default: all strategies combined")
    ap.add_argument("--history", help="path to a paper_history.jsonl to evaluate "
                                      "(e.g. prod_data/paper_history.jsonl); "
                                      "default: the live logs/paper_history.jsonl")
    ap.add_argument("--start-capital", type=float, default=PAPER_START_CAPITAL,
                    help="capital base for the drawdown %% (default: PAPER_START_CAPITAL)")
    args = ap.parse_args(argv)

    # Be robust on a legacy Windows console (cp1252) regardless of any stray glyphs.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    try:
        records = _read_jsonl(args.history) if args.history else _read_records()
    except FileNotFoundError:
        print(f"History file not found: {args.history}")
        return 1
    if args.since:
        records = [r for r in records if str(r.get("date", "")) >= args.since]
    if args.strategy:
        records = [r for r in records if str(r.get("strategy", "opt_buy")) == args.strategy]

    src = args.history if args.history else "live"
    window = f"since {args.since}" if args.since else "full history"
    strategy_scope = f" [{args.strategy}]" if args.strategy else ""
    print(f"OptionBuddy - go-live readiness ({window}; src={src}){strategy_scope}")
    print("=" * 52)
    ready, lines = evaluate(records, args.start_capital)
    print("\n".join(lines))
    print("=" * 52)
    print("RESULT: " + ("[READY] for a live dry-run" if ready
                        else "[NOT READY] - keep running in paper"))
    if ready and not args.since:
        print("Note: a full-history pass is necessary but not sufficient — re-run with "
              "--since <freeze-date> to confirm the edge holds OUT OF SAMPLE.")
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
