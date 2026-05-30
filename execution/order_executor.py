"""
order_executor.py -- INDstocks order placement with risk guards

INDstocks F&O order uses security_id (from position), not tradingsymbol.
algo_id for NSE = "99999"
product = "MARGIN" for F&O intraday
"""
from datetime import datetime

from config.settings import (
    MAX_LOSS_PER_TRADE,
    DAILY_LOSS_LIMIT,
    MAX_LOTS_PER_ORDER,
    AUTO_EXECUTE,
    MIN_CONFIDENCE,
    MARKET_OPEN,
    MARKET_CLOSE,
)
from config.logger import get_logger
from data.store import save_trade

log = get_logger("order_executor")

LOT_SIZES = {
    "NIFTY":      75,
    "BANKNIFTY":  30,
    "FINNIFTY":   40,
    "MIDCPNIFTY": 75,
}

def _get_lot_size(symbol: str) -> int:
    for index, size in LOT_SIZES.items():
        if symbol.upper().startswith(index):
            return size
    return 1

def _is_market_open() -> bool:
    now = datetime.now().strftime("%H:%M")
    return MARKET_OPEN <= now <= MARKET_CLOSE


class OrderExecutor:
    def __init__(self, session):
        self.session = session
        self._daily_loss: float = 0.0

    def add_realised_loss(self, loss: float) -> None:
        if loss > 0:
            self._daily_loss += loss

    def _check_risk(self, signal: dict) -> tuple:
        if not _is_market_open():
            return False, "Market is closed."
        if self._daily_loss >= DAILY_LOSS_LIMIT:
            return False, "Daily loss limit hit: INR %.0f" % self._daily_loss
        max_loss = signal.get("max_loss_if_held", 0)
        if isinstance(max_loss, (int, float)) and max_loss > MAX_LOSS_PER_TRADE:
            return False, "Per-trade loss INR %s exceeds limit" % max_loss
        return True, ""

    def execute(self, signal: dict, signal_id: int, approved: bool = False) -> dict:
        sig_type   = signal.get("signal", "HOLD")
        instrument = signal.get("instrument", "")
        security_id = signal.get("security_id", "")
        confidence = signal.get("confidence", 0)

        if sig_type == "HOLD":
            return {"status": "skipped", "reason": "HOLD signal"}

        if not security_id:
            log.warning("No security_id in signal -- cannot place order for %s", instrument)
            return {"status": "rejected", "reason": "missing security_id"}

        if not approved and not (AUTO_EXECUTE and confidence >= MIN_CONFIDENCE):
            return {"status": "pending_approval", "signal_id": signal_id}

        ok, reason = self._check_risk(signal)
        if not ok:
            log.warning("Risk check failed: %s", reason)
            return {"status": "rejected", "reason": reason}

        if sig_type.startswith("EXIT") or sig_type == "REDUCE_LOTS":
            txn_type = "SELL"
        elif sig_type.startswith("BUY"):
            txn_type = "BUY"
        else:
            return {"status": "skipped", "reason": "Unknown signal: " + sig_type}

        qty = _get_lot_size(instrument) * min(MAX_LOTS_PER_ORDER, 2)

        order_payload = {
            "txn_type":   txn_type,
            "exchange":   "NSE",
            "segment":    "DERIVATIVE",
            "product":    "MARGIN",
            "order_type": "MARKET",
            "validity":   "DAY",
            "security_id": security_id,
            "qty":        qty,
            "algo_id":    "99999",
            "is_amo":     False,
        }

        try:
            resp = self.session.post("/order", json=order_payload)
            order_id = resp.get("data", {}).get("order_id", "unknown")
            log.info("Order placed: %s %s x%s -> order_id=%s", txn_type, instrument, qty, order_id)
            save_trade({
                "instrument": instrument,
                "transaction": txn_type,
                "quantity": qty,
                "order_id": str(order_id),
                "signal_id": signal_id,
            })
            return {"status": "executed", "order_id": order_id, "qty": qty}
        except Exception as e:
            log.error("Order placement failed: %s", e)
            return {"status": "error", "reason": str(e)}
