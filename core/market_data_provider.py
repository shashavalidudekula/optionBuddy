"""
market_data_provider.py — single facade over the live market-data backend.

Everything in the app imports market data from HERE, never from a concrete
provider module. The backend is chosen once by MARKET_DATA_PROVIDER:

    "dhan"      → core.dhan_data       (default — native greeks/IV/OI)
    "indstocks" → core.indstocks_data  (legacy fallback)

Only the selected provider is imported, so dropping INDstocks later is a 2-step
change with nothing else to touch:
    1. delete core/indstocks_data.py + core/indstocks_auth.py
    2. delete the `elif ... "indstocks"` branch below

All data functions take the provider-specific `session` (from get_session()) as
their first argument, so the facade just forwards.
"""

from config.settings import MARKET_DATA_PROVIDER
from config.logger import get_logger

log = get_logger("market_provider")

if MARKET_DATA_PROVIDER == "indstocks":
    from core import indstocks_data as _impl
    from core.indstocks_auth import get_session as _get_session
    log.info("Market-data provider: INDstocks")
elif MARKET_DATA_PROVIDER == "dhan":
    from core import dhan_data as _impl
    from core.dhan_auth import get_session as _get_session
    log.info("Market-data provider: Dhan")
else:
    raise ValueError(
        f"Unknown MARKET_DATA_PROVIDER={MARKET_DATA_PROVIDER!r} (use 'dhan' or 'indstocks')"
    )


def get_session():
    """Create + verify a session for the active provider."""
    return _get_session()


def get_market_snapshot(session):
    return _impl.get_market_snapshot(session)


def make_price_lookup(session):
    return _impl.make_price_lookup(session)


def get_option_chain(session, underlying, count: int = 6):
    return _impl.get_option_chain(session, underlying, count)


def get_index_spots(session):
    return _impl.get_index_spots(session)


def option_expiry_for(session, underlying, instrument):
    return _impl.option_expiry_for(session, underlying, instrument)


def get_lot_size(session, underlying):
    return _impl.get_lot_size(session, underlying)


def resolve_scrip_for_call(call):
    return _impl.resolve_scrip_for_call(call)


def get_ltp(session, scrip_codes):
    return _impl.get_ltp(session, scrip_codes)


def get_positions(session):
    """Live broker positions (raw rows) for the read-only portfolio view."""
    return _impl.get_positions(session)


def warm_instruments(session):
    """Pre-load the instrument master so scrip resolution works before first use."""
    return _impl._load_master(session)
