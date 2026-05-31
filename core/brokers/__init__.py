"""
Broker abstraction layer for multi-broker support.

Provides factory function to create broker-specific adapters that implement
a common interface. This allows the trading agent to work with multiple brokers
(INDstocks, Zerodha, Grow) without broker-specific logic in the core loop.

Usage:
    adapter = create_broker_adapter('indstocks', user_id, credentials)
    positions = await adapter.fetch_positions()
    await adapter.place_order(order_spec)
"""

from .broker_adapter import BrokerAdapter, BrokerPosition

__all__ = ['BrokerAdapter', 'BrokerPosition', 'create_broker_adapter']


def create_broker_adapter(broker_type: str, user_id: str, credentials: dict) -> BrokerAdapter:
    """
    Factory function: create appropriate broker adapter.

    Args:
        broker_type: 'indstocks' | 'zerodha' | 'grow'
        user_id: User identifier (for logging)
        credentials: Dict with 'api_key' and 'api_secret'

    Returns:
        BrokerAdapter instance

    Raises:
        ValueError: Unknown broker type
    """
    broker_type = broker_type.lower().strip()

    if broker_type == 'indstocks':
        from .indstocks_adapter import IndStocksAdapter
        return IndStocksAdapter(user_id, credentials['api_key'], credentials['api_secret'])

    elif broker_type == 'zerodha':
        from .zerodha_adapter import ZerodhaAdapter
        return ZerodhaAdapter(user_id, credentials['api_key'], credentials['api_secret'])

    elif broker_type == 'grow':
        from .grow_adapter import GrowAdapter
        return GrowAdapter(user_id, credentials['api_key'], credentials['api_secret'])

    else:
        raise ValueError(f"Unknown broker type: {broker_type}. Supported: indstocks, zerodha, grow")
