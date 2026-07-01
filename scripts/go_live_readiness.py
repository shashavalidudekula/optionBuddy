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
# The gate itself lives in a dependency-free module shared with the backtester, so
# "passes the backtest" and "passes the live gate" mean exactly the same thing.
from core.backtest.metrics import evaluate  # noqa: E402,F401


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
