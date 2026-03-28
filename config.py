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

# ── Risk — Adaptive Intraday System ──────────────────────────
# Total Capital: Rs.5,00,000
# Active Trading Capital: Rs.4,00,000 (80%)
# Buffer (Risk Reserve): Rs.1,00,000 (20%)
MAX_RISK_PER_TRADE_PCT  = 0.005     # 0.50% of total capital per trade = Rs.2,500
DAILY_LOSS_LIMIT_PCT    = 0.015     # 1.5% daily max loss = Rs.7,500 → stop trading
MAX_CONSECUTIVE_LOSSES  = 4         # Stop after 4 consecutive losses (institutional standard)
MAX_OPEN_POSITIONS      = 3         # Max simultaneous positions
MAX_POSITION_PCT        = 0.25      # Max 25% capital per single position
MAX_TRADES_PER_DAY      = 5         # Hard cap: 5 trades/day across all strategies
EOD_SQUAREOFF_TIME      = "15:10"   # Primary squareoff (5 min buffer before Zerodha auto-sq)
EOD_SQUAREOFF_FINAL     = "15:20"   # Emergency backup only

# === PERFORMANCE & COST BUFFERS (V7) ===
STT_BUFFER                  = 0.997     # ~0.1% brokerage + 0.025% STT sell-side + slippage safety
ACTIVE_CAPITAL_PCT          = 0.80       # 80% for trading
RISK_RESERVE_PCT            = 0.20       # 20% safety buffer
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
VIX_EXTREME_STOP    = 30.0

# ══════════════════════════════════════════════════════════════
#  STRATEGY CONFIGURATIONS — New Strategies.MD (10-Strategy System)
# ══════════════════════════════════════════════════════════════

# ── S1: Moving Average Crossover (MD Strategy 1, lines 51-67) ──────
# Best Regime: Trending (bull/bear). Timeframe: 15-min.
# Long: 9 EMA crosses above 21 EMA AND ADX > 25 AND price > 200 EMA
# Short: 9 EMA crosses below 21 EMA AND ADX > 25 AND price < 200 EMA
S1_EMA_FAST             = 9         # Fast EMA period
S1_EMA_SLOW             = 21        # Slow EMA period
S1_EMA_TREND            = 200       # Higher TF trend filter (daily 200 EMA)
S1_ADX_PERIOD           = 14        # ADX period
S1_ADX_MIN              = 25        # No trade if ADX < 25
S1_ATR_SL_MULT          = 1.5       # Stop: 1.5 × ATR(14) below/above entry
S1_RR                   = 3.0       # Target: 1:3 RR
S1_RISK_PCT             = 0.01      # Max 1% risk per trade

# ── S2: BB + RSI Mean Reversion (MD Strategy 2, lines 69-85) ───────
# Best Regime: Sideways/choppy. Timeframe: 5-min or 15-min.
# Long: Price touches lower BB AND RSI < 30 AND price > VWAP
# Short: Price touches upper BB AND RSI > 70 AND price < VWAP
S2_BB_PERIOD            = 20        # Bollinger Bands period
S2_BB_SD                = 2.0       # Bollinger Bands standard deviation
S2_RSI_PERIOD           = 14        # RSI period
S2_RSI_OVERSOLD         = 30        # RSI < 30 → long signal
S2_RSI_OVERBOUGHT       = 70        # RSI > 70 → short signal
S2_ATR_SL_MULT          = 1.0       # Stop: 1 × ATR below/above entry
S2_RR                   = 2.0       # Target: middle BB or 1:2 RR
S2_RISK_PCT             = 0.005     # Risk 0.5% max
S2_MAX_HOLD_MINS        = 30        # Time exit: 30-min hold max
S2_VIX_MAX              = 25        # Avoid if VIX > 25 (was 20 — elevated but tradeable below 25)

# ── S3: Opening Range Breakout (MD Strategy 3, lines 87-106) ───────
# Best Regime: Volatile/trending days (intraday). Timeframe: 15-min.
# Mark High/Low of 9:15-9:30 AM candle.
# Long: First 15-min candle closes above range High + volume > average
# Short: First 15-min candle closes below range Low + volume > average
S3_RISK_PCT             = 0.0075    # Risk 0.75% max
S3_MAX_TRADES           = 2         # Max 2 trades/day
S3_ENTRY_END            = "11:00"   # Max 90 minutes after ORB formation
S3_EXIT_TIME            = "15:20"   # Mandatory exit by 3:20 PM
S3_TARGET_MULT          = 1.5       # Target: 1.5× range size

# ── S6_TREND_SHORT: Trend Breakout Short (kept from V18) ──────────
# Shorts stocks showing relative weakness on down days (intraday MIS)
S6_RSI_PERIOD           = 14
S6_RSI_ENTRY_LOW        = 48
S6_RSI_ENTRY_HIGH       = 60
S6_RSI_EXIT             = 30
S6_COOLDOWN_DAYS        = 2
S6_MIN_TURNOVER_CR      = 30
S6_RELATIVE_WEAKNESS    = 0.005
S6_RVOL_MIN             = 1.3
S6_VWAP_FILTER          = True


# ── S6_VWAP_BAND: VWAP Mean Reversion (MD Strategy 6, lines 140-155) ──
# Best Regime: Intraday any regime. Timeframe: 5-min.
# Long: Price < VWAP - 1.5 SD in uptrend (higher TF)
# Short: Price > VWAP + 1.5 SD in downtrend
S6_VWAP_SD              = 1.5       # 1.5 standard deviations from VWAP
S6_VWAP_RISK_PCT        = 0.005     # Risk 0.5%
S6_VWAP_RR              = 2.0       # Target: VWAP or 1:2 RR

# ── S7: Mean Reversion Long (kept from V18, intraday MIS) ─────────
S7_RSI_PERIOD           = 14
S7_RSI_OVERSOLD         = 30
S7_RSI_EXIT             = 60
S7_VWAP_DEVIATION_PCT   = 0.004
S7_MIN_TURNOVER_CR      = 50
S7_RVOL_MIN             = 1.2
S7_ATR_PERIOD           = 14

# ── S8: Volume Profile + Pivot Breakout (MD Strategy 8, lines 175-190) ──
# Best Regime: All (volume confirmation). Timeframe: 15-min/daily.
# Long: Break above VAH/R1 pivot + volume spike > average
# Short: Break below VAL/S1 pivot + volume spike
S8_VOL_SPIKE_MULT       = 1.5       # Volume spike must be > 1.5× average
S8_RISK_PCT             = 0.0075    # Risk 0.75%

# ── S9: Multi-Timeframe Trend + Momentum (MD Strategy 9, lines 192-207) ──
# Best Regime: Bull/bear confirmation. Timeframe: Daily + 15-min.
# Higher TF: Price > 200 EMA (uptrend) or < (downtrend)
# Lower TF: RSI > 50 + MACD crossover in trend direction
S9_EMA_TREND            = 200       # Daily 200 EMA for higher TF filter
S9_RSI_PERIOD           = 14        # 15-min RSI period
S9_RSI_THRESHOLD        = 50        # RSI > 50 for bullish momentum
S9_ATR_SL_MULT          = 2.0       # Stop: 2 × ATR
S9_RR                   = 3.0       # Target: 1:3 RR

# ── S4: Cash-Futures Arbitrage (MD Strategy 4, lines 108-121) ──────
# Best Regime: Any (low-risk). Timeframe: Tick/1-min.
# Instruments: Nifty/BankNifty futures vs underlying + rebalancing stocks.
# Entry: Long futures + short cash (or vice versa) when mispricing > 0.15%
# Exit: Convergence (0.05% profit) or max 30-min hold.
# Risk: Near-zero (hedged). Max 2% capital exposure.
# COMMENTED BY USER REQUEST:
# S4_MISPRINT_ENTRY_PCT   = 0.0015    # Enter when futures/spot diff > 0.15%
# S4_MISPRINT_EXIT_PCT    = 0.0005    # Exit when converged to within 0.05%
# S4_MAX_HOLD_MINS        = 30        # Max hold 30 minutes (MD rule, line 117)
# S4_RISK_PCT             = 0.02      # Max 2% capital exposure (MD rule, line 118)
# S4_RISK_FREE_RATE       = 0.065     # RBI repo rate approx 6.5% for fair value calc
# Instruments: Nifty and BankNifty index + their near-month futures
# S4_SPOT_TOKEN           = 256265    # NIFTY50 (same as NIFTY50_TOKEN)
# BANKNIFTY_SPOT_TOKEN    = 260105    # NIFTY BANK index token


# Based on adaptive intraday system research:
# 9:15-9:20  → No trade (opening noise)
# 9:20-11:30 → Active trading window
# 11:30-13:15 → No Trade Zone (choppy, low volume midday)
# 13:15-15:00 → Selective afternoon trades
# 15:00+      → Exit all MIS positions
TRADE_WINDOW_1_START    = "09:20"    # Morning active session
TRADE_WINDOW_1_END      = "11:30"
NO_TRADE_ZONE_START     = "11:30"    # Midday dead zone
NO_TRADE_ZONE_END       = "13:15"
TRADE_WINDOW_2_START    = "13:15"    # Afternoon selective session
TRADE_WINDOW_2_END      = "15:00"
INTRADAY_SQUAREOFF      = "15:15"    # Hard MIS square-off

# ── Stock Selection Filters ──────────────────────────────────
# Daily universe filter: focus on volatile, liquid stocks
MIN_ATR_PERCENTILE      = 50        # Only trade stocks with ATR > median
MIN_DAILY_VOLUME        = 500000    # Minimum daily average volume

# ── Timing ────────────────────────────────────────────────────
HUNT_WINDOW_START       = "09:20"
LAST_ENTRY_TIME         = "15:00"

# ── Fill monitor polling ──────────────────────────────────────
FILL_POLL_INTERVAL_SEC  = 30
FILL_TIMEOUT_MINUTES    = 30

# ── Paths ─────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
ENV_FILE    = os.path.join(BASE_DIR, ".env")
STATE_DB    = os.path.join(BASE_DIR, "data/engine_state.db")
JOURNAL_DB  = os.path.join(BASE_DIR, "data/journal.db")

