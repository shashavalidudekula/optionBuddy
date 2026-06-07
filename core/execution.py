"""
execution.py — order-routing abstraction behind a paper/live switch.

The PaperTrader fully simulates fills with its own cash model; a Broker is the
hook where REAL orders would be placed. In paper mode the broker is a no-op, so
paper behaviour is byte-identical to before. Going live is a single switch:

    EXECUTION_MODE=paper  → PaperBroker   (no real orders — default)
    EXECUTION_MODE=live   → DhanBroker    (real Dhan orders, double-guarded)

DOUBLE GUARD: even in live mode, DhanBroker only sends an order when
DHAN_ALLOW_LIVE_ORDERS=true. With the mode flipped but the guard off, it logs
the intended order and returns None — so you can dry-run the live wiring with
zero risk of real money moving.

The Dhan /orders payload below is scaffolding: confirm the field names and
product/validity values against your Dhan account + dhanhq.co/docs/v2/orders
before enabling DHAN_ALLOW_LIVE_ORDERS.
"""

from config.logger import get_logger
from config.settings import (
    EXECUTION_MODE, DHAN_ALLOW_LIVE_ORDERS, DHAN_CLIENT_ID,
)

log = get_logger("execution")


class Broker:
    """No-op base broker (paper). Returns None to signal 'simulated only'."""

    def place_entry(self, call: dict, action: str, qty: int, price: float | None) -> dict | None:
        return None

    def place_exit(self, call: dict, action: str, qty: int, price: float | None,
                   reason: str = "exit") -> dict | None:
        return None


class PaperBroker(Broker):
    """Explicit paper broker — simulation is handled by PaperTrader's cash model."""


class DhanBroker(Broker):
    """Live Dhan order broker. Inert unless DHAN_ALLOW_LIVE_ORDERS is also set."""

    def __init__(self, session):
        self.session = session
        if not DHAN_ALLOW_LIVE_ORDERS:
            log.warning("DhanBroker active in LIVE mode but DHAN_ALLOW_LIVE_ORDERS is "
                        "false — orders will be logged, NOT sent (safe dry-run).")

    # entry BUY → BUY order; entry SELL → SELL (short) order.
    def place_entry(self, call, action, qty, price):
        return self._order(call, action.upper(), qty, price, kind="entry")

    # exit reverses the side: close a long BUY by SELLing, close a short by BUYing.
    def place_exit(self, call, action, qty, price, reason="exit"):
        side = "SELL" if action.upper() == "BUY" else "BUY"
        return self._order(call, side, qty, price, kind=f"exit:{reason}")

    def _order(self, call, side, qty, price, kind):
        code = self._resolve(call)
        if not code or qty <= 0:
            log.warning("DhanBroker skip (%s): unresolved/zero — %s", kind, call.get("instrument"))
            return None
        seg, sid = code.split(":", 1)
        payload = {
            "dhanClientId": DHAN_CLIENT_ID,
            "transactionType": side,
            "exchangeSegment": seg,
            "productType": "INTRADAY",
            "orderType": "LIMIT" if price else "MARKET",
            "validity": "DAY",
            "securityId": sid,
            "quantity": int(qty),
            "price": round(float(price), 2) if price else 0,
        }
        if not DHAN_ALLOW_LIVE_ORDERS:
            log.info("[DRY-RUN] would place Dhan %s order: %s", kind, payload)
            return None
        try:
            resp = self.session.post("/orders", json=payload)
            log.info("LIVE Dhan %s order placed: %s → %s", kind, payload, resp)
            return resp
        except Exception as e:  # noqa: BLE001
            log.error("LIVE Dhan %s order FAILED (%s): %s", kind, call.get("instrument"), e)
            return None

    @staticmethod
    def _resolve(call):
        from core.market_data_provider import resolve_scrip_for_call
        return resolve_scrip_for_call(call)


def get_broker(session=None) -> Broker:
    """Return the broker for the active EXECUTION_MODE."""
    if EXECUTION_MODE == "live":
        log.info("Execution mode: LIVE (orders %s)",
                 "ENABLED" if DHAN_ALLOW_LIVE_ORDERS else "guarded/dry-run")
        return DhanBroker(session)
    log.info("Execution mode: PAPER (no real orders)")
    return PaperBroker()
