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

# [V16] Phase 3: Sectoral Indices for SectorAgent
SECTOR_TOKENS = {
    "NIFTY BANK":   260105,
    "NIFTY IT":     259849,
    "NIFTY AUTO":   263433,
    "NIFTY FMCG":   261129,
    "NIFTY METAL":  263689,
    "NIFTY REALTY": 261897,
    "NIFTY ENERGY": 261385,
    "NIFTY PHARMA": 262153,
    "NIFTY INFRA":  261641,
    "NIFTY PSE":    262665
}

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

# ── STRATEGY CONFIGURATIONS ────────────────────────────────────

# S1: Connors RSI Mean Reversion (Swing Long)
S1_RSI_PERIOD           = 4
S1_RSI_OVERSOLD         = 30
S1_RSI_OVERBOUGHT       = 55
S1_BOLLINGER_PERIOD     = 20
S1_BOLLINGER_STD        = 2.0
S1_ATR_PERIOD           = 14
S1_ATR_STOP_MULTIPLIER  = 2.0     # Wider stop to let reversion breathe
S1_HARD_STOP_PCT        = 0.10    # 10% hard cap stop
S1_MAX_HOLD_DAYS        = 5
S1_MIN_TURNOVER_CR      = 100
# Legacy — still referenced by get_s1_min_deviation()
S1_DEVIATION_MIN        = 0.12
S1_DEVIATION_NORMAL     = 0.15
S1_DEVIATION_BULL       = 0.20
S1_DEVIATION_MAX        = 0.35
S1_RSI_THRESHOLD        = 35
S1_VOLUME_MULTIPLIER    = 1.5
S1_EMA_PERIOD           = 25

# S6: Connors RSI(4) Intraday Exhaustion Short (MIS)
S6_RSI_PERIOD           = 4
S6_RSI_OVERBOUGHT       = 82      # Entry threshold (RSI > this)
S6_RSI_EXIT             = 40      # Exit when RSI cools below this
S6_COOLDOWN_DAYS        = 3       # Skip symbol if S6-traded in last N days
S6_MIN_TURNOVER_CR      = 100     # Only short liquid stocks (Rs.100 Cr+)

# S2: Overreaction (Intraday Reversal)
S2_DROP_MIN             = 0.03    # 3% drop minimum (was 4% — too strict)
S2_DROP_MAX             = 0.12 
S2_RVOL_MIN             = 1.5     # 150% opening volume (was 200% — too strict)
S2_PARTIAL_TARGET_1     = 0.010   # Take half at +1% (forces high win rate)
S2_PARTIAL_TARGET_2     = 0.015   # Final exit at +1.5%
S2_HARD_STOP_PCT        = 0.006   # Strict -0.6% stop loss
S2_TIME_STOP_MINUTES    = 30      # Out in 30 mins if stalling
S2_MIN_TURNOVER_CR      = 50      # Rs.50 Cr turnover (was Rs.250 Cr — blocked everything)

# S3: SEPA / VCP (Mid/Small Cap Fundamentals + Tech)
S3_MIN_EPS_GROWTH        = 25.0
S3_MIN_SALES_GROWTH      = 20.0
S3_MIN_ROE               = 17.0
S3_MAX_DEBT_EQUITY       = 0.5
S3_MIN_RS_SCORE          = 60     # Top 40% performers (was 75 — too restrictive for NSE)
S3_MIN_TURNOVER_CR       = 25
S3_MAX_STOP_PCT          = 0.06   # Tighter 6% max stop
S3_PARTIAL_EXIT_PCT      = 0.12   # Secure 1/2 profit at +12%
S3_TARGET_SWING_PCT      = 0.20   # Cap at +20%
S3_MAX_HOLD_DAYS         = 90     
S3_VCP_MIN_CONTRACTIONS  = 2
S3_VCP_MAX_CONTRACTIONS  = 6
S3_BREAKEVEN_MOVE_PCT    = 0.08   # Move stop to breakeven after +8%
S3_PYRAMID_ADD_PCT       = 0.10
S3_STALL_WEEKS           = 2      # Dump non-performers faster

# S4: Leadership Breakout (Large Cap Momentum)
S4_MIN_RS_SCORE          = 85     # Top 15% only
S4_BREAKOUT_VOL_MIN      = 1.3    # 130% volume surge (was 180% — too strict for NSE)
S4_MAX_BELOW_52W_HIGH    = 0.05
S4_MAX_STOP_PCT          = 0.06   # 6% max
S4_PARTIAL_EXIT_PCT      = 0.10   # Bag half the profit at +10%
S4_MAX_HOLD_DAYS         = 60
S4_MIN_TURNOVER_CR       = 100
S4_BREAKEVEN_MOVE_PCT    = 0.06   # Move to B/E instantly at +6% (Boosts Win Rate)
S4_STALL_WEEKS           = 2
S4_TARGET_SWING_PCT      = 0.20   # Cap expectations at +20%

# S5: Open Range Breakout (Intraday Momentum)
S5_ORB_PERIOD_MINUTES    = 15
S5_MIN_ORB_PCT           = 0.005
S5_MAX_ORB_PCT           = 0.025   # Slightly tighter upper bounds
S5_VWAP_PROXIMITY_PCT    = 0.010   # Within 1% of VWAP (was 0.4% — too tight)
S5_ATR_STOP_MULTIPLIER   = 1.0     # 1 ATR max stop
S5_TARGET_RR             = 1.5     # 1:1.5 RR for higher hit rate
S5_MIN_TURNOVER_CR       = 100     # Rs.100 Cr turnover (was Rs.500 Cr — too strict)
S5_MIN_RVOL              = 1.5     # Require more volume
S5_MAX_TRADES_PER_DAY    = 3
S5_HARD_STOP_PCT         = 0.010   # 1.0% absolute max stop

# ── Superperformance Stock Profile (Minervini PDF page 13) ──────────
# NSE India mid-cap equivalent: Rs.300 Cr – Rs.5,000 Cr market cap.
# "Small-mid cap, float 10-100M shares, innovation/new product." — Minervini
S3_MIN_MARKET_CAP_CR    = 300.0    # ≥ Rs.300 Cr — exclude micro caps
S3_MAX_MARKET_CAP_CR    = 5000.0   # ≤ Rs.5,000 Cr — avoid mega caps
S3_MIN_FLOAT_CR         = 50.0     # Free float ≥ Rs.50 Cr (liquidity floor)
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
STATE_DB    = os.path.join(BASE_DIR, "data/engine_state.db")
JOURNAL_DB  = os.path.join(BASE_DIR, "data/journal.db")
