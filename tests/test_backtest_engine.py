"""End-to-end test of the backtest engine + data helpers with a toy seller."""
from datetime import date, timedelta

import pytest

from core.backtest import engine, data
from core.strategies.base import Strategy, Order, OptionLeg


def _series(spot, vix, n=15, start="2025-06-02"):
    """n sequential daily bars at constant spot/vix → (underlying_bars, vix_bars)."""
    d0 = date.fromisoformat(start)
    ub, vb = [], []
    for i in range(n):
        d = (d0 + timedelta(days=i)).isoformat()
        ub.append({"date": d, "open": spot, "high": spot, "low": spot, "close": spot, "volume": 0})
        vb.append({"date": d, "open": vix, "high": vix, "low": vix, "close": vix, "volume": 0})
    return ub, vb


class ToyCondorSeller(Strategy):
    """Sell one defined-risk iron condor on the first bar; hold to expiry."""
    name = "toy_condor"

    def __init__(self, dte=7):
        self.opened = False
        self.dte = dte

    def generate(self, ctx):
        if self.opened:
            return []
        self.opened = True
        s, step = ctx.spot, ctx.step
        exp = (date.fromisoformat(ctx.date) + timedelta(days=self.dte)).isoformat()
        legs = [
            OptionLeg("CE", s + step, "SELL", 1, expiry_date=exp),
            OptionLeg("CE", s + 3 * step, "BUY", 1, expiry_date=exp),
            OptionLeg("PE", s - step, "SELL", 1, expiry_date=exp),
            OptionLeg("PE", s - 3 * step, "BUY", 1, expiry_date=exp),
        ]
        return [Order(kind="option", underlying="NIFTY", tag="toy", legs=legs)]


# ── data helpers ─────────────────────────────────────────────────────────────

def test_align_inner_joins_on_date():
    a = [{"date": "2025-06-02", "close": 1}, {"date": "2025-06-03", "close": 2}]
    b = [{"date": "2025-06-03", "close": 9}, {"date": "2025-06-04", "close": 8}]
    out = data.align(a, b)
    assert [d for d, _, _ in out] == ["2025-06-03"]


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "CACHE_DIR", str(tmp_path))
    rows = [{"date": "2025-06-02", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}]
    path = data._cache_path("NIFTY")
    data._write_cache(path, rows)
    assert data._read_cache(path) == rows


# ── engine end-to-end ────────────────────────────────────────────────────────

def test_condor_on_flat_market_keeps_credit():
    ub, vb = _series(20000, 14, n=15)
    res = engine.run(ToyCondorSeller(dte=7), ub, vb, underlying="NIFTY",
                     start_capital=300000)
    assert len(res.records) == 1
    rec = res.records[0]
    assert rec["reason"] == "expiry"
    assert rec["pnl"] > 0           # flat market → shorts expire worthless → keep credit
    assert res.equity_curve[-1][1] > res.start_capital
    assert res.summary()["trades"] == 1


def test_capital_limit_blocks_when_broke():
    ub, vb = _series(20000, 14, n=15)
    res = engine.run(ToyCondorSeller(dte=7), ub, vb, underlying="NIFTY",
                     start_capital=1000)   # margin >> 1000 → nothing opens
    assert res.records == []


def test_margin_defined_risk_vs_naked():
    # a covered short (condor) is defined-risk; a bare short is naked
    s = 20000
    condor = [OptionLeg("CE", s + 50, "SELL", 1, 100.0), OptionLeg("CE", s + 150, "BUY", 1, 30.0)]
    naked = [OptionLeg("CE", s + 50, "SELL", 1, 100.0)]
    assert engine._is_defined_risk(condor) is True
    assert engine._is_defined_risk(naked) is False
