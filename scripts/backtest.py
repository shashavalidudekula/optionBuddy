"""backtest.py — run a strategy over historical data and report the go-live gate.

The gatekeeper of the research-first workflow: a strategy is only worth papering if
it passes HERE first. Reuses the SAME metrics as the live gate
(scripts/go_live_readiness.evaluate), so a backtest PASS means the same thing.

Usage:
    # one-time: populate the local cache (needs a Dhan session)
    python -m scripts.backtest --strategy vol_seller --underlying NIFTY \
        --from 2023-01-01 --to 2025-12-31 --refresh

    # thereafter runs offline from cache
    python -m scripts.backtest --strategy vol_seller --underlying BANKNIFTY

Exit code: 0 = backtest PASSES the gate, 1 = fails / no data.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import PAPER_START_CAPITAL  # noqa: E402
from core.backtest import data, engine, equity_engine  # noqa: E402
from core.backtest.metrics import evaluate  # noqa: E402  (the shared go-live gate)
from core.strategies.vol_seller import VolSeller  # noqa: E402
from core.strategies.pairs import PairsStrategy  # noqa: E402

# Option strategies run on (underlying + VIX) via engine.run; "pairs" runs on two
# equity series via equity_engine.run_pairs (see the branch in main).
STRATEGIES = {"vol_seller": VolSeller}
ALL_STRATEGIES = sorted(list(STRATEGIES) + ["pairs"])


def _regime_breakdown(records: list, vix_bars: list) -> list[str]:
    """Split closed-trade P&L by the VIX tercile at ENTRY (low/mid/high vol)."""
    vix_by_date = {b["date"]: b["close"] for b in vix_bars}
    levels = sorted(vix_by_date.values())
    if len(levels) < 3:
        return []
    lo_cut = levels[len(levels) // 3]
    hi_cut = levels[2 * len(levels) // 3]
    buckets = {"low VIX": [], "mid VIX": [], "high VIX": []}
    for r in records:
        vix = vix_by_date.get(str(r.get("entry_date", ""))[:10])
        if vix is None:
            continue
        b = "low VIX" if vix <= lo_cut else ("high VIX" if vix > hi_cut else "mid VIX")
        buckets[b].append(r["pnl"])
    out = ["", "By entry-VIX regime:"]
    for name, pnls in buckets.items():
        if pnls:
            out.append(f"  {name:<9}: {len(pnls):>3} trades, net Rs {sum(pnls):>10,.0f}, "
                       f"avg Rs {sum(pnls)/len(pnls):>8,.0f}")
    return out


def _build_option_strategy(name: str, args):
    if name == "vol_seller":
        return VolSeller(iv_rank_min=args.iv_rank_min, iv_rank_max=args.iv_rank_max,
                         target_delta=args.delta, wing_pts=args.wing,
                         wing_delta=args.wing_delta, dte=args.dte)
    return STRATEGIES[name]()


def _pooled_curve(records: list, start_capital: float) -> list:
    """Cumulative realised-P&L equity curve from pooled trades (works across underlyings)."""
    by_date: dict = {}
    for r in sorted(records, key=lambda x: x["ts"]):
        by_date[r["date"]] = by_date.get(r["date"], 0.0) + r["pnl"]
    cum, out = start_capital, []
    for d in sorted(by_date):
        cum += by_date[d]
        out.append((d, round(cum, 2)))
    return out


def _save_curve(strategy: str, underlying: str, curve: list) -> str:
    os.makedirs("logs", exist_ok=True)
    path = os.path.join("logs", f"bt_{strategy}_{underlying}.csv")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("date,equity\n")
        for d, eq in curve:
            fh.write(f"{d},{eq}\n")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backtest a strategy against the go-live gate.")
    ap.add_argument("--strategy", required=True, choices=ALL_STRATEGIES)
    ap.add_argument("--underlying", default="NIFTY",
                    help="option strategies: index underlying(s), comma-separated to POOL "
                         "(e.g. NIFTY,BANKNIFTY,FINNIFTY)")
    ap.add_argument("--pair", help="pairs strategy: 'SYMA:SYMB' (e.g. HDFCBANK:ICICIBANK)")
    ap.add_argument("--from", dest="from_date")
    ap.add_argument("--to", dest="to_date")
    ap.add_argument("--start-capital", type=float, default=PAPER_START_CAPITAL)
    ap.add_argument("--refresh", action="store_true", help="re-fetch history (needs a session)")
    # vol_seller tuning levers (override settings defaults for sweeps)
    ap.add_argument("--iv-rank-min", type=float, dest="iv_rank_min")
    ap.add_argument("--iv-rank-max", type=float, dest="iv_rank_max")
    ap.add_argument("--delta", type=float, help="short-strike target |delta|")
    ap.add_argument("--wing", type=int, help="wing width in points (fixed)")
    ap.add_argument("--wing-delta", type=float, dest="wing_delta",
                    help="place wings at this |delta| instead of fixed points (auto-scales per underlying)")
    ap.add_argument("--dte", type=int, help="days to expiry")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    session = None
    if args.refresh:
        try:
            from core.market_data_provider import get_session
            session = get_session()
        except Exception as e:  # noqa: BLE001
            print(f"(no session: {e}); trying cache")

    if args.strategy == "pairs":
        if not args.pair or ":" not in args.pair:
            print("--pair SYMA:SYMB is required for the pairs strategy "
                  "(e.g. --pair HDFCBANK:ICICIBANK)")
            return 1
        sa, sb = [x.strip().upper() for x in args.pair.split(":", 1)]
        a_bars = data.load_daily(session, sa, args.from_date, args.to_date, refresh=args.refresh)
        b_bars = data.load_daily(session, sb, args.from_date, args.to_date, refresh=args.refresh)
        if not a_bars or not b_bars:
            print(f"No data for {sa}/{sb}. Run once with --refresh and a Dhan session.")
            return 1
        res = equity_engine.run_pairs(PairsStrategy(sa, sb), a_bars, b_bars,
                                      symbol_a=sa, symbol_b=sb, start_capital=args.start_capital)
        label, n_bars = f"{sa}/{sb}", min(len(a_bars), len(b_bars))
        span = (a_bars[0]["date"], a_bars[-1]["date"])
        regime = []
    else:
        from core.backtest.engine import BacktestResult
        vb = data.load_daily(session, "INDIAVIX", args.from_date, args.to_date, refresh=args.refresh)
        if not vb:
            print("No INDIAVIX data. Run once with --refresh and a Dhan session.")
            return 1
        unders = [u.strip().upper() for u in args.underlying.split(",") if u.strip()]
        pooled, spans = [], []
        for ul in unders:
            ub = data.load_daily(session, ul, args.from_date, args.to_date, refresh=args.refresh)
            if not ub:
                print(f"(no data for {ul}; skipping)")
                continue
            r = engine.run(_build_option_strategy(args.strategy, args), ub, vb,
                           underlying=ul, start_capital=args.start_capital)
            for rec in r.records:                       # namespace ids so pooled trades stay distinct
                rec["position_id"] = f"{ul}:{rec['position_id']}"
                rec["underlying"] = ul
                pooled.append(rec)
            spans.append((ub[0]["date"], ub[-1]["date"]))
        if not pooled and not spans:
            print(f"No data for {args.underlying}. Run once with --refresh and a Dhan session.")
            return 1
        res = BacktestResult(records=pooled, equity_curve=_pooled_curve(pooled, args.start_capital),
                             start_capital=args.start_capital, underlying="+".join(unders))
        label = "+".join(unders)
        n_bars = len({r["date"] for r in pooled})
        span = (min(s[0] for s in spans), max(s[1] for s in spans)) if spans else ("?", "?")
        regime = _regime_breakdown(pooled, vb)

    s = res.summary()
    print(f"OptionBuddy backtest — {args.strategy} on {label} "
          f"({span[0]} → {span[1]}, {n_bars} bars)")
    print("=" * 60)
    if s.get("trades", 0) == 0:
        print("No trades generated (check entry gates / data span).")
        return 1
    print(f"Trades {s['trades']} | Win {s['win_rate']}% | Net Rs {s['net_pnl']:,.0f} "
          f"({s['return_pct']}%) | PF {s['profit_factor']} | t {s['t_stat']} | "
          f"MaxDD Rs {s['max_drawdown']:,.0f}")
    print("\n-- go-live gate --")
    ready, lines = evaluate(res.records, args.start_capital)
    print("\n".join(lines))
    if regime:
        print("\n".join(regime))
    path = _save_curve(args.strategy, label.replace("/", "_"), res.equity_curve)
    print("=" * 60)
    print(f"Equity curve → {path}")
    print("RESULT: " + ("[PASS] worth papering" if ready
                        else "[FAIL] iterate the hypothesis, do NOT deploy"))
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
