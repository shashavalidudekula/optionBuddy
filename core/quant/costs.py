"""
costs.py — dependency-free cost/slippage helpers.

Lives in core/quant (not paper_trader) so the backtester and gate can reuse the
SAME slippage model without dragging in the live/DB stack (psycopg2, broker,
tracker). The economics match paper_trader._slip_fill by construction.
"""


def slip_fill(price: float, side: str, slip: float = 0.0) -> float:
    """Adverse slippage on a MARKET fill, as a fraction of price.
    `side='buy'` pays UP (long entry / buy-to-close a short); `side='sell'`
    receives LESS (sell-to-open / sell-to-close a long). Limit/settlement fills
    don't pass through here."""
    slip = max(slip or 0.0, 0.0)
    if slip <= 0:
        return round(price, 2)
    return round(price * (1 + slip), 2) if str(side).lower() == "buy" else round(price * (1 - slip), 2)
