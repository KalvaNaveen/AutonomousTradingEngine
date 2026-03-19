import os
from dotenv import load_dotenv
load_dotenv()

# ── Timezone ──────────────────────────────────────────────────
# All datetime.now() calls must use IST regardless of server timezone.
# On any cloud instance (AWS, GCP, Azure), the default is UTC.
# UTC+5:30 = IST. Using zoneinfo (Python 3.9+, no extra install).
from zoneinfo import ZoneInfo
IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> "datetime.datetime":
    """Return current datetime in IST. Use everywhere instead of datetime.now()."""
    import datetime
    return datetime.datetime.now(IST)


def today_ist() -> "datetime.date":
    """Return today's date in IST."""
    return now_ist().date()

# ── Paper Trading Mode ────────────────────────────────────────
# true  → PaperBroker intercepts all orders. Live data, virtual fills.
# false → Real KiteConnect. Real orders. Real money.
# Change ONLY in .env — never hardcode here.
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"

# ── Zerodha ───────────────────────────────────────────────────
KITE_API_KEY        = os.getenv("KITE_API_KEY")
KITE_API_SECRET     = os.getenv("KITE_API_SECRET")
KITE_ACCESS_TOKEN   = os.getenv("KITE_ACCESS_TOKEN")
# Set this to the redirect URL configured in your Kite Connect app
# console.zerodha.com → Apps → your app → Redirect URL
KITE_REDIRECT_URL   = os.getenv("KITE_REDIRECT_URL", "https://127.0.0.1")
ZERODHA_USER_ID     = os.getenv("ZERODHA_USER_ID")
ZERODHA_PASSWORD    = os.getenv("ZERODHA_PASSWORD")
ZERODHA_TOTP_SECRET = os.getenv("ZERODHA_TOTP_SECRET")

# ── Telegram ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
# Comma-separated chat IDs — supports one or many recipients
# e.g. "123456789" or "123456789,987654321,111222333"
_raw_ids            = os.getenv("TELEGRAM_CHAT_IDS", "")
TELEGRAM_CHAT_IDS   = [cid.strip() for cid in _raw_ids.split(",") if cid.strip()]

# ── Capital ───────────────────────────────────────────────────
# PAPER MODE:  engine uses this value as fixed capital for the session.
# LIVE MODE:   engine fetches live_balance from kite.margins() at startup
#              and uses that instead. This value is the fallback only if
#              the margins() call fails (network issue, API error, etc.).
# Keep this set to your approximate trading capital as a safety net.
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
VIX_BEAR_PANIC      = 22.5
VIX_NORMAL_HIGH     = 22.0
VIX_NORMAL_LOW      = 12.0
VIX_BULL_MAX        = 18.0
# Hard stop threshold — engine refuses all new entries above this.
# At VIX 30+, market structure breaks down. Spreads widen, circuit breakers
# trigger, intraday reversals become meaningless. No strategy edge exists.
# Engine still monitors and exits open positions — it only blocks new entries.
VIX_EXTREME_STOP    = 30.0

# ── Strategy 1: EMA Divergence (CNC swing) ───────────────────
S1_EMA_PERIOD           = 25
S1_DEVIATION_MIN        = 0.12
S1_DEVIATION_NORMAL     = 0.15
S1_DEVIATION_BULL       = 0.20
S1_DEVIATION_MAX        = 0.35
S1_RSI_THRESHOLD        = 38
S1_VOLUME_MULTIPLIER    = 1.5
S1_HARD_STOP_PCT        = 0.07
S1_MAX_HOLD_DAYS        = 3
S1_MIN_TURNOVER_CR      = 100

# ── Strategy 2: Overreaction Bounce (MIS intraday) ───────────
S2_DROP_MIN             = 0.03
S2_DROP_MAX             = 0.10
S2_RVOL_MIN             = 1.8
S2_PARTIAL_TARGET_1     = 0.012
S2_PARTIAL_TARGET_2     = 0.020
S2_HARD_STOP_PCT        = 0.008
S2_TIME_STOP_MINUTES    = 45
S2_MIN_TURNOVER_CR      = 250

# ── Strategy 3: SEPA + VCP Swing (CNC multi-week, Minervini) ────
# Sourced strictly from: Trade Like a Stock Market Wizard (2013),
# Think & Trade Like a Champion (2017), Mindset Secrets for Winning (2019).
S3_MIN_EPS_GROWTH        = 25.0   # % quarterly YoY (prefer 40–100%)
S3_MIN_SALES_GROWTH      = 20.0   # % quarterly YoY
S3_MIN_ROE               = 17.0   # % annual
S3_MAX_DEBT_EQUITY       = 0.5    # ratio (<50%)
S3_MIN_RS_SCORE          = 70     # 1–99 custom RS rank (≥70 = top 30%)
S3_MIN_TURNOVER_CR       = 25     # Lowers the floor to catch quiet VCP bases in mid-caps
S3_MAX_STOP_PCT          = 0.08   # 8% max stop (Minervini hard rule)
S3_PARTIAL_EXIT_PCT      = 0.22   # 1/3 partial at +22% if < 3 weeks
S3_TARGET_SWING_PCT      = 0.40   # Trail overrides; placeholder R:R
S3_MAX_HOLD_DAYS         = 90     # 3-month swing max
S3_VCP_MIN_CONTRACTIONS  = 2      # Minimum VCP pullback count
S3_VCP_MAX_CONTRACTIONS  = 6
S3_BREAKEVEN_MOVE_PCT    = 0.12   # Move stop to breakeven after +12%
S3_PYRAMID_ADD_PCT       = 0.12   # Pyramid trigger: +12% from entry
S3_STALL_WEEKS           = 3      # No-progress exit: 3 weeks

# ── Strategy 4: Leadership Breakout (CNC momentum, Minervini) ────
S4_MIN_RS_SCORE          = 80     # Top 20% performers
S4_BREAKOUT_VOL_MIN      = 1.5    # ≥150% of average volume
S4_MAX_BELOW_52W_HIGH    = 0.05   # Within 5% of 52-week high
S4_MAX_STOP_PCT          = 0.08   # 8% max
S4_PARTIAL_EXIT_PCT      = 0.20   # Partial at +20%
S4_MAX_HOLD_DAYS         = 60     # 2-month max
S4_MIN_TURNOVER_CR       = 100    # Momentum breakouts need moderate liquidity
S4_BREAKEVEN_MOVE_PCT    = 0.10   # Breakeven after +10%
S4_STALL_WEEKS           = 3
S4_TARGET_SWING_PCT      = 0.40   # 40% swing target for S4 leadership breakouts

# ── Superperformance Stock Profile (Minervini PDF page 13) ──────────
# NSE India mid-cap equivalent: ₹300 Cr – ₹5,000 Cr market cap.
# "Small-mid cap, float 10-100M shares, innovation/new product." — Minervini
S3_MIN_MARKET_CAP_CR    = 300.0    # ≥ ₹300 Cr — exclude micro caps
S3_MAX_MARKET_CAP_CR    = 5000.0   # ≤ ₹5,000 Cr — avoid mega caps
S3_MIN_FLOAT_CR         = 50.0     # Free float ≥ ₹50 Cr (liquidity floor)
S3_INNOVATION_SALES_ACCEL = 80.0   # Sales growth ≥ 80% = innovation proxy

# ── Market Status — Minervini market timing ───────────────────────
NIFTY_DIST_DAYS_LIMIT    = 4      # Distribution days → reduce S3/S4
NIFTY_FTD_MIN_PCT        = 1.25   # Follow-Through Day minimum move % [v12: 1.5→1.25, matches Minervini PDF p.10]
NIFTY_FTD_MIN_DAY        = 4      # Earliest rally day for FTD signal

# ── Timing ────────────────────────────────────────────────────
HUNT_WINDOW_START       = "09:30"
LAST_ENTRY_TIME         = "15:00"
INTRADAY_SQUAREOFF      = "15:15"

# ── Fill monitor polling ──────────────────────────────────────
FILL_POLL_INTERVAL_SEC  = 30
FILL_TIMEOUT_MINUTES    = 30

# ── Paths ─────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
ENV_FILE    = os.path.join(BASE_DIR, ".env")
STATE_DB    = os.path.join(BASE_DIR, "engine_state.db")
JOURNAL_DB  = os.path.join(BASE_DIR, "journal.db")
