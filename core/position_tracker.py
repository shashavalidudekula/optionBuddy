"""
position_tracker.py -- Live F&O positions via INDstocks /portfolio/positions
"""
from dataclasses import dataclass, field
from typing import Optional

from config.logger import get_logger

log = get_logger("position_tracker")

SWING_THRESHOLD = 2000  # INR swing that triggers urgent re-analysis


@dataclass
class Position:
    tradingsymbol: str
    security_id: str
    exchange_segment: str
    quantity: int
    average_price: float
    last_price: float
    pnl: float

    @property
    def direction(self) -> str:
        return "LONG" if self.quantity > 0 else "SHORT"

    @property
    def option_type(self) -> Optional[str]:
        sym = self.tradingsymbol.upper()
        if sym.endswith("CE"):
            return "CE"
        if sym.endswith("PE"):
            return "PE"
        return None

    def to_dict(self) -> dict:
        return {
            "symbol": self.tradingsymbol,
            "security_id": self.security_id,
            "qty": self.quantity,
            "avg_buy": self.average_price,
            "ltp": self.last_price,
            "pnl": round(self.pnl, 2),
            "direction": self.direction,
            "option_type": self.option_type,
        }


class PositionTracker:
    def __init__(self, session):
        self.session = session
        self._last_pnl: float = 0.0
        self._snapshot: list = []

    def fetch(self) -> list:
        try:
            resp = self.session.get(
                "/portfolio/positions",
                params={"segment": "derivative", "product": "margin"},
            )
            if isinstance(resp, list):
                raw = resp
            else:
                data = resp.get("data", [])
                if isinstance(data, dict):
                    raw = data.get("net_positions", [])
                elif isinstance(data, list):
                    raw = data
                else:
                    raw = []
        except Exception as e:
            log.error("Failed to fetch positions: %s", e)
            return self._snapshot

        positions = []
        for p in raw:
            # INDstocks uses net_qty field, not net_quantity
            qty = int(p.get("net_qty", 0))
            if qty == 0:
                continue

            # Build symbol with option details (e.g., NIFTY23800PE)
            symbol = p.get("symbol", "")
            if p.get("drv_option_type"):
                strike = p.get("drv_strike_price", "")
                opt_type = p.get("drv_option_type", "")
                symbol = f"{symbol}{strike}{opt_type}"

            positions.append(Position(
                tradingsymbol=symbol,
                security_id=str(p.get("security_id", "")),
                exchange_segment=p.get("segment", "DERIVATIVE"),
                quantity=qty,
                average_price=float(p.get("avg_price", 0)),
                last_price=float(p.get("last_traded_price", 0)),
                pnl=float(p.get("realized_profit", 0)),
            ))

        self._snapshot = positions
        return positions

    def total_unrealised_pnl(self) -> float:
        return sum(p.pnl for p in self._snapshot)

    def has_significant_swing(self) -> bool:
        current = self.total_unrealised_pnl()
        if abs(current - self._last_pnl) >= SWING_THRESHOLD:
            self._last_pnl = current
            return True
        self._last_pnl = current
        return False

    def summary(self) -> dict:
        return {
            "open_count": len(self._snapshot),
            "total_unrealised_pnl": round(self.total_unrealised_pnl(), 2),
            "positions": [p.to_dict() for p in self._snapshot],
        }
