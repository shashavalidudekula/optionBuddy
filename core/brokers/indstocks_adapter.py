"""
indstocks_adapter.py — INDstocks broker integration (refactored from core/indstocks_auth.py).

Implements BrokerAdapter for INDstocks trading platform.
"""

import requests
from config.logger import get_logger
from .broker_adapter import BrokerAdapter, BrokerPosition

log = get_logger("indstocks_adapter")


class IndStocksSession:
    """HTTP session wrapper for INDstocks API."""

    def __init__(self, api_key: str, api_secret: str):
        self.session = requests.Session()
        self.base_url = "https://api.indstocks.com"
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}:{api_secret}",
            "Content-Type": "application/json",
        })

    def get(self, path: str, **kwargs):
        """GET request."""
        url = self.base_url + path
        resp = self.session.get(url, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, json=None, **kwargs):
        """POST request."""
        url = self.base_url + path
        resp = self.session.post(url, json=json, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def verify(self) -> bool:
        """Test connection to INDstocks."""
        try:
            resp = self.get("/account/profile")
            return resp.get("status") == "success"
        except Exception as e:
            log.warning(f"INDstocks verification failed: {e}")
            return False


class IndStocksAdapter(BrokerAdapter):
    """INDstocks broker adapter."""

    def __init__(self, user_id: str, api_key: str, api_secret: str):
        self.user_id = user_id
        self.session = IndStocksSession(api_key, api_secret)
        log.info(f"Initialized IndStocksAdapter for user {user_id}")

    async def authenticate(self) -> bool:
        """Verify INDstocks connection."""
        return self.session.verify()

    async def fetch_positions(self) -> list[BrokerPosition]:
        """Fetch open positions from INDstocks."""
        try:
            resp = self.session.get(
                "/portfolio/positions",
                params={"segment": "derivative", "product": "margin"}
            )

            positions = []
            for item in resp.get("data", []):
                # Skip closed positions
                if item.get("net_qty", 0) == 0:
                    continue

                pos = BrokerPosition(
                    tradingsymbol=item.get("symbol", ""),
                    security_id=item.get("security_id", ""),
                    quantity=float(item.get("net_qty", 0)),
                    average_price=float(item.get("avg_price", 0)),
                    last_price=float(item.get("last_price", 0)),
                    pnl=float(item.get("realised_profit", 0)),
                    exchange_segment=item.get("exchange_segment", "NSE"),
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
            # Convert instrument list to security IDs if needed
            security_ids = ",".join(str(i) for i in instruments)

            resp = self.session.get(
                "/market/quotes/ltp",
                params={"security_id": security_ids, "exchange_segment": "NSE_INDEX,NSE_EQUITY"}
            )

            market_data = {}
            for item in resp.get("data", []):
                symbol = item.get("symbol", "")
                market_data[symbol] = {
                    "ltp": float(item.get("last_traded_price", 0)),
                    "change": float(item.get("change_percent", 0)),
                    "bid": float(item.get("bid_price", 0)),
                    "ask": float(item.get("ask_price", 0)),
                }

            return market_data

        except Exception as e:
            log.error(f"Failed to fetch market data: {e}")
            return {}

    async def place_order(self, order_spec: dict) -> dict:
        """Place order on INDstocks."""
        try:
            payload = {
                "txn_type": order_spec.get("txn_type"),
                "exchange": order_spec.get("exchange", "NSE"),
                "segment": order_spec.get("segment", "DERIVATIVE"),
                "product": order_spec.get("product", "MARGIN"),
                "order_type": order_spec.get("order_type", "MARKET"),
                "validity": order_spec.get("validity", "DAY"),
                "security_id": order_spec.get("security_id"),
                "qty": order_spec.get("qty"),
                "price": order_spec.get("price", 0),
            }

            resp = self.session.post("/orders/place", json=payload)

            if resp.get("status") == "success":
                order_id = resp.get("data", {}).get("order_id", "")
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
                    "message": resp.get("message", "Unknown error")
                }

        except Exception as e:
            log.error(f"Failed to place order: {e}")
            return {
                "order_id": None,
                "status": "failed",
                "message": str(e)
            }

    async def get_portfolio_summary(self) -> dict:
        """Get portfolio summary from INDstocks."""
        try:
            resp = self.session.get("/account/portfolio")

            positions = await self.fetch_positions()
            total_pnl = sum(p.pnl for p in positions)

            return {
                "total_unrealised_pnl": total_pnl,
                "total_realised_pnl": resp.get("realised_pnl", 0),
                "positions": positions,
                "margin_available": resp.get("margin_available", 0),
                "margin_used": resp.get("margin_used", 0),
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
