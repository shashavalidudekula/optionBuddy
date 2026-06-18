"""
pricing.py — Black-Scholes option pricing, greeks, and implied-vol solver.

Pure stdlib math (no numpy/scipy), so it runs the same in the backtester and live
and unit-tests without dependencies. Used to PRICE options synthetically in the
backtest (premium from underlying + an IV estimate) and to derive greeks/strikes.

Conventions:
  S      spot (or forward) of the underlying
  K      strike
  T      time to expiry IN YEARS (calendar days / 365)
  r      risk-free rate (annualised, e.g. 0.065)
  sigma  volatility (annualised, e.g. 0.14 for 14%)
  q      carry / dividend yield (default 0; index options ≈ 0)
  opt    "CE"/"call" or "PE"/"put" (case-insensitive)

Greeks are returned in practical units: delta per ₹1 of spot, vega per 1 vol POINT
(i.e. per 0.01 of sigma), theta per CALENDAR DAY (per 1/365 year).
"""
import math

_SQRT2 = math.sqrt(2.0)
_SQRT2PI = math.sqrt(2.0 * math.pi)


def _is_call(opt: str) -> bool:
    o = str(opt).strip().upper()
    if o in ("CE", "C", "CALL"):
        return True
    if o in ("PE", "P", "PUT"):
        return False
    raise ValueError(f"unknown option type: {opt!r} (expected CE/PE/call/put)")


def _phi(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / _SQRT2PI


def _Phi(x: float) -> float:
    """Standard normal CDF via erf (no scipy)."""
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float, q: float):
    vol = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol
    return d1, d1 - vol


def bs_price(S: float, K: float, T: float, r: float, sigma: float, opt: str,
             q: float = 0.0) -> float:
    """Black-Scholes price of a European option. Falls back to discounted
    intrinsic when T<=0 or sigma<=0 (so the simulator never divides by zero)."""
    call = _is_call(opt)
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        intrinsic = (S - K) if call else (K - S)
        return round(max(intrinsic, 0.0), 4)
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    disc_r, disc_q = math.exp(-r * T), math.exp(-q * T)
    if call:
        val = S * disc_q * _Phi(d1) - K * disc_r * _Phi(d2)
    else:
        val = K * disc_r * _Phi(-d2) - S * disc_q * _Phi(-d1)
    return round(max(val, 0.0), 4)


def bs_greeks(S: float, K: float, T: float, r: float, sigma: float, opt: str,
              q: float = 0.0) -> dict:
    """delta (per ₹1 spot), gamma, vega (per 1 vol point), theta (per calendar day)."""
    call = _is_call(opt)
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        intrinsic_delta = (1.0 if S > K else 0.0) if call else (-1.0 if S < K else 0.0)
        return {"delta": intrinsic_delta, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    disc_r, disc_q = math.exp(-r * T), math.exp(-q * T)
    pdf = _phi(d1)
    delta = disc_q * (_Phi(d1) if call else _Phi(d1) - 1.0)
    gamma = disc_q * pdf / (S * sigma * math.sqrt(T))
    vega = S * disc_q * pdf * math.sqrt(T) / 100.0  # per 1 vol POINT (0.01 of sigma)
    term1 = -(S * disc_q * pdf * sigma) / (2.0 * math.sqrt(T))
    if call:
        theta = term1 - r * K * disc_r * _Phi(d2) + q * S * disc_q * _Phi(d1)
    else:
        theta = term1 + r * K * disc_r * _Phi(-d2) - q * S * disc_q * _Phi(-d1)
    return {"delta": round(delta, 4), "gamma": round(gamma, 6),
            "vega": round(vega, 4), "theta": round(theta / 365.0, 4)}


def implied_vol(price: float, S: float, K: float, T: float, r: float, opt: str,
                q: float = 0.0, lo: float = 1e-4, hi: float = 5.0,
                tol: float = 1e-6, max_iter: int = 100) -> float | None:
    """Invert Black-Scholes for sigma via bisection. None if the price is outside
    the no-arbitrage band (can't be matched by any vol)."""
    call = _is_call(opt)
    if price is None or price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max((S - K) if call else (K - S), 0.0) * math.exp(-r * T)
    upper = (S * math.exp(-q * T)) if call else (K * math.exp(-r * T))
    if price < intrinsic - tol or price > upper + tol:
        return None
    f_lo = bs_price(S, K, T, r, lo, opt, q) - price
    f_hi = bs_price(S, K, T, r, hi, opt, q) - price
    if f_lo * f_hi > 0:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = bs_price(S, K, T, r, mid, opt, q) - price
        if abs(f_mid) < tol:
            return round(mid, 6)
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return round(0.5 * (lo + hi), 6)
