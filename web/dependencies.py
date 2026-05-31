"""
dependencies.py — FastAPI dependencies for database access and authentication.

Provides reusable dependency functions for database connections and user authentication.
"""

from fastapi import Depends, HTTPException, status
from data.store import get_conn, get_user
from config.logger import get_logger

log = get_logger("dependencies")


def get_db():
    """Dependency: get database connection."""
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def get_current_user(user_id: str = None):
    """
    Dependency: get current authenticated user.

    Args:
        user_id: User ID from session or query parameter

    Returns:
        User dict if found

    Raises:
        HTTPException 401 if user not authenticated
    """
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )

    user = get_user(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    return user


def verify_broker_credentials(broker_type: str, credentials: dict) -> bool:
    """
    Verify broker credentials by testing connection.

    Args:
        broker_type: 'indstocks' | 'zerodha' | 'grow'
        credentials: {'api_key': '...', 'api_secret': '...'}

    Returns:
        True if credentials are valid, False otherwise
    """
    try:
        from core.brokers import create_broker_adapter
        import asyncio

        adapter = create_broker_adapter(broker_type, "test_user", credentials)

        # Test authentication (async)
        loop = asyncio.new_event_loop()
        is_valid = loop.run_until_complete(adapter.authenticate())
        loop.close()

        return is_valid

    except Exception as e:
        log.warning(f"Broker credential verification failed: {e}")
        return False
