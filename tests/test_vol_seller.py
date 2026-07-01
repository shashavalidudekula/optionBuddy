"""Tests for the VRP vol-seller strategy via the backtest engine."""
from datetime import date, timedelta

import pytest

from core.backtest import engine
from core.strategies.vol_seller import VolSeller


def _series(spot, vix, n=20, start="2025-06-02"):
    d0 = date.fromisoformat(start)
    ub, vb = [], []
    for i in range(n):
        d = (d0 + timedelta(days=i)).isoformat()
        ub.append({"date": d, "open": spot, "high": spot, "low": spot, "close": spot, "volume": 0})
        vb.append({"date": d, "open": vix, "high": vix, "low": vix, "close": vix, "volume": 0})
    return ub, vb


def test_seller_trades_and_profits_on_calm_market():
    # Flat spot + steady vol → shorts decay → seller harvests the premium.
    ub, vb = _series(20000, 14, n=20)
    res = engine.run(VolSeller(iv_rank_min=0.0), ub, vb, underlying="NIFTY",
                     start_capital=300000)
    s = res.summary()
    assert s["trades"] >= 2          # ~weekly condors over 3 weeks
    assert s["net_pnl"] > 0          # calm market is the seller's friend
    assert res.equity_curve[-1][1] > res.start_capital


def test_iv_rank_gate_blocks_when_premium_cheap():
    # With an impossible IV-rank floor the seller must stand aside entirely.
    ub, vb = _series(20000, 14, n=20)
    res = engine.run(VolSeller(iv_rank_min=1.01), ub, vb, underlying="NIFTY",
                     start_capital=300000)
    assert res.records == []


def test_seller_defined_risk_structure():
    # One generated order is a 4-leg iron condor (2 sells covered by 2 further-OTM buys).
    from core.backtest.engine import BacktestContext, strike_step
    ctx = BacktestContext("NIFTY", strike_step("NIFTY"), date="2025-06-02", spot=20000,
                          iv=0.14, iv_rank=0.8)
    orders = VolSeller(iv_rank_min=0.0).generate(ctx)
    assert len(orders) == 1
    legs = orders[0].legs
    assert len(legs) == 4
    assert engine._is_defined_risk(legs) is True
