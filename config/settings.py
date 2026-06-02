"""
settings.py -- All configuration loaded from .env
"""
import os
from dotenv import load_dotenv

load_dotenv()

# -- INDstocks ----------------------------------------------------------------
INDSTOCKS_ACCESS_TOKEN = os.getenv("INDSTOCKS_ACCESS_TOKEN", "")
INDSTOCKS_BASE_URL     = os.getenv("INDSTOCKS_BASE_URL", "https://api.indstocks.com")

# -- LLM provider -------------------------------------------------------------
# Which backend powers call generation + position review: "azure" | "openai" | "gemini".
LLM_PROVIDER       = os.getenv("LLM_PROVIDER", "azure").lower()

# Gemini (Google AI Studio) — legacy/fallback.
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL       = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Azure OpenAI — endpoint like https://<resource>.openai.azure.com/ ; DEPLOYMENT is the
# deployment NAME you gave the model (e.g. "gpt-4.1-mini"), not the base model id.
AZURE_OPENAI_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
# Optional separate deployment for the heavier /review reasoning (e.g. gpt-4o).
# Falls back to AZURE_OPENAI_DEPLOYMENT when unset. Generation uses the default
# (point it at a cheaper mini model for the high-frequency call generation).
AZURE_OPENAI_DEPLOYMENT_REVIEW = os.getenv("AZURE_OPENAI_DEPLOYMENT_REVIEW", "")

# OpenAI direct (if LLM_PROVIDER="openai").
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL       = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

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

# -- Paper trading (shadow mode) ----------------------------------------------
# A simulated account that "takes" qualifying calls with zero real money, so the
# engine's edge can be proven before any live execution is ever considered.
PAPER_TRADING_ENABLED   = os.getenv("PAPER_TRADING_ENABLED", "true").lower() == "true"
PAPER_START_CAPITAL     = float(os.getenv("PAPER_START_CAPITAL", "100000"))
PAPER_RISK_PCT          = float(os.getenv("PAPER_RISK_PCT", "0.02"))   # risk 2% of equity to stop
PAPER_MAX_OPEN          = int(os.getenv("PAPER_MAX_OPEN", "4"))         # max concurrent positions
PAPER_DAILY_LOSS_PCT    = float(os.getenv("PAPER_DAILY_LOSS_PCT", "0.04"))  # halt new entries for the day
PAPER_PARTIAL_FRACTION  = float(os.getenv("PAPER_PARTIAL_FRACTION", "0.5"))  # book this much at T1
# Categories the paper trader will act on (start narrow: index options only).
PAPER_CATEGORIES        = tuple(
    c.strip() for c in os.getenv("PAPER_CATEGORIES", "index_option").split(",") if c.strip()
)
# Fallback lot sizes when the instruments master is unavailable (SEBI revises these
# periodically; the master is preferred when a session is present).
PAPER_LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 35, "FINNIFTY": 65,
    "MIDCPNIFTY": 120, "SENSEX": 20, "BANKEX": 30,
}

# -- Timing -------------------------------------------------------------------
POLL_INTERVAL_SEC  = int(os.getenv("POLL_INTERVAL_SEC", "5"))   # fast loop: tracking + trigger checks
MARKET_OPEN        = os.getenv("MARKET_OPEN", "09:15")
MARKET_CLOSE       = os.getenv("MARKET_CLOSE", "15:30")

# -- Live generation engine ---------------------------------------------------
# index_option calls are event-driven: regenerate when the market actually moves,
# not on a fixed timer. Guard rails keep the LLM spend sane.
OPT_GEN_MIN_GAP_SEC   = int(os.getenv("OPT_GEN_MIN_GAP_SEC", "45"))    # cooldown between option scans
OPT_GEN_FLOOR_SEC     = int(os.getenv("OPT_GEN_FLOOR_SEC", "120"))     # scan at least this often
GEN_MOVE_PCT          = float(os.getenv("GEN_MOVE_PCT", "0.15"))       # index % move since last scan
GEN_VIX_JUMP_PCT      = float(os.getenv("GEN_VIX_JUMP_PCT", "3.0"))    # India VIX % change since last scan
# Non-option categories (equity/futures/commodity) are swing-y — slower cadence.
OTHER_GEN_INTERVAL_MIN = int(os.getenv("OTHER_GEN_INTERVAL_MIN", "15"))
# ATM strike step per index, for the "new ATM strike" trigger.
ATM_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25, "SENSEX": 100}

# -- Instruments (INDstocks security_ids) ------------------------------------
TRACKED_INDICES    = os.getenv("TRACKED_INDICES", "13,25,1").split(",")      # Nifty50, BankNifty, VIX
TRACKED_STOCKS     = [s.strip() for s in os.getenv("TRACKED_STOCKS", "").split(",") if s.strip()]
TRACKED_COMMODITIES= [c.strip() for c in os.getenv("TRACKED_COMMODITIES", "").split(",") if c.strip()]

# -- Paths --------------------------------------------------------------------
BASE_DIR           = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH            = os.path.join(BASE_DIR, "data", "trading.db")
LOG_DIR            = os.path.join(BASE_DIR, "logs")
