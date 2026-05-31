"""
user_context.py — Per-user state and broker session management for multi-tenant agent.

UserContext encapsulates all per-user state:
- Broker adapter (session)
- Position tracker
- Order executor
- Pending signals awaiting approval
- Briefing state (daily flags)
- Daily loss tracking

One UserContext instance per active user.
"""

from dataclasses import dataclass, field
from datetime import datetime
from config.logger import get_logger
from core.brokers import create_broker_adapter
from core.position_tracker import PositionTracker
from execution.order_executor import OrderExecutor
from web.secrets import decrypt_json

log = get_logger("user_context")


@dataclass
class UserContext:
    """Encapsulates all state for a single user."""

    user_id: str
    telegram_chat_id: int
    username: str
    broker_type: str
    signal_mode: str  # 'personal' | 'shared' | 'both'
    is_paused: bool

    # Broker session and trading state
    broker_adapter: object = None
    position_tracker: object = None
    executor: object = None

    # Pending signals awaiting user approval
    pending_signals: dict = field(default_factory=dict)  # signal_id -> signal

    # Daily briefing state
    briefing_state: dict = field(default_factory=lambda: {
        'briefing_sent_today': False,
        'midday_briefing_sent_today': False,
        'afternoon_news_sent_today': False,
        'last_briefing_date': None,
    })

    # Daily P&L tracking
    daily_loss: float = 0.0
    last_pnl_snapshot_ts: str = None

    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @classmethod
    def create(cls, user_id: str, user_data: dict, encrypted_creds: bytes):
        """
        Factory: Create UserContext from database record.

        Args:
            user_id: User ID
            user_data: Dict from get_user() with username, broker_type, etc.
            encrypted_creds: Encrypted credentials bytes from DB

        Returns:
            UserContext instance ready for trading

        Raises:
            ValueError: If broker credentials are invalid
        """
        try:
            # Decrypt credentials
            creds = decrypt_json(encrypted_creds)

            # Create broker adapter
            adapter = create_broker_adapter(
                user_data['broker_type'],
                user_id,
                creds
            )

            # Create trading components
            tracker = PositionTracker(adapter)
            executor = OrderExecutor(adapter)

            ctx = cls(
                user_id=user_id,
                telegram_chat_id=user_data['telegram_chat_id'],
                username=user_data['username'],
                broker_type=user_data['broker_type'],
                signal_mode=user_data['signal_mode'],
                is_paused=user_data['is_paused'],
                broker_adapter=adapter,
                position_tracker=tracker,
                executor=executor,
            )

            log.info(
                f"UserContext created for {user_data['username']} "
                f"({user_data['broker_type']})"
            )

            return ctx

        except Exception as e:
            log.error(f"Failed to create UserContext for {user_id}: {e}")
            raise

    async def fetch_positions(self):
        """Fetch positions from user's broker."""
        try:
            if not self.position_tracker:
                return []
            positions = await self.position_tracker.fetch()
            return positions
        except Exception as e:
            log.error(f"Failed to fetch positions for {self.user_id}: {e}")
            return []

    async def fetch_portfolio_summary(self):
        """Get portfolio summary from user's broker."""
        try:
            if not self.broker_adapter:
                return {}
            summary = await self.broker_adapter.get_portfolio_summary()
            return summary
        except Exception as e:
            log.error(f"Failed to fetch portfolio for {self.user_id}: {e}")
            return {
                'total_unrealised_pnl': 0,
                'total_realised_pnl': 0,
                'positions': [],
                'margin_available': 0,
                'margin_used': 0,
            }

    def reset_daily_state(self):
        """Reset daily flags for new trading day."""
        self.briefing_state = {
            'briefing_sent_today': False,
            'midday_briefing_sent_today': False,
            'afternoon_news_sent_today': False,
            'last_briefing_date': None,
        }
        self.daily_loss = 0.0
        log.info(f"Daily state reset for {self.username}")

    def add_pending_signal(self, signal_id: int, signal: dict):
        """Track a signal awaiting user approval."""
        self.pending_signals[signal_id] = signal
        log.debug(f"Signal #{signal_id} pending for {self.username}")

    def pop_pending_signal(self, signal_id: int):
        """Remove a pending signal (after approval/rejection)."""
        return self.pending_signals.pop(signal_id, None)

    def __repr__(self):
        return (
            f"UserContext(user={self.username}, "
            f"broker={self.broker_type}, "
            f"paused={self.is_paused})"
        )
