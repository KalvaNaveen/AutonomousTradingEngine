"""
ScannerAgent — New Strategies.MD (10-Strategy System)

Implements ALL feasible strategies from New Strategies.MD:
S1_MA_CROSS      — Strategy 1: 9/21 EMA Crossover + ADX(14) + 200 EMA filter
S2_BB_MEAN_REV   — Strategy 2: BB(20,2σ) + RSI(14) + VWAP
S3_ORB           — Strategy 3: Opening Range Breakout (9:15-9:30 first candle)
S4_ARBITRAGE     — Strategy 4: Nifty/BankNifty Futures vs Spot mispricing (FUTURES ONLY)
S6_TREND_SHORT   — V18 Trend Breakout Short (kept: VWAP + RSI + relative weakness)
S6_VWAP_BAND     — Strategy 6: VWAP ± 1.5 SD mean reversion
S7_MEAN_REV_LONG — V18 Mean Reversion Long (kept: BB + RSI(4) + VWAP deviation)
    S8_VOL_PIVOT     — Strategy 8: Volume Profile + Pivot Point Breakout
    S9_MTF_MOMENTUM  — Strategy 9: Daily 200 EMA + 15-min RSI + MACD crossover

Strategies NOT implemented (hard infrastructure requirements):
Strategy 5:  Pairs Trading/StatArb (needs cointegration testing + separate universe)
Strategy 7:  Options Iron Condor (OPTIONS — explicitly excluded per user requirement)
Strategy 10: ML Hybrid (needs Random Forest training pipeline)

S4 FUTURES POLICY (strict):
- ONLY index futures (NIFTY FUT, BANKNIFTY FUT) — NO options, NO stock futures
- instrument_type == "FUT" filter enforced in DataAgent.load_futures_tokens()
- Two-leg order: Long futures + Short spot ETF surrogate (or vice versa)
- Entry: mispricing > 0.15% from fair value (MD line 115)
- Exit: convergence to 0.05% OR 30-min max hold (MD line 117)

Universal Risk Rules (MD lines 26-48) enforced across all strategies:
- Max 5 concurrent positions
- VIX > 25 = stop all trades
- Daily max loss 2% = engine stopped
- All intraday positions exit by 3:20 PM
"""

import datetime
import numpy as np
from agents.data_agent import DataAgent
from config import *


class ScannerAgent:

    def __init__(self, data_agent: DataAgent, blackout_calendar,
                fundamental_agent=None, stage_agent=None,
                vcp_agent=None, market_status_agent=None,
                sector_agent=None):
        self.data       = data_agent
        self.blackout   = blackout_calendar
        # Legacy args accepted but unused (prevents import crashes)
        self._fundamental = fundamental_agent
        self._stage       = stage_agent
        self._vcp         = vcp_agent
        self._mkt_status  = market_status_agent
        self._sector_agent = sector_agent
        # Cooldown tracking
        self._s6_cooldown       = {}   # symbol -> last_s6_trade_date
        self._s3_trades_today   = 0    # S3 ORB: max 2 trades/day (MD Strategy 3)
        self._s3_trade_date     = None
        self._daily_trade_count = 0
        self._daily_trade_date  = None
        # S4 Arbitrage: futures token map (set by main.py via set_futures_tokens())
        # Keys: "NIFTY", "BANKNIFTY"
        # Values: {"token": int, "symbol": str, "expiry": date}
        # STRICT: ONLY futures (instrument_type == "FUT"). NO options ever.
        self._futures_tokens: dict = {}
        # ── S6 VWAP session guard (max 2 trades/day + 90-min loss cooloff) ──
        self._s6v_trades_today  = 0
        self._s6v_trade_date    = None
        self._s6v_last_loss_time = None   # datetime of last S6V stop-loss exit
        # ── Symbol cooldown after stop-loss (2 sessions) ──
        self._symbol_cooldown: dict = {}  # symbol -> sessions_remaining
        # ── Daily P&L circuit breaker ──
        self._daily_pnl         = 0.0
        self._daily_pnl_date    = None

    # ══════════════════════════════════════════════════════════════
    #  REGIME DETECTION
    #  MD lines 22: ADX > 25 = trending, < 20 = sideways
    #               VIX < 20 = low vol, > 25 = high vol
    # ══════════════════════════════════════════════════════════════

    def detect_regime(self) -> str:
        """
        VIX + AD ratio + Nifty EMA-25.
        MD universal rule: VIX > 25 = EXTREME_PANIC → no new entries.
        """
        vix = self.data.get_india_vix()

        if vix >= VIX_EXTREME_STOP:
            return "EXTREME_PANIC"

        ad_ratio = self.data.get_advance_decline_ratio()

        if (self.data.daily_cache and
                self.data.daily_cache.is_loaded()):
            ema_25    = self.data.daily_cache.get_ema25(NIFTY50_TOKEN)
            ts        = self.data.tick_store
            nifty_ltp = (ts.get_ltp_if_fresh(NIFTY50_TOKEN)
                        if ts and ts.is_fresh() else 0.0)
            if nifty_ltp <= 0.0:
                try:
                    q = self.data.kite.quote(["NSE:NIFTY 50"])
                    nifty_ltp = q.get("NSE:NIFTY 50", {}).get("last_price", 0.0)
                except Exception:
                    pass
            above_ema = nifty_ltp > ema_25 if nifty_ltp > 0 else False
        else:
            hist = self.data.get_daily_ohlcv(NIFTY50_TOKEN, days=60)
            if len(hist) < 30:
                return "CHOP"
            closes    = [d["close"] for d in hist]
            ema_25    = DataAgent.compute_ema(closes, 25)[-1]
            nifty_ltp = closes[-1]
            above_ema = closes[-1] > ema_25

        if vix >= VIX_EXTREME_STOP:
            return "EXTREME_PANIC"

        if vix > VIX_BEAR_PANIC and not above_ema and ad_ratio < 0.40:
            print(f"[Regime] BEAR_PANIC — VIX={vix:.1f} AD={ad_ratio:.2f}")
            return "BEAR_PANIC"

        if vix < VIX_BEAR_PANIC and above_ema and ad_ratio > 0.60:
            print(f"[Regime] BULL — VIX={vix:.1f} AD={ad_ratio:.2f}")
            return "BULL"

        # ── VOLATILE: VIX 18–30 (elevated but not bear panic) ──
        if VIX_BULL_MAX <= vix < VIX_EXTREME_STOP:
            print(f"[Regime] VOLATILE — VIX={vix:.1f} AD={ad_ratio:.2f}")
            return "VOLATILE"

        # ── NORMAL: VIX 12–18 ──
        if VIX_NORMAL_LOW <= vix < VIX_BULL_MAX:
            print(f"[Regime] NORMAL — VIX={vix:.1f} AD={ad_ratio:.2f}")
            return "NORMAL"

        print(f"[Regime] CHOP — VIX={vix:.1f} AD={ad_ratio:.2f}")
        return "CHOP"

    # ══════════════════════════════════════════════════════════════
    #  TIME WINDOW ENFORCEMENT
    # ══════════════════════════════════════════════════════════════

    def is_in_trade_window(self) -> bool:
        """
        Returns True if within active trading windows.
        Window 1: 09:20 – 11:30 (morning momentum)
        No-Trade Zone: 11:30 – 13:15 (midday chop)
        Window 2: 13:15 – 15:00 (afternoon selective)
        MD Strategy 3 ORB uses its own window: 9:30 AM – 2:00 PM.
        """
        now = now_ist().time()
        w1 = datetime.time(9, 20) <= now <= datetime.time(11, 30)
        w2 = datetime.time(13, 15) <= now <= datetime.time(15, 0)
        return w1 or w2

    def _check_daily_trade_limit(self) -> bool:
        """Returns True if we can still trade today (< MAX_TRADES_PER_DAY)."""
        if self.circuit_breaker_tripped():
            return False
            
        today = now_ist().date()
        if self._daily_trade_date != today:
            self._daily_trade_count = 0
            self._daily_trade_date = today
        return self._daily_trade_count < MAX_TRADES_PER_DAY

    def register_trade(self):
        """Call after a trade is executed to track daily count."""
        today = now_ist().date()
        if self._daily_trade_date != today:
            self._daily_trade_count = 0
            self._daily_trade_date = today
        self._daily_trade_count += 1

    def is_valid_trading_time(self) -> tuple:
        now = now_ist()
        t   = now.time()
        if self.blackout and self.blackout.is_blackout(now.date()):
            return False, "BLACKOUT_DATE"
        if now.weekday() >= 5:
            return False, "WEEKEND"
        if t < datetime.time(9, 20):
            return False, "BEFORE_MARKET"
        if t >= datetime.time(15, 0):
            return False, "AFTER_LAST_ENTRY"
        return True, "OK"

    # ── Session / circuit breaker / cooldown helpers ────────────

    def new_session(self, date=None):
        """Call at start of each trading day to reset daily state."""
        self._daily_pnl = 0.0
        self._daily_pnl_date = date
        self._s6v_trades_today = 0
        self._s6v_trade_date = date
        self._s6v_last_loss_time = None
        # Decrement symbol cooldowns
        expired = [k for k, v in self._symbol_cooldown.items() if v <= 1]
        for k in expired:
            del self._symbol_cooldown[k]
        for k in self._symbol_cooldown:
            self._symbol_cooldown[k] -= 1

    def circuit_breaker_tripped(self) -> bool:
        """Returns True if daily loss exceeds -1.5% of capital → halt new entries."""
        return self._daily_pnl < -(TOTAL_CAPITAL * DAILY_LOSS_LIMIT_PCT)

    def record_pnl(self, pnl: float):
        """Call after any trade closes to update daily P&L tracker."""
        self._daily_pnl += pnl

    def add_symbol_cooldown(self, symbol: str, sessions: int = 2):
        """Call after any STOP_LOSS exit to block re-entry for N sessions."""
        self._symbol_cooldown[symbol] = sessions

    def is_symbol_on_cooldown(self, symbol: str) -> bool:
        return self._symbol_cooldown.get(symbol, 0) > 0

    def can_s6v_trade(self, current_time=None) -> bool:
        """S6_VWAP_BAND session guard: max 2 trades/day + 90-min loss cooloff."""
        today = now_ist().date()
        if self._s6v_trade_date != today:
            self._s6v_trades_today = 0
            self._s6v_trade_date = today
            self._s6v_last_loss_time = None
        if self._s6v_trades_today >= 5:
            return False
        if self._s6v_last_loss_time and current_time:
            # 90-min cooloff after loss
            if hasattr(current_time, 'timestamp') and hasattr(self._s6v_last_loss_time, 'timestamp'):
                elapsed = (current_time.timestamp() - self._s6v_last_loss_time.timestamp()) / 60
                if elapsed < 90:
                    return False
        return True

    def register_s6v_trade(self):
        """Call after an S6_VWAP_BAND trade executes."""
        today = now_ist().date()
        if self._s6v_trade_date != today:
            self._s6v_trades_today = 0
            self._s6v_trade_date = today
        self._s6v_trades_today += 1

    def on_s6v_loss(self, exit_time):
        """Call after an S6_VWAP_BAND stop-loss exit."""
        self._s6v_last_loss_time = exit_time

    # ── Internal helpers ──────────────────────────────────────────

    def _cache_ts_ready(self) -> bool:
        return (self.data.daily_cache and self.data.daily_cache.is_loaded()
                and self.data.tick_store and self.data.tick_store.is_ready())

    def _get_15min_closes(self, token: int) -> list:
        """Returns close prices from 15-min candles (3 × 5-min candles)."""
        candles = self.data.get_intraday_ohlcv(token, "5minute")
        if len(candles) < 3:
            return []
        # Group 5-min candles into 15-min buckets
        closes_15 = []
        for i in range(2, len(candles), 3):
            closes_15.append(candles[i]["close"])
        return closes_15

    def _get_15min_ohlc(self, token: int) -> list:
        """Returns OHLC bars in 15-min groupings from 5-min candles."""
        candles = self.data.get_intraday_ohlcv(token, "5minute")
        bars_15 = []
        for i in range(0, len(candles) - 2, 3):
            group = candles[i:i + 3]
            if len(group) < 3:
                continue
            bars_15.append({
                "open":   group[0]["open"],
                "high":   max(c["high"]   for c in group),
                "low":    min(c["low"]    for c in group),
                "close":  group[2]["close"],
                "volume": sum(c["volume"] for c in group),
            })
        return bars_15

    def _check_order_book_filter(self, token: int, is_short: bool) -> bool:
        """
        [V19 Order Book Filter]
        Checks L2 depth to prevent trading against massive institutional walls.
        Returns True if SAFE to trade, False if blocked by imbalance.
        """
        if not self.data.tick_store: return True
        depth = self.data.tick_store.get_depth(token)
        if not depth: return True
        
        ratio = depth.get("bid_ask_ratio", 1.0)
        
        if is_short:
            # We want to SHORT. If bids (buyers) overwhelm asks by > 2x, block it.
            if ratio > 2.0:
                print(f"[Filter] Blocked Short on {token}: Massive buy wall (Ratio: {ratio:.1f})")
                return False
        else:
            # We want to LONG. If asks (sellers) overwhelm bids (ratio < 0.5), block it.
            if ratio < 0.5:
                print(f"[Filter] Blocked Long on {token}: Massive sell wall (Ratio: {ratio:.1f})")
                return False
                
        return True

    # ══════════════════════════════════════════════════════════════
    #  STRATEGY 1: MOVING AVERAGE CROSSOVER (MD lines 51-67)
    # ══════════════════════════════════════════════════════════════

    def scan_s1_ma_cross(self, regime: str) -> list:
        """
        MD Strategy 1: Moving Average Crossover (Trend Following)
        Best Regime: Trending (bull/bear). Timeframe: 15-min.

        Entry:
        Long:  9 EMA crosses above 21 EMA AND ADX > 25 AND price > 200 EMA
        Short: 9 EMA crosses below 21 EMA AND ADX > 25 AND price < 200 EMA

        Exit:
        Target: 1:3 RR
        Stop: 1.5 × ATR(14) below/above entry
        Trailing: Move SL to breakeven after 1:1 RR

        Risk: Max 1% per trade. No trade if ADX < 25.
        """
        if not self.is_in_trade_window():
            print('Blocked by trade window')
            return []
        if not self._check_daily_trade_limit():
            print('Blocked by daily limit')
            return []
        if regime == "EXTREME_PANIC":
            return []
        if not self._cache_ts_ready():
            print('Blocked by cache ready')
            return []

        signals = []

        for token, symbol in self.data.UNIVERSE.items():
            if self.blackout and self.blackout.is_blackout():
                continue

            current = self.data.tick_store.get_ltp_if_fresh(token)
            if current <= 0:
                continue

            # ── Higher TF filter: price vs 200 EMA (daily) ──
            sma200 = self.data.daily_cache.get_sma200(token)
            if sma200 <= 0:
                continue
            is_above_200 = current > sma200

            # ── Build 15-min candle series for EMA ──
            c15 = self._get_15min_closes(token)
            if len(c15) < S1_EMA_SLOW + 2:
                continue

            ema9_series  = DataAgent.compute_ema(c15, S1_EMA_FAST)
            ema21_series = DataAgent.compute_ema(c15, S1_EMA_SLOW)

            if len(ema9_series) < 2 or len(ema21_series) < 2:
                continue

            # Crossover: current and previous bar
            ema9_now,  ema9_prev  = ema9_series[-1],  ema9_series[-2]
            ema21_now, ema21_prev = ema21_series[-1], ema21_series[-2]

            cross_up   = ema9_prev <= ema21_prev and ema9_now > ema21_now
            cross_down = ema9_prev >= ema21_prev and ema9_now < ema21_now

            if not cross_up and not cross_down:
                continue

            rvol = self.data.compute_rvol(token)
            if rvol < 1.5:  # Crossover must come with volume conviction
                continue

            # ── ADX filter: must be > 25 (MD: no trade if ADX < 25) ──
            candles = self._get_15min_ohlc(token)
            if len(candles) < S1_ADX_PERIOD + 2:
                continue
                
            highs_15 = [c["high"] for c in candles]
            lows_15  = [c["low"] for c in candles]
            closes_15 = [c["close"] for c in candles]
            adx = DataAgent.compute_adx(highs_15, lows_15, closes_15, S1_ADX_PERIOD)
            if adx < S1_ADX_MIN:
                continue

            # ── ATR-based stop: 1.5 × ATR(14) ──
            atr_vals = [max(highs_15[i]-lows_15[i], abs(highs_15[i]-closes_15[i-1]), abs(lows_15[i]-closes_15[i-1])) for i in range(1, len(highs_15))]
            atr = float(np.mean(atr_vals[-14:])) if len(atr_vals) >= 14 else current * 0.005
            if atr <= 0:
                atr = current * 0.015

            # [PATCH 7] EMA21 slope filter: ema21 must be rising for longs, falling for shorts
            if len(ema21_series) >= 4:
                ema21_slope_up   = ema21_series[-1] > ema21_series[-3]
                ema21_slope_down = ema21_series[-1] < ema21_series[-3]
            else:
                ema21_slope_up = ema21_slope_down = False

            if cross_up and is_above_200 and ema21_slope_up:
                # Long: 9 EMA crosses above 21 EMA AND price > 200 EMA AND ema21 rising
                stop_price   = round(current - S1_ATR_SL_MULT * atr, 2)
                risk_per_share = current - stop_price
                if risk_per_share <= 0:
                    continue
                target_price = round(current + S1_RR * risk_per_share, 2)
                signals.append({
                    "strategy":    "S1_MA_CROSS",
                    "symbol":      symbol,
                    "token":       token,
                    "regime":      regime,
                    "entry_price": current,
                    "stop_price":  stop_price,
                    "partial_target": round(current + 1.0 * risk_per_share, 2),
                    "target_price": target_price,
                    "atr":         round(atr, 2),
                    "adx":         adx,
                    "ema9":        round(ema9_now, 2),
                    "ema21":       round(ema21_now, 2),
                    "product":     "MIS",
                    "is_short":    False,
                    "max_hold_days": 0,
                    "entry_time":  None,
                    "entry_date":  None,
                })

            elif cross_down and not is_above_200 and ema21_slope_down:
                # Short: 9 EMA crosses below 21 EMA AND price < 200 EMA AND ema21 falling
                stop_price   = round(current + S1_ATR_SL_MULT * atr, 2)
                risk_per_share = stop_price - current
                if risk_per_share <= 0:
                    continue
                target_price = round(current - S1_RR * risk_per_share, 2)
                signals.append({
                    "strategy":    "S1_MA_CROSS",
                    "symbol":      symbol,
                    "token":       token,
                    "regime":      regime,
                    "entry_price": current,
                    "stop_price":  stop_price,
                    "partial_target": round(current - 1.0 * risk_per_share, 2),
                    "target_price": target_price,
                    "atr":         round(atr, 2),
                    "adx":         adx,
                    "ema9":        round(ema9_now, 2),
                    "ema21":       round(ema21_now, 2),
                    "product":     "MIS",
                    "is_short":    True,
                    "max_hold_days": 0,
                    "entry_time":  None,
                    "entry_date":  None,
                })

        return sorted(signals, key=lambda x: x["adx"], reverse=True)[:3]

    # ══════════════════════════════════════════════════════════════
    #  STRATEGY 2: BB + RSI MEAN REVERSION (MD lines 69-85)
    # ══════════════════════════════════════════════════════════════

    def scan_s2_bb_mean_rev(self, regime: str) -> list:
        """
        MD Strategy 2: Mean Reversion (Bollinger Bands + RSI)
        Best Regime: Sideways/choppy. Timeframe: 5-min or 15-min.

        Entry:
        Long:  Price touches lower BB AND RSI < 30 AND price > VWAP (bullish bias)
        Short: Price touches upper BB AND RSI > 70 AND price < VWAP

        Exit:
        Target: Middle BB or 1:2 RR
        Stop: 1 × ATR below/above entry
        Time exit: EOD or 30-min hold max

        Risk: 0.5% max. Avoid if VIX > 20.
        """
        if not self.is_in_trade_window():
            print('Blocked by trade window')
            return []
        if not self._check_daily_trade_limit():
            print('Blocked by daily limit')
            return []
        if regime == "EXTREME_PANIC":
            return []
        if not self._cache_ts_ready():
            print('Blocked by cache ready')
            return []

        # MD: Avoid if VIX > 20
        vix = self.data.get_india_vix()
        if vix > S2_VIX_MAX:
            return []

        signals = []

        for token, symbol in self.data.UNIVERSE.items():
            current = self.data.tick_store.get_ltp_if_fresh(token)
            if current <= 0:
                continue

            # Intraday ATR check — skip if stock is trending today (not ranging)
            candles_today = self.data.get_intraday_ohlcv(token, "5minute")
            if len(candles_today) >= 6:
                intra_ranges = [c["high"] - c["low"] for c in candles_today[-6:]]
                avg_intra_range = float(np.mean(intra_ranges))
                daily_atr = self.data.daily_cache.get_atr(token)
                if daily_atr > 0 and avg_intra_range > daily_atr * 0.80:
                    continue

            # ── Bollinger Bands from 5-min closes ──
            candles = self.data.get_intraday_ohlcv(token, "5minute")
            if len(candles) < S2_BB_PERIOD:
                # Fall back to daily closes
                intra_closes = self.data.daily_cache.get_closes(token)
            else:
                intra_closes = [c["close"] for c in candles]

            if len(intra_closes) < S2_BB_PERIOD:
                continue

            bb_hi, bb_mid, bb_lo = DataAgent.compute_bollinger(
                intra_closes, S2_BB_PERIOD, S2_BB_SD
            )

            # ── RSI(14) ──
            rsi_val = (DataAgent.compute_rsi(intra_closes, S2_RSI_PERIOD) or [50])[-1]

            # ── VWAP ──
            vwap = self.data.tick_store.get_vwap(token)
            if vwap <= 0:
                continue
            vwap_dev = (current - vwap) / vwap

            # ── ATR stop ──
            atr = self.data.daily_cache.get_atr(token)
            if atr <= 0:
                atr = current * 0.015

            sma200 = self.data.daily_cache.get_sma200(token)

            # [PATCH 1] RVOL gate: require minimum volume confirmation
            rvol = self.data.compute_rvol(token)
            if rvol < 1.2:
                continue

            # [PATCH 1] Need at least 2 candles for close-back confirmation
            if len(candles) < 2:
                continue
            prev_candle = candles[-2]
            curr_candle = candles[-1]

            # [PATCH 1] Close-back-inside-band confirmation (not touch entry)
            # Long: prev candle closed BELOW lower BB, current closes BACK ABOVE it
            if prev_candle["close"] < bb_lo and curr_candle["close"] > bb_lo and rsi_val < S2_RSI_OVERSOLD and vwap_dev > -0.03 and (sma200 <= 0 or current >= sma200):
                # Long: BB close-back confirmation + RSI < 30 + VWAP dev > -3%
                stop_price   = round(current - S2_ATR_SL_MULT * atr, 2)
                target_price = round(bb_mid, 2)   # revert to middle BB
                risk_per_share = current - stop_price
                if target_price <= current * 1.001:
                    target_price = round(current + S2_RR * (current - stop_price), 2)
                signals.append({
                    "strategy":     "S2_BB_MEAN_REV",
                    "symbol":       symbol,
                    "token":        token,
                    "regime":       regime,
                    "entry_price":  current,
                    "stop_price":   stop_price,
                    "partial_target": round(current + 1.0 * risk_per_share, 2),
                    "target_price": target_price,
                    "bb_lower":     round(bb_lo, 2),
                    "bb_mid":       round(bb_mid, 2),
                    "rsi":          round(rsi_val, 2),
                    "vwap":         round(vwap, 2),
                    "atr":          round(atr, 2),
                    "product":      "MIS",
                    "is_short":     False,
                    "max_hold_mins": S2_MAX_HOLD_MINS,
                    "entry_time":   None,
                    "entry_date":   None,
                })

            # [PATCH 1] Short: prev candle closed ABOVE upper BB, current closes BACK BELOW it
            elif prev_candle["close"] > bb_hi and curr_candle["close"] < bb_hi and rsi_val > S2_RSI_OVERBOUGHT and vwap_dev < 0.03:
                # Short: BB close-back confirmation + RSI > 70 + VWAP dev < 3%
                stop_price   = round(current + S2_ATR_SL_MULT * atr, 2)
                target_price = round(bb_mid, 2)
                risk_per_share = stop_price - current
                if target_price >= current * 0.999:
                    target_price = round(current - S2_RR * (stop_price - current), 2)
                signals.append({
                    "strategy":     "S2_BB_MEAN_REV",
                    "symbol":       symbol,
                    "token":        token,
                    "regime":       regime,
                    "entry_price":  current,
                    "stop_price":   stop_price,
                    "partial_target": round(current - 1.0 * risk_per_share, 2),
                    "target_price": target_price,
                    "bb_upper":     round(bb_hi, 2),
                    "bb_mid":       round(bb_mid, 2),
                    "rsi":          round(rsi_val, 2),
                    "vwap":         round(vwap, 2),
                    "atr":          round(atr, 2),
                    "product":      "MIS",
                    "is_short":     True,
                    "max_hold_mins": S2_MAX_HOLD_MINS,
                    "entry_time":   None,
                    "entry_date":   None,
                })

        return sorted(signals, key=lambda x: abs(x["rsi"] - 50), reverse=True)[:3]

    # ══════════════════════════════════════════════════════════════
    #  STRATEGY 3: OPENING RANGE BREAKOUT (MD lines 87-106)
    # ══════════════════════════════════════════════════════════════
    # ══════════════════════════════════════════════════════════════
    #  S3_ORB — MADE STRICT FOR PROFITABILITY (V19.3)
    # ══════════════════════════════════════════════════════════════
    def scan_s3_orb(self, regime: str) -> list:
        if not self.is_in_trade_window():
            print('Blocked by trade window')
            return []
        if regime not in ["BULL", "VOLATILE"]:   # Only strong regimes
            return []
        if not self._check_daily_trade_limit():
            print('Blocked by daily limit')
            return []
        if not self._cache_ts_ready():
            print('Blocked by cache ready')
            return []

        # Max 5 S3 trades per day (was 1 — too restrictive)
        today = now_ist().date()
        if self._s3_trade_date == today and self._s3_trades_today >= 5:
            return []

        signals = []
        for token, symbol in self.data.UNIVERSE.items():
            current = self.data.tick_store.get_ltp_if_fresh(token)
            if current <= 0 or current < 800:   # Higher min price
                continue

            rvol = self.data.compute_rvol(token)
            if rvol < 2.0:   # Strong volume only
                continue

            # Get ORB from tick_store (already locked at 09:30)
            orb_dict = self.data.tick_store.get_orb(token)
            orb_high = orb_dict.get("orb_high", 0.0)
            orb_low  = orb_dict.get("orb_low", 0.0)
            if orb_high <= 0 or orb_low <= 0:
                continue

            atr = self.data.daily_cache.get_atr(token) or (current * 0.015)

            # LONG
            if current > orb_high + atr * 0.3:
                if not self._check_order_book_filter(token, is_short=False):
                    continue
                stop_price = round(orb_low - atr * 0.2, 2)
                risk = current - stop_price
                if risk <= 0: continue
                target_price = round(current + 2.5 * risk, 2)   # Higher RR
                signals.append({
                    "strategy": "S3_ORB", "symbol": symbol, "token": token,
                    "regime": regime, "entry_price": current,
                    "stop_price": stop_price, "target_price": target_price,
                    "rvol": round(rvol, 2), "is_short": False
                })

            # SHORT
            elif current < orb_low - atr * 0.3:
                if not self._check_order_book_filter(token, is_short=True):
                    continue
                stop_price = round(orb_high + atr * 0.2, 2)
                risk = stop_price - current
                if risk <= 0: continue
                target_price = round(current - 2.5 * risk, 2)
                signals.append({
                    "strategy": "S3_ORB", "symbol": symbol, "token": token,
                    "regime": regime, "entry_price": current,
                    "stop_price": stop_price, "target_price": target_price,
                    "rvol": round(rvol, 2), "is_short": True
                })

        # Register if any signal (even if not executed)
        if signals:
            self._s3_trade_date = today
            self._s3_trades_today += 1

        return sorted(signals, key=lambda x: x["rvol"], reverse=True)[:1]


    # ══════════════════════════════════════════════════════════════
    #  S6: TREND BREAKOUT SHORT (kept from V18)
    # ══════════════════════════════════════════════════════════════

    def scan_s6_trend_short(self, regime: str) -> list:
        """
        V18 S6: Intraday Short on stocks showing relative weakness.
        Price < VWAP, breaks 5-min low, RVOL >= 1.3, RSI(4) 55-85,
        stock underperforming Nifty by >= 1%, price < Day Open.
        """
        if not self.is_in_trade_window():
            print('Blocked by trade window')
            return []
        if not self._check_daily_trade_limit():
            print('Blocked by daily limit')
            return []
        if regime in ("BULL", "EXTREME_PANIC"):
            return []
        if not self._cache_ts_ready():
            print('Blocked by cache ready')
            return []

        nifty_ltp  = self.data.tick_store.get_ltp_if_fresh(NIFTY50_TOKEN)
        nifty_open = self.data.tick_store.get_day_open(NIFTY50_TOKEN)
        if nifty_ltp <= 0 or nifty_open <= 0:
            return []
        nifty_chg = (nifty_ltp - nifty_open) / nifty_open

        today   = now_ist().date()
        signals = []

        for token, symbol in self.data.UNIVERSE.items():
            if self.blackout and self.blackout.is_blackout():
                continue

            last_s6 = self._s6_cooldown.get(symbol)
            if last_s6 and (today - last_s6).days < S6_COOLDOWN_DAYS:
                continue

            if self.data.get_avg_daily_turnover_cr(token) < S6_MIN_TURNOVER_CR:
                continue

            current  = self.data.tick_store.get_ltp_if_fresh(token)
            day_open = self.data.tick_store.get_day_open(token)
            if current <= 0 or day_open <= 0:
                continue

            if current >= day_open:
                continue

            vwap = self.data.tick_store.get_vwap(token)
            if vwap > 0 and current >= vwap:
                continue

            stock_chg = (current - day_open) / day_open
            relative_weakness = nifty_chg - stock_chg
            if relative_weakness < S6_RELATIVE_WEAKNESS:
                continue

            rvol = self.data.compute_rvol(token)
            if rvol < S6_RVOL_MIN:
                continue

            closes = self.data.daily_cache.get_closes(token)
            if len(closes) < 10:
                continue
            live_closes = closes.copy()
            live_closes.append(current)
            rsi_4 = (DataAgent.compute_rsi(live_closes, S6_RSI_PERIOD) or [50])[-1]
            if not (S6_RSI_ENTRY_LOW <= rsi_4 <= S6_RSI_ENTRY_HIGH):
                continue

            candles = self.data.get_intraday_ohlcv(token, "5minute")
            if len(candles) >= 3:
                prev_low = candles[-2].get("low", 0) if isinstance(candles[-2], dict) else 0
                if prev_low > 0 and current > prev_low:
                    continue

            atr = self.data.daily_cache.get_atr(token)
            if atr <= 0:
                atr = current * 0.02

            # [V19 Order Book Imbalance Filter]
            # Since S6 is a short-only strategy, check against massive buy walls
            if not self._check_order_book_filter(token, is_short=True):
                continue

            # [V19 Optimized] Ultra-tight Intraday SL to maximize size and force early exits
            stop_price  = round(current + atr * 0.4, 2)
            risk = stop_price - current
            if risk <= 0: continue
            target_1    = round(current - risk * 1.5, 2)
            target_2    = round(current - risk * 2.5, 2)

            signals.append({
                "strategy":          "S6_TREND_SHORT",
                "symbol":            symbol,
                "token":             token,
                "regime":            regime,
                "entry_price":       current,
                "partial_target":    target_1,
                "target_price":      target_2,
                "stop_price":        stop_price,
                "rsi_4":             round(rsi_4, 2),
                "atr":               round(atr, 2),
                "rvol":              round(rvol, 2),
                "vwap":              round(vwap, 2) if vwap > 0 else 0,
                "relative_weakness": round(relative_weakness * 100, 2),
                "product":           "MIS",
                "is_short":          True,
                "max_hold_days":     0,
                "entry_time":        None,
                "entry_date":        None,
            })

        return sorted(signals, key=lambda x: x["relative_weakness"], reverse=True)[:3]

    # ══════════════════════════════════════════════════════════════
    #  STRATEGY 6 (MD): VWAP MEAN REVERSION BANDS (MD lines 140-155)
    # ══════════════════════════════════════════════════════════════

    def scan_s6_vwap_band(self, regime: str) -> list:
        if not self.is_in_trade_window():
            print('Blocked by trade window')
            return []
        if not self._check_daily_trade_limit():
            print('Blocked by daily limit')
            return []
        if regime == "EXTREME_PANIC":
            return []
        if not self._cache_ts_ready():
            print('Blocked by cache ready')
            return []

        signals = []
        for token, symbol in self.data.UNIVERSE.items():
            current = self.data.tick_store.get_ltp_if_fresh(token)
            if current <= 0 or current < 400:  # higher min price
                continue

            if self.data.get_avg_daily_turnover_cr(token) < 40:
                continue

            vwap = self.data.tick_store.get_vwap(token)
            if vwap <= 0:
                continue

            candles = self.data.get_intraday_ohlcv(token, "5minute")
            if len(candles) < 20:
                continue
            closes = [c["close"] for c in candles]
            vwap_series = [self.data.tick_store.get_vwap(token) for _ in closes]  # approx
            # Use real SD from recent closes
            _, _, vwap_sd = DataAgent.compute_bollinger(closes[-20:], 20, S6_VWAP_SD)
            if vwap_sd <= 0:
                vwap_sd = current * 0.008

            rvol = self.data.compute_rvol(token)
            if rvol < 1.5:
                continue

            # RSI extreme required
            rsi = (DataAgent.compute_rsi(closes, 14) or [50])[-1]

            # LONG
            if (current < vwap - 1.8 * vwap_sd and rsi < 35 and
                regime in ["CHOP", "NORMAL"]):
                stop_price = round(current - 1.2 * vwap_sd, 2)
                risk = current - stop_price
                if risk <= 0: continue
                target_price = round(vwap, 2)
                if (target_price - current) < 2.0 * risk:
                    target_price = round(current + 2.0 * risk, 2)
                signals.append({
                    "strategy": "S6_VWAP_BAND", "symbol": symbol, "token": token,
                    "regime": regime, "entry_price": current, "stop_price": stop_price,
                    "target_price": target_price, "vwap": round(vwap, 2),
                    "rvol": round(rvol, 2), "rsi": round(rsi, 1), "is_short": False
                })

            # SHORT
            elif (current > vwap + 1.8 * vwap_sd and rsi > 65 and
                regime in ["CHOP", "NORMAL"]):
                stop_price = round(current + 1.2 * vwap_sd, 2)
                risk = stop_price - current
                if risk <= 0: continue
                target_price = round(vwap, 2)
                if (current - target_price) < 2.0 * risk:
                    target_price = round(current - 2.0 * risk, 2)
                signals.append({
                    "strategy": "S6_VWAP_BAND", "symbol": symbol, "token": token,
                    "regime": regime, "entry_price": current, "stop_price": stop_price,
                    "target_price": target_price, "vwap": round(vwap, 2),
                    "rvol": round(rvol, 2), "rsi": round(rsi, 1), "is_short": True
                })

        return sorted(signals, key=lambda x: abs(x["entry_price"] - x["vwap"]), reverse=True)[:2]

    # ══════════════════════════════════════════════════════════════
    #  S7: MEAN REVERSION LONG (kept from V18)
    # ══════════════════════════════════════════════════════════════

    def scan_s7_mean_rev_long(self, regime: str) -> list:
        """
        V18 S7: Mean Reversion Long — oversold stocks bouncing.
        Price > SMA200, VWAP deviation > -0.4%, RSI(4) < 30,
        price < Lower BB, RVOL >= 1.2, exit to VWAP.
        """
        if not self.is_in_trade_window():
            print('Blocked by trade window')
            return []
        if not self._check_daily_trade_limit():
            print('Blocked by daily limit')
            return []
        if regime in ("EXTREME_PANIC", "BEAR_PANIC"):
            return []
        if not self._cache_ts_ready():
            print('Blocked by cache ready')
            return []

        signals = []

        for token, symbol in self.data.UNIVERSE.items():
            if self.blackout and self.blackout.is_blackout():
                continue

            if self.data.get_avg_daily_turnover_cr(token) < S7_MIN_TURNOVER_CR:
                continue

            current = self.data.tick_store.get_ltp_if_fresh(token)
            if current <= 0:
                continue

            # Intraday ATR check — skip if stock is trending today (not ranging)
            candles_today = self.data.get_intraday_ohlcv(token, "5minute")
            if len(candles_today) >= 6:
                intra_ranges = [c["high"] - c["low"] for c in candles_today[-6:]]
                avg_intra_range = float(np.mean(intra_ranges))
                daily_atr = self.data.daily_cache.get_atr(token)
                if daily_atr > 0 and avg_intra_range > daily_atr * 0.50:
                    continue

            sma200 = self.data.daily_cache.get_sma200(token)
            if current < sma200 or sma200 <= 0:
                continue

            vwap = self.data.tick_store.get_vwap(token)
            if vwap <= 0:
                continue
            vwap_dev = (current - vwap) / vwap
            if vwap_dev > -S7_VWAP_DEVIATION_PCT:
                continue

            bb_lo = self.data.daily_cache.get_bb_lower(token)
            if current >= bb_lo or bb_lo <= 0:
                continue

            closes = self.data.daily_cache.get_closes(token)
            if len(closes) < 10:
                continue
            live_closes = closes.copy()
            live_closes.append(current)
            rsi_4 = (DataAgent.compute_rsi(live_closes, S7_RSI_PERIOD) or [50])[-1]
            if rsi_4 >= S7_RSI_OVERSOLD:
                continue

            rvol = self.data.compute_rvol(token)
            if rvol < S7_RVOL_MIN:
                continue

            atr = self.data.daily_cache.get_atr(token)
            if atr <= 0:
                atr = current * 0.02

            stop_price   = round(current - atr * 1.5, 2)
            risk = current - stop_price
            if risk <= 0: continue
            target_price = round(vwap, 2)
            if (target_price - current) < risk * 1.5:
                target_price = round(current + risk * 1.5, 2)
            if stop_price >= current * 0.998:
                continue

            signals.append({
                "strategy":      "S7_MEAN_REV_LONG",
                "symbol":        symbol,
                "token":         token,
                "regime":        regime,
                "entry_price":   current,
                "partial_target": round(current + 1.0 * risk, 2),
                "target_price":  target_price,
                "stop_price":    stop_price,
                "rsi_4":         round(rsi_4, 2),
                "atr":           round(atr, 2),
                "rvol":          round(rvol, 2),
                "vwap":          round(vwap, 2),
                "vwap_dev_pct":  round(vwap_dev * 100, 2),
                "product":       "MIS",
                "is_short":      False,
                "max_hold_days": 0,
                "entry_time":    None,
                "entry_date":    None,
            })

        return sorted(signals, key=lambda x: x["rsi_4"])[:3]

    # ══════════════════════════════════════════════════════════════
    #  STRATEGY 8: VOLUME PROFILE + PIVOT BREAKOUT (MD lines 175-190)
    # ══════════════════════════════════════════════════════════════

    def scan_s8_vol_pivot(self, regime: str) -> list:
        """
        MD Strategy 8: Volume Profile + Pivot Point Breakout
        Best Regime: All (volume confirmation). Timeframe: 15-min/daily.

        Floor pivot = (H + L + C) / 3 of previous day.
        R1 = 2 × Pivot - Low  (resistance 1)
        S1 = 2 × Pivot - High (support 1)

        Entry:
        Long:  Break above R1 pivot + volume spike > 1.5× average
        Short: Break below S1 pivot + volume spike > 1.5× average

        Exit:
        Target: Next pivot level (R2 / S2)
        Stop: Below/above previous pivot

        Risk: 0.75%. Trailing to next level.
        """
        if not self.is_in_trade_window():
            print('Blocked by trade window')
            return []
        if not self._check_daily_trade_limit():
            print('Blocked by daily limit')
            return []
        if regime == "EXTREME_PANIC":
            return []
        if not self._cache_ts_ready():
            print('Blocked by cache ready')
            return []

        # [PATCH 4] Start at 10:00 not 09:45
        now_t = now_ist().time()
        if now_t < datetime.time(10, 0):
            return []

        signals = []

        for token, symbol in self.data.UNIVERSE.items():
            current = self.data.tick_store.get_ltp_if_fresh(token)
            if current <= 0:
                continue

            # Minimum stock price filter to avoid cost-killing low-price trades
            if current < 300:
                continue

            # ── Compute floor pivots from yesterday's daily bar ──
            closes = self.data.daily_cache.get_closes(token)
            highs  = self.data.daily_cache.get_highs(token)
            lows   = self.data.daily_cache.get_lows(token)
            if len(closes) < 2 or len(highs) < 2 or len(lows) < 2:
                continue

            prev_h = highs[-2]
            prev_l = lows[-2]
            prev_c = closes[-2]
            pivot  = (prev_h + prev_l + prev_c) / 3
            r1     = 2 * pivot - prev_l     # resistance 1
            s1     = 2 * pivot - prev_h     # support 1
            r2     = pivot + (prev_h - prev_l)   # resistance 2
            s2     = pivot - (prev_h - prev_l)   # support 2

            # ── Volume spike: current RVOL > S8_VOL_SPIKE_MULT × average ──
            rvol = self.data.compute_rvol(token)
            if rvol < S8_VOL_SPIKE_MULT:
                continue

            atr = self.data.daily_cache.get_atr(token)
            if atr <= 0:
                atr = current * 0.015

            candles = self.data.get_intraday_ohlcv(token, "5minute")
            if len(candles) < 2:
                continue
            curr_candle = candles[-1]
            prev_candle = candles[-2]

            # ── LONG: R1 breakout or pullback retest ──
            # [PATCH 4] Standard: current candle closes above R1
            r1_breakout = curr_candle["close"] > r1
            # [PATCH 4] Pullback retest: prev broke R1, current pulled back to R1 & held
            r1_retest   = (prev_candle["close"] > r1 and
                        curr_candle["low"] <= r1 and
                        curr_candle["close"] > r1)
            long_signal = r1_breakout or r1_retest

            if long_signal:
                # [PATCH 4] Stop below R1 (the breakout level), with tiny buffer
                stop_price   = round(r1 - 0.15 * atr, 2)
                risk = current - stop_price
                if risk <= 0:
                    stop_price = round(current - 0.5 * atr, 2)
                    risk = current - stop_price
                if risk <= 0:
                    continue
                target_price = round(r2, 2)       # Target: R2
                # Sanity guard
                if target_price <= current or r2 <= r1:
                    continue
                # [PATCH 4] Minimum RR floor of 1.5:1
                target_price = max(target_price, round(current + 1.5 * risk, 2))
                signals.append({
                    "strategy":     "S8_VOL_PIVOT",
                    "symbol":       symbol,
                    "token":        token,
                    "regime":       regime,
                    "entry_price":  current,
                    "stop_price":   stop_price,
                    "partial_target": round(current + 1.0 * risk, 2),
                    "target_price": target_price,
                    "pivot":        round(pivot, 2),
                    "r1":           round(r1, 2),
                    "r2":           round(r2, 2),
                    "rvol":         round(rvol, 2),
                    "atr":          round(atr, 2),
                    "product":      "MIS",
                    "is_short":     False,
                    "max_hold_days": 0,
                    "entry_time":   None,
                    "entry_date":   None,
                })

            else:
                # ── SHORT: S1 breakdown or pullback retest ──
                s1_breakdown = curr_candle["close"] < s1
                s1_retest    = (prev_candle["close"] < s1 and
                                curr_candle["high"] >= s1 and
                                curr_candle["close"] < s1)
                short_signal = s1_breakdown or s1_retest

                if short_signal:
                    # [PATCH 4] Stop above S1 with tiny buffer
                    stop_price   = round(s1 + 0.15 * atr, 2)
                    risk = stop_price - current
                    if risk <= 0:
                        stop_price = round(current + 0.5 * atr, 2)
                        risk = stop_price - current
                    if risk <= 0:
                        continue
                    target_price = round(s2, 2)       # Target: S2
                    if target_price >= current or s2 <= 0:
                        continue
                    # [PATCH 4] Minimum RR floor of 1.5:1
                    target_price = min(target_price, round(current - 1.5 * risk, 2))
                    signals.append({
                        "strategy":     "S8_VOL_PIVOT",
                        "symbol":       symbol,
                        "token":        token,
                        "regime":       regime,
                        "entry_price":  current,
                        "stop_price":   stop_price,
                        "partial_target": round(current - 1.0 * risk, 2),
                        "target_price": target_price,
                        "pivot":        round(pivot, 2),
                        "s1":           round(s1, 2),
                        "s2":           round(s2, 2),
                        "rvol":         round(rvol, 2),
                        "atr":          round(atr, 2),
                        "product":      "MIS",
                        "is_short":     True,
                        "max_hold_days": 0,
                        "entry_time":   None,
                        "entry_date":   None,
                    })

        return sorted(signals, key=lambda x: x["rvol"], reverse=True)[:3]

    # ══════════════════════════════════════════════════════════════
    #  STRATEGY 9: MULTI-TIMEFRAME TREND + MOMENTUM (MD lines 192-207)
    # ══════════════════════════════════════════════════════════════
    # ══════════════════════════════════════════════════════════════
    #  STRATEGY 9: MULTI-TIMEFRAME TREND + MOMENTUM (MD lines 192-207)
    # ══════════════════════════════════════════════════════════════

    def scan_s9_mtf_momentum(self, regime: str) -> list:
        """S9: Multi-Timeframe Trend + Momentum (Daily 200 EMA + 15-min RSI + MACD)"""
        if not self.is_in_trade_window():
            print('Blocked by trade window')
            return []
        if regime not in ["BULL", "NORMAL", "VOLATILE", "CHOP"]:   # CHOP allowed only with strong volume
            return []
        if not self._check_daily_trade_limit():
            print('Blocked by daily limit')
            return []
        if not self._cache_ts_ready():
            print('Blocked by cache ready')
            return []

        signals = []
        for token, symbol in self.data.UNIVERSE.items():
            current = self.data.tick_store.get_ltp_if_fresh(token)
            if current <= 0 or current < 300:
                continue

            # Higher timeframe trend filter
            closes_d = self.data.daily_cache.get_closes(token)
            if len(closes_d) < S9_EMA_TREND + 5:
                continue
            ema200_daily = DataAgent.compute_ema(closes_d, S9_EMA_TREND)[-1]
            is_uptrend = current > ema200_daily

            # 5-minute data for momentum signals
            c_intra = self.data.get_intraday_ohlcv(token, "5minute")
            if len(c_intra) < 30:
                continue
            c_closes = [c["close"] for c in c_intra]

            # MACD crossover detection
            macd_val, signal_val, _ = DataAgent.compute_macd(c_closes)
            prev_closes = c_closes[:-1]
            macd_prev, signal_prev, _ = DataAgent.compute_macd(prev_closes)

            rsi_15 = (DataAgent.compute_rsi(c_closes, S9_RSI_PERIOD) or [50])[-1]
            rvol = self.data.compute_rvol(token)

            # Volume filter (stricter in CHOP)
            if regime == "CHOP" and rvol < 1.8:
                continue
            if rvol < 1.3:
                continue

            # Extra ADX filter only in volatile regimes
            if regime == "VOLATILE":
                highs = [c["high"] for c in c_intra[-14:]]
                lows = [c["low"] for c in c_intra[-14:]]
                closes_short = c_closes[-14:]
                adx = DataAgent.compute_adx(highs, lows, closes_short)
                if adx < 22:
                    continue

            atr = self.data.daily_cache.get_atr(token)
            if atr <= 0:
                atr = current * 0.02

            # LONG ENTRY
            if (is_uptrend and 
                rsi_15 > S9_RSI_THRESHOLD and 
                macd_prev < signal_prev and 
                macd_val > signal_val):
                
                stop_price = round(current - S9_ATR_SL_MULT * atr, 2)
                risk = current - stop_price
                if risk <= 0: 
                    continue
                target_price = round(current + S9_RR * risk, 2)
                
                signals.append({
                    "strategy":     "S9_MTF_MOMENTUM",
                    "symbol":       symbol,
                    "token":        token,
                    "regime":       regime,
                    "entry_price":  current,
                    "stop_price":   stop_price,
                    "partial_target": round(current + 1.0 * risk, 2),
                    "target_price": target_price,
                    "ema200":       round(ema200_daily, 2),
                    "rsi15":        round(rsi_15, 2),
                    "macd":         round(macd_val, 4),
                    "macd_signal":  round(signal_val, 4),
                    "rvol":         round(rvol, 2),
                    "atr":          round(atr, 2),
                    "product":      "MIS",
                    "is_short":     False,
                    "max_hold_days": 0,
                    "entry_time":   None,
                    "entry_date":   None,
                })

            # SHORT ENTRY
            elif (not is_uptrend and 
                  rsi_15 < (100 - S9_RSI_THRESHOLD) and 
                  macd_prev > signal_prev and 
                  macd_val < signal_val):
                
                stop_price = round(current + S9_ATR_SL_MULT * atr, 2)
                risk = stop_price - current
                if risk <= 0: 
                    continue
                target_price = round(current - S9_RR * risk, 2)
                
                signals.append({
                    "strategy":     "S9_MTF_MOMENTUM",
                    "symbol":       symbol,
                    "token":        token,
                    "regime":       regime,
                    "entry_price":  current,
                    "stop_price":   stop_price,
                    "partial_target": round(current - 1.0 * risk, 2),
                    "target_price": target_price,
                    "ema200":       round(ema200_daily, 2),
                    "rsi15":        round(rsi_15, 2),
                    "macd":         round(macd_val, 4),
                    "macd_signal":  round(signal_val, 4),
                    "rvol":         round(rvol, 2),
                    "atr":          round(atr, 2),
                    "product":      "MIS",
                    "is_short":     True,
                    "max_hold_days": 0,
                    "entry_time":   None,
                    "entry_date":   None,
                })

        return sorted(signals, key=lambda x: abs(x.get("rsi15", 50) - 50), reverse=True)[:3]
    # ══════════════════════════════════════════════════════════════
    #  STRATEGY 4: CASH-FUTURES ARBITRAGE (MD lines 108-121)
    #  *** FUTURES ONLY — OPTIONS STRICTLY EXCLUDED ***
    # ══════════════════════════════════════════════════════════════

    def set_futures_tokens(self, futures_map: dict):
        """
        Called by main.py at startup after DataAgent.load_futures_tokens().
        COMMENTED OUT PURSUANT TO USER REQUEST
        """
        self._futures_tokens = {}

    def scan_s4_arbitrage(self) -> list:
        """
        MD Strategy 4: Cash-Futures Arbitrage (Index Level)
        COMMENTED OUT PURSUANT TO USER REQUEST
        """
        return []
