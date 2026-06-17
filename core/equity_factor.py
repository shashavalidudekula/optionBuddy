"""
equity_factor.py — Deterministic low-frequency equity strategy (NO LLM).

Monthly momentum rank + low-vol/quality filter on large-cap indices (NIFTY50 / NIFTYNXT50).
Long cash positions held weekly/monthly; rebalances on a fixed schedule. Simplest
evidence-based retail strategy in India: low turnover, low costs, documented positive-
expectancy in academic literature on emerging markets.

No prediction attempts; purely mean-reversion to ranked exposures (momentum >0, vol <percentile).
"""
from datetime import datetime, timedelta, time as dtime

from config.logger import get_logger
from config.settings import (
    EQUITY_FACTOR_ENABLED, EQUITY_FACTOR_UNIVERSE, EQUITY_FACTOR_REBALANCE_DOW,
    EQUITY_FACTOR_MIN_MOMENTUM_PCT, EQUITY_FACTOR_MAX_VOL_PERCENTILE, MARKET_OPEN,
)
from core.market_data_provider import get_historical_daily, get_index_spots
from data.advisory_store import save_call, update_call_status

log = get_logger("equity_factor")


class EquityFactor:
    """Generates monthly equity long calls via deterministic factor screening."""

    def __init__(self, session, paper):
        self.session = session
        self.paper = paper
        hh, mm = MARKET_OPEN.split(":")
        self._rebal_dw = EQUITY_FACTOR_REBALANCE_DOW  # day of week (0=Mon, 4=Fri)
        self._rebal_t = dtime(int(hh), int(mm))
        self._last_rebal = None

    def step(self, now: datetime, price_lookup) -> list[str]:
        if not EQUITY_FACTOR_ENABLED or self.session is None or self.paper is None:
            return []
        if now.weekday() >= 5:
            return []
        today = now.date()
        if now.weekday() != self._rebal_dw or now.time() < self._rebal_t:
            return []
        if self._last_rebal == today:
            return []  # Already rebalanced today
        self._last_rebal = today
        return self._rebalance(now, price_lookup)

    def _rebalance(self, now: datetime, price_lookup) -> list[str]:
        """Screen universe, rank by momentum, filter by vol. Long 3–5 best names."""
        notes = []
        candidates = []

        for symbol in EQUITY_FACTOR_UNIVERSE:
            try:
                bars = get_historical_daily(self.session, symbol, days=252)  # 1 year of history
                if not bars or len(bars) < 60:
                    continue
                recent_bars = bars[-20:]  # Last 20 days for momentum
                long_bars = bars  # All year for vol

                # 1-month momentum: (latest_close − 20-day-ago close) / 20-day-ago close
                if len(recent_bars) < 2:
                    continue
                p_now = float(recent_bars[-1]["close"])
                p_20d = float(recent_bars[0]["close"])
                momentum_pct = (p_now - p_20d) / p_20d * 100 if p_20d > 0 else 0.0

                # Volatility: annualized stddev of daily returns (last 60 days).
                if len(bars) < 60:
                    continue
                closes_60d = [float(b["close"]) for b in bars[-60:]]
                rets_60d = [(closes_60d[i] - closes_60d[i-1]) / closes_60d[i-1]
                            for i in range(1, len(closes_60d))]
                import statistics
                vol_60d = statistics.stdev(rets_60d) if len(rets_60d) > 1 else 0.0
                vol_annual = vol_60d * (252 ** 0.5)

                if momentum_pct >= EQUITY_FACTOR_MIN_MOMENTUM_PCT:
                    candidates.append({
                        "symbol": symbol, "momentum": momentum_pct, "vol": vol_annual,
                        "price": p_now, "vol_rank": None,  # Rank it later
                    })
            except Exception as e:  # noqa: BLE001
                log.debug("Equity factor screening for %s failed: %s", symbol, e)

        if not candidates:
            log.info("Equity factor: no candidates passed momentum filter (>%.2f%%)",
                    EQUITY_FACTOR_MIN_MOMENTUM_PCT)
            return notes

        # Rank by vol; pick those below the vol percentile.
        vols = sorted([c["vol"] for c in candidates])
        vol_threshold_idx = max(0, int(len(vols) * (EQUITY_FACTOR_MAX_VOL_PERCENTILE / 100)))
        vol_threshold = vols[vol_threshold_idx] if vol_threshold_idx < len(vols) else vols[-1]

        selected = [c for c in candidates if c["vol"] <= vol_threshold]
        if not selected:
            log.info("Equity factor: no candidates passed vol filter (<= %.2f %%ile)",
                    EQUITY_FACTOR_MAX_VOL_PERCENTILE)
            return notes

        # Sort by momentum (descending) and open top N.
        selected_sorted = sorted(selected, key=lambda c: -c["momentum"])[:5]
        for cand in selected_sorted:
            note = self._open_position(cand, now)
            if note:
                notes.append(note)

        return notes

    def _open_position(self, cand: dict, now: datetime) -> str | None:
        """Generate a long equity call. Entry: today's close (approximated). Hold 1 month."""
        symbol = cand["symbol"]
        price = cand["price"]
        momentum = cand["momentum"]

        # Target: 5% gain (conservative). Stop: 3% loss. Expiry: 30 days (monthly hold).
        call = {
            "category": "equity_cash",
            "instrument": symbol,
            "underlying": symbol,
            "action": "BUY",
            "timeframe": "swing",
            "entry_price": round(price, 2),
            "entry_min": round(price * 0.99, 2),
            "entry_max": round(price * 1.01, 2),
            "target_1": round(price * 1.03, 2),  # 3% into momentum
            "target_2": round(price * 1.05, 2),  # 5% target
            "stop_loss": round(price * 0.97, 2),  # 3% stop
            "confidence": 70 + int(min(30, momentum / 2)),  # Boost by momentum
            "rationale": (f"Equity factor: {symbol} ranked by momentum ({momentum:.2f}%) "
                         f"+ low vol. Hold 1 month, rebalance next cycle."),
            "expires_at": now + timedelta(days=30),
            "strategy": "equity_cash",
        }
        try:
            call_id = save_call(call)
            call["id"] = call_id
            update_call_status(call_id, "entry_triggered", last_price=price, entry_triggered=True)
            note = self.paper._maybe_open(call, price)
        except Exception as e:  # noqa: BLE001
            log.error("Equity factor entry failed (%s): %s", symbol, e)
            return None
        from data.advisory_store import get_open_paper_position_by_call
        if not get_open_paper_position_by_call(call_id):
            update_call_status(call_id, "closed", last_price=price)
            log.info("Equity factor: paper did not open %s (guardrail) — call closed", symbol)
            return note

        log.info("Equity factor ENTER %s @ %.2f (momentum %.2f%%)", symbol, price, momentum)
        return note or f"📈 <b>Equity long</b> {symbol} @ ₹{price:,.2f} (momentum {momentum:+.2f}%)"
