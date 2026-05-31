"""
zerodha_adapter.py — Zerodha broker integration.

Implements BrokerAdapter for Zerodha trading platform using KiteConnect library.
"""

from config.logger import get_logger
from .broker_adapter import BrokerAdapter, BrokerPosition

log = get_logger("zerodha_adapter")


class ZerodhaAdapter(BrokerAdapter):
    """Zerodha broker adapter."""

    def __init__(self, user_id: str, api_key: str, api_secret: str):
        """
        Initialize Zerodha adapter.

        Args:
            user_id: User identifier
            api_key: Zerodha API key
            api_secret: Zerodha access token (not the actual secret)
        """
        self.user_id = user_id

        try:
            from kiteconnect import KiteConnect
            self.kite = KiteConnect(api_key=api_key)
            self.kite.set_access_token(api_secret)
            log.info(f"Initialized ZerodhaAdapter for user {user_id}")
        except ImportError:
            log.error("KiteConnect library not installed. Run: pip install kiteconnect")
            raise
        except Exception as e:
            log.error(f"Failed to initialize Zerodha adapter: {e}")
            raise

    async def authenticate(self) -> bool:
        """Verify Zerodha connection."""
        try:
            profile = self.kite.profile()
            is_valid = profile.get("user_id") is not None
            log.info(f"Zerodha authentication: {'success' if is_valid else 'failed'}")
            return is_valid
        except Exception as e:
            log.warning(f"Zerodha verification failed: {e}")
            return False

    async def fetch_positions(self) -> list[BrokerPosition]:
        """Fetch open positions from Zerodha."""
        try:
            data = self.kite.positions()
            positions = []

            for item in data.get('net', []):
                # Skip closed positions
                if item.get('quantity', 0) == 0:
                    continue

                # Map Zerodha response to BrokerPosition
                pos = BrokerPosition(
                    tradingsymbol=item.get('tradingsymbol', ''),
                    security_id=item.get('instrument_token', ''),
                    quantity=float(item.get('quantity', 0)),
                    average_price=float(item.get('average_price', 0)),
                    last_price=float(item.get('last_price', 0)),
                    pnl=float(item.get('unrealised', 0)),  # Unrealised PnL
                    exchange_segment='NSE' if 'NFO' not in item.get('tradingsymbol', '') else 'NFO',
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
            # Zerodha requires instrument tokens, not symbols
            # For now, we'll fetch quote data if available
            market_data = {}

            for instrument in instruments:
                try:
                    quote = self.kite.quote(instrument)
                    if instrument in quote:
                        q = quote[instrument]
                        market_data[instrument] = {
                            "ltp": float(q.get("last_price", 0)),
                            "change": float(q.get("change", 0)),
                            "bid": float(q.get("bid", 0)),
                            "ask": float(q.get("ask", 0)),
                        }
                except Exception as e:
                    log.warning(f"Failed to fetch quote for {instrument}: {e}")

            return market_data

        except Exception as e:
            log.error(f"Failed to fetch market data: {e}")
            return {}

    async def place_order(self, order_spec: dict) -> dict:
        """Place order on Zerodha."""
        try:
            # Map our order spec to Zerodha parameters
            order_id = self.kite.place_order(
                variety=order_spec.get("variety", "regular"),
                exchange=order_spec.get("exchange", "NSE"),
                tradingsymbol=order_spec.get("tradingsymbol"),
                transaction_type=order_spec.get("txn_type", "BUY"),
                quantity=int(order_spec.get("qty", 1)),
                order_type=order_spec.get("order_type", "MARKET"),
                price=float(order_spec.get("price", 0)) if order_spec.get("order_type") == "LIMIT" else None,
                validity=order_spec.get("validity", "DAY"),
            )

            log.info(f"Order placed successfully: {order_id}")
            return {
                "order_id": order_id,
                "status": "placed",
                "message": "Order placed successfully"
            }

        except Exception as e:
            log.error(f"Failed to place order: {e}")
            return {
                "order_id": None,
                "status": "failed",
                "message": str(e)
            }

    async def get_portfolio_summary(self) -> dict:
        """Get portfolio summary from Zerodha."""
        try:
            positions_data = self.kite.positions()
            positions = await self.fetch_positions()

            # Calculate total PnL
            total_pnl = sum(p.pnl for p in positions)

            # Get margins
            margins = self.kite.margins()
            equity_margins = margins.get('equity', {})

            return {
                "total_unrealised_pnl": total_pnl,
                "total_realised_pnl": positions_data.get('realised', 0),
                "positions": positions,
                "margin_available": float(equity_margins.get('available', 0)),
                "margin_used": float(equity_margins.get('used', 0)),
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
        """Cancel an open order on Zerodha."""
        try:
            self.kite.cancel_order(
                variety="regular",
                order_id=order_id
            )
            log.info(f"Order cancelled: {order_id}")
            return True
        except Exception as e:
            log.error(f"Failed to cancel order {order_id}: {e}")
            return False
