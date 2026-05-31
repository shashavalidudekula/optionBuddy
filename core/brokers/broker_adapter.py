"""
broker_adapter.py — Abstract base class for broker integrations.

Defines the interface that all broker adapters must implement. This allows
the trading agent to treat all brokers uniformly, regardless of their
underlying API differences.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class BrokerPosition:
    """Standardized position format across all brokers."""
    tradingsymbol: str
    security_id: str
    quantity: float
    average_price: float
    last_price: float
    pnl: float
    exchange_segment: str

    def to_dict(self):
        return {
            'tradingsymbol': self.tradingsymbol,
            'security_id': self.security_id,
            'quantity': self.quantity,
            'average_price': self.average_price,
            'last_price': self.last_price,
            'pnl': self.pnl,
            'exchange_segment': self.exchange_segment,
        }


class BrokerAdapter(ABC):
    """
    Abstract base class for broker integrations.

    All broker adapters must inherit from this and implement all abstract methods.
    This ensures compatibility with the trading agent's multi-broker architecture.
    """

    @abstractmethod
    async def authenticate(self) -> bool:
        """
        Verify connection with broker API.

        Returns:
            True if authentication successful, False otherwise
        """
        pass

    @abstractmethod
    async def fetch_positions(self) -> list[BrokerPosition]:
        """
        Fetch all open positions from broker.

        Returns:
            List of BrokerPosition objects
        """
        pass

    @abstractmethod
    async def fetch_market_data(self, instruments: list[str]) -> dict:
        """
        Fetch live market data for specified instruments.

        Args:
            instruments: List of instrument symbols or IDs

        Returns:
            Dict with market data: {'instrument': {'ltp': price, 'change': pct, ...}}
        """
        pass

    @abstractmethod
    async def place_order(self, order_spec: dict) -> dict:
        """
        Place an order on the broker.

        Args:
            order_spec: Order details {
                'txn_type': 'BUY' | 'SELL',
                'instrument': symbol or security_id,
                'quantity': int,
                'order_type': 'MARKET' | 'LIMIT',
                'price': float (for limit orders),
                ...
            }

        Returns:
            {'order_id': str, 'status': 'placed' | 'failed', 'message': str}
        """
        pass

    @abstractmethod
    async def get_portfolio_summary(self) -> dict:
        """
        Get overall portfolio summary.

        Returns:
            {
                'total_unrealised_pnl': float,
                'total_realised_pnl': float,
                'positions': [BrokerPosition],
                'margin_available': float,
                'margin_used': float,
            }
        """
        pass

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order (optional, default raises NotImplementedError).

        Args:
            order_id: ID of order to cancel

        Returns:
            True if cancelled, False if failed
        """
        raise NotImplementedError(f"Order cancellation not supported for this broker")

    async def modify_order(self, order_id: str, new_quantity: int) -> bool:
        """
        Modify quantity of an open order (optional, default raises NotImplementedError).

        Args:
            order_id: ID of order to modify
            new_quantity: New quantity

        Returns:
            True if modified, False if failed
        """
        raise NotImplementedError(f"Order modification not supported for this broker")
