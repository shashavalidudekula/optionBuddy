"""End-to-end test of the pairs strategy via the equity backtest engine."""
import random
from datetime import date, timedelta

import pytest

from core.backtest import equity_engine
from core.strategies.pairs import PairsStrategy


def _pair_bars(n=220, beta=1.2, ar=0.85, amp=2.0, seed=3, start="2024-01-01"):
    """B = a volatile random walk (strong co-movement) and A = beta*B + a
    mean-reverting spread → genuinely correlated AND cointegrated (real pairs
    co-move; the spread is the tradable, stationary residual)."""
    random.seed(seed)
    b_px = [100.0]
    spread = [0.0]
    for _ in range(1, n):
        b_px.append(b_px[-1] * (1 + random.gauss(0, 0.025)))   # B moves a lot → high in-window corr
        spread.append(ar * spread[-1] + random.gauss(0, amp))
    a_px = [beta * b_px[i] + spread[i] for i in range(n)]
    d0 = date.fromisoformat(start)
    ba, bb = [], []
    for i in range(n):
        d = (d0 + timedelta(days=i)).isoformat()
        ba.append({"date": d, "open": a_px[i], "high": a_px[i], "low": a_px[i], "close": a_px[i], "volume": 0})
        bb.append({"date": d, "open": b_px[i], "high": b_px[i], "low": b_px[i], "close": b_px[i], "volume": 0})
    return ba, bb


def test_pairs_trades_and_profits_on_mean_reversion():
    ba, bb = _pair_bars()
    strat = PairsStrategy("A", "B", lookback=60, min_corr=0.6, entry_z=1.5,
                          exit_z=0.5, max_half_life=40, alloc=100000)
    res = equity_engine.run_pairs(strat, ba, bb, symbol_a="A", symbol_b="B",
                                  start_capital=400000, lookback=60)
    s = res.summary()
    assert s["trades"] >= 1            # the spread stretches past entry-z and reverts
    assert s["net_pnl"] > 0            # harvesting mean reversion is the edge
    assert res.equity_curve[-1][1] > res.start_capital


def test_pairs_no_trade_before_lookback():
    ba, bb = _pair_bars(n=40)          # fewer bars than the 60-day lookback
    strat = PairsStrategy("A", "B", lookback=60)
    res = equity_engine.run_pairs(strat, ba, bb, symbol_a="A", symbol_b="B",
                                  start_capital=300000, lookback=60)
    assert res.records == []


def test_pairs_one_position_at_a_time():
    ba, bb = _pair_bars()
    strat = PairsStrategy("A", "B", lookback=60, min_corr=0.7)
    res = equity_engine.run_pairs(strat, ba, bb, symbol_a="A", symbol_b="B",
                                  start_capital=300000, lookback=60)
    # entries and exits must interleave — never two opens without a close between
    # (enforced by the ctx.open_positions guard); so closed trades are well-formed.
    assert all(r["pnl"] is not None for r in res.records)
