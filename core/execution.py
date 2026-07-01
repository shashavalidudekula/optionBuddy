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

    def fetch_positions(self) -> dict | None:
        """Net open quantity per scrip code: {"SEG:securityId": net_qty} (signed:
        + long, − short). Used to reconcile internal state against the real account.

        Return convention (so reconciliation can't false-alarm on a hiccup):
          None → cannot report (paper / unsupported / fetch error) → skip this cycle
          {}   → fetched OK, the account is genuinely flat
        Paper has no broker book, so the base returns None.
        """
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

    # Read-only — safe to call regardless of DHAN_ALLOW_LIVE_ORDERS (no money moves).
    def fetch_positions(self):
        """Net open qty per scrip from Dhan's day positions: {"SEG:id": net_qty}.

        Envelope VERIFIED live 2026-06-16: DhanHQ v2 GET /positions returns a BARE
        JSON LIST (not wrapped in {"data": [...]}) — both shapes are handled below.
        Rows follow Dhan's documented schema: exchangeSegment, securityId, netQty
        (signed), positionType (LONG/SHORT/CLOSED). Row fields are NOT yet confirmed
        against a real position (the account was flat at verification) — re-confirm on
        the first live position. Returns None on failure so reconciliation skips.
        """
        try:
            resp = self.session.get("/positions")
        except Exception as e:  # noqa: BLE001
            log.error("Dhan fetch_positions failed: %s", e)
            return None
        rows = resp.get("data") if isinstance(resp, dict) else resp
        out: dict[str, int] = {}
        for r in (rows or []):
            seg = r.get("exchangeSegment") or r.get("exchange_segment")
            sid = r.get("securityId") or r.get("security_id")
            if seg is None or sid is None:
                continue
            net = int(float(r.get("netQty", r.get("net_qty", 0)) or 0))
            ptype = str(r.get("positionType") or r.get("position_type") or "").upper()
            if ptype == "CLOSED" or net == 0:
                continue
            if ptype == "SHORT" and net > 0:
                net = -net
            code = f"{seg}:{sid}"
            out[code] = out.get(code, 0) + net
        return out


def get_broker(session=None) -> Broker:
    """Return the broker for the active EXECUTION_MODE."""
    if EXECUTION_MODE == "live":
        log.info("Execution mode: LIVE (orders %s)",
                 "ENABLED" if DHAN_ALLOW_LIVE_ORDERS else "guarded/dry-run")
        return DhanBroker(session)
    log.info("Execution mode: PAPER (no real orders)")
    return PaperBroker()
