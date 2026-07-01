"""
vol.py — volatility analytics: realised vol, IV rank/percentile, and the VRP.

The edge we're chasing lives here: in Indian index options the IMPLIED vol
(India-VIX) is, on average, richer than the vol the index subsequently REALISES —
that gap is the volatility risk premium (VRP) that option sellers harvest. These
helpers quantify it and tell a strategy WHEN premium is rich enough to sell.

Pure stdlib. Inputs are plain lists of floats; the caller decides units (e.g. pass
VIX/100 to compare against a realised vol expressed as a fraction).
"""
import math

TRADING_DAYS = 252


def log_returns(closes: list[float]) -> list[float]:
    out = []
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a and b and a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def realized_vol(closes: list[float], periods_per_year: int = TRADING_DAYS) -> float | None:
    """Annualised close-to-close realised vol (sample stdev of log returns)."""
    rets = log_returns(closes)
    if len(rets) < 2:
        return None
    mu = sum(rets) / len(rets)
    var = sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


def parkinson_vol(highs: list[float], lows: list[float],
                  periods_per_year: int = TRADING_DAYS) -> float | None:
    """Annualised Parkinson (high-low range) vol — more efficient than close-to-close
    when intraday range data is available."""
    n = min(len(highs), len(lows))
    pairs = [(highs[i], lows[i]) for i in range(n) if highs[i] > 0 and lows[i] > 0 and highs[i] >= lows[i]]
    if not pairs:
        return None
    s = sum(math.log(h / l) ** 2 for h, l in pairs)
    var = s / (4.0 * math.log(2.0) * len(pairs))
    return math.sqrt(var) * math.sqrt(periods_per_year)


def iv_rank(current: float, history: list[float]) -> float | None:
    """Where `current` sits between the lookback MIN and MAX, in [0,1].
    1.0 = at the period high (richest premium), 0.0 = at the period low."""
    vals = [x for x in history if x is not None]
    if not vals:
        return None
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (current - lo) / (hi - lo)))


def iv_percentile(current: float, history: list[float]) -> float | None:
    """Fraction of the lookback that traded BELOW `current`, in [0,1]."""
    vals = [x for x in history if x is not None]
    if not vals:
        return None
    below = sum(1 for x in vals if x < current)
    return below / len(vals)


def vrp(implied: float, realized: float) -> float | None:
    """Volatility risk premium = implied − realised (positive ⇒ selling is paid)."""
    if implied is None or realized is None:
        return None
    return implied - realized
