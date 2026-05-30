"""
settings.py -- All configuration loaded from .env
"""
import os
from dotenv import load_dotenv

load_dotenv()

# -- INDstocks ----------------------------------------------------------------
INDSTOCKS_ACCESS_TOKEN = os.getenv("INDSTOCKS_ACCESS_TOKEN", "")
INDSTOCKS_BASE_URL     = os.getenv("INDSTOCKS_BASE_URL", "https://api.indstocks.com")

# -- Gemini -------------------------------------------------------------------
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL       = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# -- Telegram -----------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# -- NewsAPI ------------------------------------------------------------------
NEWS_API_KEY       = os.getenv("NEWS_API_KEY", "")

# -- Risk controls ------------------------------------------------------------
MAX_LOSS_PER_TRADE = int(os.getenv("MAX_LOSS_PER_TRADE", "2000"))
DAILY_LOSS_LIMIT   = int(os.getenv("DAILY_LOSS_LIMIT", "5000"))
MAX_LOTS_PER_ORDER = int(os.getenv("MAX_LOTS_PER_ORDER", "2"))
AUTO_EXECUTE       = os.getenv("AUTO_EXECUTE", "false").lower() == "true"
MIN_CONFIDENCE     = int(os.getenv("MIN_CONFIDENCE", "75"))

# -- Timing -------------------------------------------------------------------
POLL_INTERVAL_SEC  = int(os.getenv("POLL_INTERVAL_SEC", "20"))
MARKET_OPEN        = os.getenv("MARKET_OPEN", "09:15")
MARKET_CLOSE       = os.getenv("MARKET_CLOSE", "15:30")

# -- Instruments (INDstocks security_ids) ------------------------------------
TRACKED_INDICES    = os.getenv("TRACKED_INDICES", "13,25,1").split(",")      # Nifty50, BankNifty, VIX
TRACKED_STOCKS     = [s.strip() for s in os.getenv("TRACKED_STOCKS", "").split(",") if s.strip()]
TRACKED_COMMODITIES= [c.strip() for c in os.getenv("TRACKED_COMMODITIES", "").split(",") if c.strip()]

# -- Paths --------------------------------------------------------------------
BASE_DIR           = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH            = os.path.join(BASE_DIR, "data", "trading.db")
LOG_DIR            = os.path.join(BASE_DIR, "logs")
