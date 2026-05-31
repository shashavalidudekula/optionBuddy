"""
grow_adapter.py — Grow.in broker integration.

Implements BrokerAdapter for Grow.in trading platform using REST API.
"""

import requests
from config.logger import get_logger
from .broker_adapter import BrokerAdapter, BrokerPosition

log = get_logger("grow_adapter")


class GrowAdapter(BrokerAdapter):
    """Grow.in broker adapter."""

    def __init__(self, user_id: str, api_key: str, api_secret: str):
        """
        Initialize Grow adapter.

        Args:
            user_id: User identifier
            api_key: Grow API key
            api_secret: Grow API secret
        """
        self.user_id = user_id
        self.base_url = "https://api.groww.in/v1"
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": api_key,
            "Authorization": f"Bearer {api_secret}",
            "Content-Type": "application/json",
        })
        log.info(f"Initialized GrowAdapter for user {user_id}")

    async def authenticate(self) -> bool:
        """Verify Grow connection."""
        try:
            resp = self.session.get(f"{self.base_url}/account/profile")
            resp.raise_for_status()
            data = resp.json()
            is_valid = data.get("status") == "success"
            log.info(f"Grow authentication: {'success' if is_valid else 'failed'}")
            return is_valid
        except Exception as e:
            log.warning(f"Grow verification failed: {e}")
            return False

    async def fetch_positions(self) -> list[BrokerPosition]:
        """Fetch open positions from Grow."""
        try:
            resp = self.session.get(f"{self.base_url}/portfolio/positions")
            resp.raise_for_status()
            data = resp.json()

            positions = []
            for item in data.get("positions", []):
                # Skip closed positions
                if item.get("quantity", 0) == 0:
                    continue

                pos = BrokerPosition(
                    tradingsymbol=item.get("symbol", ""),
                    security_id=item.get("security_id", ""),
                    quantity=float(item.get("quantity", 0)),
                    average_price=float(item.get("average_price", 0)),
                    last_price=float(item.get("current_price", 0)),
                    pnl=float(item.get("unrealised_pnl", 0)),
                    exchange_segment=item.get("exchange", "NSE"),
                )
                positions.append(pos)

            log.info(f"Fetched {len(positions)} positions for {self.user_id}")
            return positions

        except Exception as e:
            log.error(f"Failed to fetch positions: {e}")
            return []

    async def fetch_market_data(self, instruments: list[str]) -> dict:
        """Fetch live prices for instruments."""
        try:
            market_data = {}

            for instrument in instruments:
                try:
                    resp = self.session.get(
                        f"{self.base_url}/market/quote",
                        params={"symbol": instrument}
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    if data.get("status") == "success":
                        quote = data.get("quote", {})
                        market_data[instrument] = {
                            "ltp": float(quote.get("last_traded_price", 0)),
                            "change": float(quote.get("change_percent", 0)),
                            "bid": float(quote.get("bid_price", 0)),
                            "ask": float(quote.get("ask_price", 0)),
                        }
                except Exception as e:
                    log.warning(f"Failed to fetch quote for {instrument}: {e}")

            return market_data

        except Exception as e:
            log.error(f"Failed to fetch market data: {e}")
            return {}

    async def place_order(self, order_spec: dict) -> dict:
        """Place order on Grow."""
        try:
            payload = {
                "symbol": order_spec.get("tradingsymbol"),
                "transaction_type": order_spec.get("txn_type", "BUY"),
                "quantity": int(order_spec.get("qty", 1)),
                "order_type": order_spec.get("order_type", "MARKET"),
                "exchange": order_spec.get("exchange", "NSE"),
                "validity": order_spec.get("validity", "DAY"),
            }

            if order_spec.get("order_type") == "LIMIT":
                payload["price"] = float(order_spec.get("price", 0))

            resp = self.session.post(f"{self.base_url}/orders/place", json=payload)
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") == "success":
                order_id = data.get("order_id", "")
                log.info(f"Order placed successfully: {order_id}")
                return {
                    "order_id": order_id,
                    "status": "placed",
                    "message": "Order placed successfully"
                }
            else:
                return {
                    "order_id": None,
                    "status": "failed",
                    "message": data.get("message", "Unknown error")
                }

        except Exception as e:
            log.error(f"Failed to place order: {e}")
            return {
                "order_id": None,
                "status": "failed",
                "message": str(e)
            }

    async def get_portfolio_summary(self) -> dict:
        """Get portfolio summary from Grow."""
        try:
            resp = self.session.get(f"{self.base_url}/account/portfolio")
            resp.raise_for_status()
            data = resp.json()

            positions = await self.fetch_positions()
            total_pnl = sum(p.pnl for p in positions)

            return {
                "total_unrealised_pnl": total_pnl,
                "total_realised_pnl": float(data.get("realised_pnl", 0)),
                "positions": positions,
                "margin_available": float(data.get("margin_available", 0)),
                "margin_used": float(data.get("margin_used", 0)),
            }

        except Exception as e:
            log.error(f"Failed to get portfolio summary: {e}")
            return {
                "total_unrealised_pnl": 0,
                "total_realised_pnl": 0,
                "positions": [],
                "margin_available": 0,
                "margin_used": 0,
            }

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order on Grow."""
        try:
            resp = self.session.post(
                f"{self.base_url}/orders/{order_id}/cancel"
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "success":
                log.info(f"Order cancelled: {order_id}")
                return True
            else:
                log.warning(f"Failed to cancel order {order_id}: {data.get('message')}")
                return False
        except Exception as e:
            log.error(f"Failed to cancel order {order_id}: {e}")
            return False
