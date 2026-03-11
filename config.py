import os
from dotenv import load_dotenv
load_dotenv()

# ── Zerodha ───────────────────────────────────────────────────
KITE_API_KEY        = os.getenv("KITE_API_KEY")
KITE_API_SECRET     = os.getenv("KITE_API_SECRET")
KITE_ACCESS_TOKEN   = os.getenv("KITE_ACCESS_TOKEN")
ZERODHA_USER_ID     = os.getenv("ZERODHA_USER_ID")
ZERODHA_PASSWORD    = os.getenv("ZERODHA_PASSWORD")
ZERODHA_TOTP_SECRET = os.getenv("ZERODHA_TOTP_SECRET")

# ── Telegram ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")

# ── Capital ───────────────────────────────────────────────────
TOTAL_CAPITAL       = float(os.getenv("TRADING_CAPITAL", "500000"))

# ── Risk (BNF Rules — DO NOT CHANGE) ─────────────────────────
MAX_RISK_PER_TRADE_PCT  = 0.01
DAILY_LOSS_LIMIT_PCT    = 0.025
MAX_CONSECUTIVE_LOSSES  = 3
MAX_OPEN_POSITIONS      = 3
MAX_POSITION_PCT        = 0.20

# ── Instrument Tokens ─────────────────────────────────────────
NIFTY50_TOKEN       = 256265
INDIA_VIX_TOKEN     = 264969

# ── 4-Tier Regime Thresholds ──────────────────────────────────
VIX_BEAR_PANIC      = 18.0
VIX_NORMAL_HIGH     = 18.0
VIX_NORMAL_LOW      = 15.0
VIX_BULL_MAX        = 15.0

# ── Strategy 1: EMA Divergence (CNC swing) ───────────────────
S1_EMA_PERIOD           = 25
S1_DEVIATION_MIN        = 0.20
S1_DEVIATION_NORMAL     = 0.25
S1_DEVIATION_BULL       = 0.30
S1_DEVIATION_MAX        = 0.35
S1_RSI_THRESHOLD        = 32
S1_VOLUME_MULTIPLIER    = 1.5
S1_HARD_STOP_PCT        = 0.07
S1_MAX_HOLD_DAYS        = 3
S1_MIN_TURNOVER_CR      = 100

# ── Strategy 2: Overreaction Bounce (MIS intraday) ───────────
S2_DROP_MIN             = 0.05
S2_DROP_MAX             = 0.10
S2_RVOL_MIN             = 2.5
S2_PARTIAL_TARGET_1     = 0.012
S2_PARTIAL_TARGET_2     = 0.020
S2_HARD_STOP_PCT        = 0.008
S2_TIME_STOP_MINUTES    = 45
S2_MIN_TURNOVER_CR      = 500

# ── Timing ────────────────────────────────────────────────────
HUNT_WINDOW_START       = "09:30"
LAST_ENTRY_TIME         = "15:00"
INTRADAY_SQUAREOFF      = "15:15"

# ── Fill monitor polling ──────────────────────────────────────
FILL_POLL_INTERVAL_SEC  = 30
FILL_TIMEOUT_MINUTES    = 5

# ── Paths ─────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
ENV_FILE    = os.path.join(BASE_DIR, ".env")
STATE_DB    = os.path.join(BASE_DIR, "engine_state.db")
JOURNAL_DB  = os.path.join(BASE_DIR, "journal.db")
