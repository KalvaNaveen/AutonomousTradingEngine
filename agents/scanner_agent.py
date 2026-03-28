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
            above_ema = nifty_ltp > ema_25 if nifty_ltp > 0 else False
        else:
            hist = self.data.get_daily_ohlcv(NIFTY50_TOKEN, days=60)
            if len(hist) < 30:
                return "CHOP"
            closes    = [d["close"] for d in hist]
            ema_25    = DataAgent.compute_ema(closes, 25)[-1]
            nifty_ltp = closes[-1]
            above_ema = closes[-1] > ema_25

        if vix > VIX_BEAR_PANIC and not above_ema and ad_ratio < 0.40:
            print(f"[Regime] BEAR_PANIC — VIX={vix:.1f} AD={ad_ratio:.2f}")
            return "BEAR_PANIC"
        if vix < VIX_BULL_MAX and above_ema and ad_ratio > 0.60:
            print(f"[Regime] BULL — VIX={vix:.1f} AD={ad_ratio:.2f}")
            return "BULL"
        if VIX_NORMAL_LOW <= vix <= VIX_NORMAL_HIGH:
            print(f"[Regime] NORMAL — VIX={vix:.1f} AD={ad_ratio:.2f}")
            return "NORMAL"
        if vix > VIX_BULL_MAX and vix < VIX_BEAR_PANIC:
            print(f"[Regime] VOLATILE — VIX={vix:.1f} AD={ad_ratio:.2f}")
            return "VOLATILE"

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
            return []
        if not self._check_daily_trade_limit():
            return []
        if regime == "EXTREME_PANIC":
            return []
        if not self._cache_ts_ready():
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

            # ── ADX filter: must be > 25 (MD: no trade if ADX < 25) ──
            candles = self.data.get_intraday_ohlcv(token, "15minute")
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

            if cross_up and is_above_200:
                # Long: 9 EMA crosses above 21 EMA AND price > 200 EMA
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

            elif cross_down and not is_above_200:
                # Short: 9 EMA crosses below 21 EMA AND price < 200 EMA
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
            return []
        if not self._check_daily_trade_limit():
            return []
        if regime == "EXTREME_PANIC":
            return []
        if not self._cache_ts_ready():
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
                if daily_atr > 0 and avg_intra_range > daily_atr * 0.50:
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

            # ── ATR stop ──
            atr = self.data.daily_cache.get_atr(token)
            if atr <= 0:
                atr = current * 0.015

            if current <= bb_lo and rsi_val < S2_RSI_OVERSOLD and current > vwap:
                # Long: price touches lower BB AND RSI < 30 AND price > VWAP
                stop_price   = round(current - S2_ATR_SL_MULT * atr, 2)
                target_price = round(bb_mid, 2)   # revert to middle BB
                if target_price <= current * 1.001:
                    target_price = round(current + S2_RR * (current - stop_price), 2)
                signals.append({
                    "strategy":     "S2_BB_MEAN_REV",
                    "symbol":       symbol,
                    "token":        token,
                    "regime":       regime,
                    "entry_price":  current,
                    "stop_price":   stop_price,
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

            elif current >= bb_hi and rsi_val > S2_RSI_OVERBOUGHT and current < vwap:
                # Short: price touches upper BB AND RSI > 70 AND price < VWAP
                stop_price   = round(current + S2_ATR_SL_MULT * atr, 2)
                target_price = round(bb_mid, 2)
                if target_price >= current * 0.999:
                    target_price = round(current - S2_RR * (stop_price - current), 2)
                signals.append({
                    "strategy":     "S2_BB_MEAN_REV",
                    "symbol":       symbol,
                    "token":        token,
                    "regime":       regime,
                    "entry_price":  current,
                    "stop_price":   stop_price,
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

    def scan_s3_orb(self) -> list:
        """
        MD Strategy 3: Opening Range Breakout (ORB)
        Best Regime: Volatile/trending days. Timeframe: 15-min.

        Mark High/Low of 9:15-9:30 AM candle (TickStore tracks this).
        Long:  First 15-min candle closes above range High + volume > average
        Short: First 15-min candle closes below range Low + volume > average

        Entry window: 9:30 AM - 2:00 PM only.
        Stop: Opposite side of range.
        Target: 1.5× range size (MD: 1–2× preferred).
        Trailing: Pivot-based after 1:1 RR.
        Mandatory exit by 3:20 PM.

        Risk: 0.75% max. Max 2 trades/day. Skip Thursdays (expiry).
        """
        now = now_ist()
        t   = now.time()

        # Entry window: 9:30 AM - S3_ENTRY_END
        eh, em = map(int, S3_ENTRY_END.split(':'))
        if not (datetime.time(9, 30) <= t <= datetime.time(eh, em)):
            return []

        # Skip expiry days robustly (Wed/Thu checks + blackout integration)
        if self.blackout and self.blackout.is_blackout():
            return []
        if now.weekday() in (3, 4):  # Broad expiry window filter
            return []

        # Max 2 trades/day (MD rule, line 103)
        _today = now.date()
        if self._s3_trade_date != _today:
            self._s3_trades_today = 0
            self._s3_trade_date = _today
        if self._s3_trades_today >= S3_MAX_TRADES:
            return []

        if not self._cache_ts_ready():
            return []

        signals = []

        for token, symbol in self.data.UNIVERSE.items():
            if self.blackout and self.blackout.is_blackout():
                continue

            # ── ORB from TickStore (locked after 9:30) ──
            orb = self.data.tick_store.get_orb(token)
            orb_high = orb.get("orb_high", 0)
            orb_low  = orb.get("orb_low", 999999)
            orb_locked = orb.get("orb_locked", False)

            if not orb_locked or orb_high <= 0 or orb_low >= 999999:
                continue    # ORB not yet locked or no data

            orb_range = orb_high - orb_low
            if orb_range <= 0:
                continue

            current  = self.data.tick_store.get_ltp_if_fresh(token)
            if current <= 0:
                continue

            # ── Volume confirmation: current RVOL must beat average ──
            rvol = self.data.compute_rvol(token)
            if rvol < 1.0:         # Must exceed average volume
                continue

            atr = self.data.daily_cache.get_atr(token)
            if atr <= 0:
                atr = orb_range

            # ── Long breakout: price breaks above ORB high ──
            if current > orb_high:
                # Stop: ATR below entry (tight, just under breakout candle)
                # NOT orb_low — that would make risk = full range + gap, killing RR
                stop_price   = round(current - max(atr, orb_range * 0.5), 2)
                risk         = current - stop_price
                if risk <= 0:
                    continue
                target_price = round(current + S3_TARGET_MULT * 2 * risk, 2)  # 3R target
                # Sanity: target must be meaningfully above ORB high
                if target_price <= orb_high:
                    target_price = round(orb_high + S3_TARGET_MULT * orb_range, 2)
                signals.append({
                    "strategy":     "S3_ORB",
                    "symbol":       symbol,
                    "token":        token,
                    "regime":       "ANY",
                    "entry_price":  current,
                    "stop_price":   stop_price,
                    "target_price": target_price,
                    "orb_high":     round(orb_high, 2),
                    "orb_low":      round(orb_low, 2),
                    "orb_range":    round(orb_range, 2),
                    "rvol":         round(rvol, 2),
                    "product":      "MIS",
                    "is_short":     False,
                    "exit_by":      S3_EXIT_TIME,
                    "entry_time":   None,
                    "entry_date":   None,
                })

            # ── Short breakout: price breaks below ORB low ──
            elif current < orb_low:
                # Stop: ATR above entry (tight, just above breakout point)
                stop_price   = round(current + max(atr, orb_range * 0.5), 2)
                risk         = stop_price - current
                if risk <= 0:
                    continue
                target_price = round(current - S3_TARGET_MULT * 2 * risk, 2)  # 3R target
                if target_price >= orb_low:
                    target_price = round(orb_low - S3_TARGET_MULT * orb_range, 2)
                signals.append({
                    "strategy":     "S3_ORB",
                    "symbol":       symbol,
                    "token":        token,
                    "regime":       "ANY",
                    "entry_price":  current,
                    "stop_price":   stop_price,
                    "target_price": target_price,
                    "orb_high":     round(orb_high, 2),
                    "orb_low":      round(orb_low, 2),
                    "orb_range":    round(orb_range, 2),
                    "rvol":         round(rvol, 2),
                    "product":      "MIS",
                    "is_short":     True,
                    "exit_by":      S3_EXIT_TIME,
                    "entry_time":   None,
                    "entry_date":   None,
                })

        return sorted(signals, key=lambda x: x["rvol"], reverse=True)[:2]

    def register_s3_trade(self):
        """Call after an S3 ORB trade executes (tracks 2/day limit)."""
        today = now_ist().date()
        if self._s3_trade_date != today:
            self._s3_trades_today = 0
            self._s3_trade_date = today
        self._s3_trades_today += 1

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
            return []
        if not self._check_daily_trade_limit():
            return []
        if regime in ("BULL", "EXTREME_PANIC"):
            return []
        if not self._cache_ts_ready():
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

            stop_price  = round(current + atr * 1.0, 2)
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
        """
        MD Strategy 6: VWAP Mean Reversion
        Best Regime: Intraday any regime. Timeframe: 5-min.

        Entry:
          Long:  Price < VWAP - 1.5 SD AND stock in uptrend (price > SMA200)
          Short: Price > VWAP + 1.5 SD AND stock in downtrend (price < SMA200)

        Exit:
          Target: VWAP or 1:2 RR
          Stop: 1 ATR

        Risk: 0.5%
        """
        if not self.is_in_trade_window():
            return []
        if not self._check_daily_trade_limit():
            return []
        if regime == "EXTREME_PANIC":
            return []
        if not self._cache_ts_ready():
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
                if daily_atr > 0 and avg_intra_range > daily_atr * 0.50:
                    continue

            vwap = self.data.tick_store.get_vwap(token)
            if vwap <= 0:
                continue

            # ── Compute VWAP standard deviation from 5-min candle closes ──
            candles = self.data.get_intraday_ohlcv(token, "5minute")
            if len(candles) < 10:
                # Not enough intraday data for SD
                continue
            intra_closes = [c["close"] for c in candles]
            vwap_sd = float(np.std([c - vwap for c in intra_closes])) if len(intra_closes) >= 5 else 0
            if vwap_sd <= 0:
                continue

            vwap_upper = vwap + S6_VWAP_SD * vwap_sd
            vwap_lower = vwap - S6_VWAP_SD * vwap_sd

            # ── Higher TF trend filter ──
            sma200 = self.data.daily_cache.get_sma200(token)
            if sma200 <= 0:
                continue

            atr = self.data.daily_cache.get_atr(token)
            if atr <= 0:
                atr = current * 0.015

            if current < vwap_lower and current > sma200:
                # Long: price < VWAP - 1.5 SD in uptrend
                stop_price   = round(current - atr, 2)
                target_price = round(vwap, 2)
                risk = current - stop_price
                if risk <= 0:
                    continue
                # Ensure minimum RR: if VWAP-to-entry gap < RR*risk, use RR target
                if (target_price - current) < S6_VWAP_RR * risk:
                    target_price = round(current + S6_VWAP_RR * risk, 2)
                signals.append({
                    "strategy":     "S6_VWAP_BAND",
                    "symbol":       symbol,
                    "token":        token,
                    "regime":       regime,
                    "entry_price":  current,
                    "stop_price":   stop_price,
                    "target_price": target_price,
                    "vwap":         round(vwap, 2),
                    "vwap_lower":   round(vwap_lower, 2),
                    "vwap_sd":      round(vwap_sd, 2),
                    "atr":          round(atr, 2),
                    "product":      "MIS",
                    "is_short":     False,
                    "max_hold_days": 0,
                    "entry_time":   None,
                    "entry_date":   None,
                })

            elif current > vwap_upper and current < sma200:
                # Short: price > VWAP + 1.5 SD in downtrend
                stop_price   = round(current + atr, 2)
                target_price = round(vwap, 2)
                risk = stop_price - current
                if risk <= 0:
                    continue
                # Ensure minimum RR: if entry-to-VWAP gap < RR*risk, use RR target
                if (current - target_price) < S6_VWAP_RR * risk:
                    target_price = round(current - S6_VWAP_RR * risk, 2)
                signals.append({
                    "strategy":     "S6_VWAP_BAND",
                    "symbol":       symbol,
                    "token":        token,
                    "regime":       regime,
                    "entry_price":  current,
                    "stop_price":   stop_price,
                    "target_price": target_price,
                    "vwap":         round(vwap, 2),
                    "vwap_upper":   round(vwap_upper, 2),
                    "vwap_sd":      round(vwap_sd, 2),
                    "atr":          round(atr, 2),
                    "product":      "MIS",
                    "is_short":     True,
                    "max_hold_days": 0,
                    "entry_time":   None,
                    "entry_date":   None,
                })

        # Sort by deviation from VWAP (deepest extremes first)
        return sorted(signals,
                      key=lambda x: abs(x["entry_price"] - x["vwap"]),
                      reverse=True)[:3]

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
            return []
        if not self._check_daily_trade_limit():
            return []
        if regime in ("EXTREME_PANIC", "BEAR_PANIC"):
            return []
        if not self._cache_ts_ready():
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
            return []
        if not self._check_daily_trade_limit():
            return []
        if regime == "EXTREME_PANIC":
            return []
        if not self._cache_ts_ready():
            return []

        signals = []

        for token, symbol in self.data.UNIVERSE.items():
            current = self.data.tick_store.get_ltp_if_fresh(token)
            if current <= 0:
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

            if current > r1:
                # Long: break above R1 + volume spike
                stop_price   = round(pivot, 2)   # SL: back below pivot
                target_price = round(r2, 2)       # Target: R2
                # Sanity guard: r2 must be above current (stock hasn't already blown past it)
                if target_price <= current or r2 <= r1:
                    continue
                risk = current - stop_price
                if risk <= 0:
                    continue
                signals.append({
                    "strategy":     "S8_VOL_PIVOT",
                    "symbol":       symbol,
                    "token":        token,
                    "regime":       regime,
                    "entry_price":  current,
                    "stop_price":   stop_price,
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

            elif current < s1:
                # Short: break below S1 + volume spike
                stop_price   = round(pivot, 2)   # SL: back above pivot
                target_price = round(s2, 2)       # Target: S2
                # Sanity guard: s2 must be below entry (bad data can invert this)
                if target_price >= current or s2 <= 0:
                    continue
                risk = stop_price - current
                if risk <= 0:
                    continue
                signals.append({
                    "strategy":     "S8_VOL_PIVOT",
                    "symbol":       symbol,
                    "token":        token,
                    "regime":       regime,
                    "entry_price":  current,
                    "stop_price":   stop_price,
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

    def scan_s9_mtf_momentum(self, regime: str) -> list:
        """
        MD Strategy 9: Multi-Timeframe Trend + Momentum Filter
        Best Regime: Bull/bear confirmation. Timeframe: Daily + 15-min.

        Entry:
          Higher TF: Price > 200 EMA (uptrend) or < (downtrend)
          Lower TF:  RSI > 50 + MACD crossover in trend direction

        Exit:
          Target: 1:3 RR
          Stop: 2 × ATR

        Risk: Dynamic sizing based on trend strength (ADX proxy).
        """
        if not self.is_in_trade_window():
            return []
        if not self._check_daily_trade_limit():
            return []
        if regime == "EXTREME_PANIC":
            return []
        if not self._cache_ts_ready():
            return []

        # S9 needs enough 15-min bars for MACD — only valid after 11:30
        now_t = now_ist().time()
        if now_t < datetime.time(13, 15):  # Only in Window 2
            return []

        signals = []

        for token, symbol in self.data.UNIVERSE.items():
            current = self.data.tick_store.get_ltp_if_fresh(token)
            if current <= 0:
                continue

            # ── Higher TF: Daily 200 EMA trend filter ──
            closes_d = self.data.daily_cache.get_closes(token)
            if len(closes_d) < S9_EMA_TREND + 5:
                continue

            ema200_daily = DataAgent.compute_ema(closes_d, S9_EMA_TREND)[-1]
            is_uptrend   = current > ema200_daily

            # ── Lower TF: 15-min RSI > 50 + MACD crossover ──
            c15 = self._get_15min_closes(token)
            if len(c15) < 35:     # Need enough bars for MACD(12,26,9)
                continue

            rsi_15 = (DataAgent.compute_rsi(c15, S9_RSI_PERIOD) or [50])[-1]
            macd, signal_line, histogram = DataAgent.compute_macd(c15)

            # Need previous bar for crossover detection
            if len(c15) < 36:
                continue
            macd_prev, sig_prev, _ = DataAgent.compute_macd(c15[:-1])
            macd_cross_up   = macd_prev <= sig_prev and macd > signal_line
            macd_cross_down = macd_prev >= sig_prev and macd < signal_line

            atr = self.data.daily_cache.get_atr(token)
            if atr <= 0:
                atr = current * 0.02

            if (is_uptrend and
                    rsi_15 > S9_RSI_THRESHOLD and    # RSI > 50 (bullish momentum)
                    macd_cross_up):                   # MACD bullish crossover
                # Long: uptrend + RSI > 50 + MACD crossover up
                stop_price   = round(current - S9_ATR_SL_MULT * atr, 2)
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
                    "target_price": target_price,
                    "ema200":       round(ema200_daily, 2),
                    "rsi15":        round(rsi_15, 2),
                    "macd":         macd,
                    "macd_signal":  signal_line,
                    "atr":          round(atr, 2),
                    "product":      "MIS",
                    "is_short":     False,
                    "max_hold_days": 0,
                    "entry_time":   None,
                    "entry_date":   None,
                })

            elif (not is_uptrend and
                    rsi_15 < (100 - S9_RSI_THRESHOLD) and   # RSI < 50 (bearish)
                    macd_cross_down):                         # MACD bearish crossover
                # Short: downtrend + RSI < 50 + MACD crossover down
                stop_price   = round(current + S9_ATR_SL_MULT * atr, 2)
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
                    "target_price": target_price,
                    "ema200":       round(ema200_daily, 2),
                    "rsi15":        round(rsi_15, 2),
                    "macd":         macd,
                    "macd_signal":  signal_line,
                    "atr":          round(atr, 2),
                    "product":      "MIS",
                    "is_short":     True,
                    "max_hold_days": 0,
                    "entry_time":   None,
                    "entry_date":   None,
                })

        return sorted(signals, key=lambda x: abs(x["rsi15"] - 50), reverse=True)[:3]

    # ══════════════════════════════════════════════════════════════
    #  STRATEGY 4: CASH-FUTURES ARBITRAGE (MD lines 108-121)
    #  *** FUTURES ONLY — OPTIONS STRICTLY EXCLUDED ***
    # ══════════════════════════════════════════════════════════════

    def set_futures_tokens(self, futures_map: dict):
        """
        Called by main.py at startup after DataAgent.load_futures_tokens().
        futures_map format: {
            "NIFTY":     {"token": int, "symbol": str, "expiry": date},
            "BANKNIFTY": {"token": int, "symbol": str, "expiry": date},
        }
        STRICT POLICY: Only call with FUT instrument types.
        Never pass CE/PE option tokens here.
        """
        # Safety filter: DataAgent already filters instrument_type == "FUT",
        # but we double-check symbol suffix as an extra guard.
        safe = {}
        for name, info in futures_map.items():
            sym = info.get("symbol", "")
            if sym.endswith("FUT"):
                safe[name] = info
            else:
                print(f"[Scanner] S4 REJECTED non-futures token: {sym} "
                      f"(only 'FUT' suffix allowed — no options)")
        self._futures_tokens = safe
        if safe:
            print(f"[Scanner] S4 futures registered: "
                  f"{[v['symbol'] for v in safe.values()]}")

    def scan_s4_arbitrage(self) -> list:
        """
        MD Strategy 4: Cash-Futures Arbitrage (Index Level)
        Best Regime: Any (near-zero risk, hedged). Timeframe: Tick/1-min.

        Instruments: NIFTY FUT / BANKNIFTY FUT vs their underlying spot index.
        *** STRICTLY FUTURES ONLY — NO OPTIONS, NO STOCK FUTURES ***

        Fair value formula:
          FV = Spot × (1 + r × T/365)
          r  = S4_RISK_FREE_RATE (6.5% RBI repo approx)
          T  = calendar days to expiry

        Entry Rule (MD line 115):
          If Futures > FV × (1 + S4_MISPRINT_ENTRY_PCT):
            → Sell futures (overpriced) — futures leg only.
              The "short cash" leg = hold / do not buy spot.
          If Futures < FV × (1 - S4_MISPRINT_ENTRY_PCT):
            → Buy futures (underpriced) — futures leg only.
              The "short cash" leg = NIFTY ETF (NIFTYBEES) if available.

        Exit Rule (MD line 117):
          Convergence when | Futures - FV | < S4_MISPRINT_EXIT_PCT × Spot
          OR max 30-min hold.

        Risk (MD line 118): 2% capital exposure per leg (near-zero net risk).
        Win Rate: 70-90% (small edges, MD line 120).

        Note on two-leg execution:
          The signal dict contains 'is_two_leg': True flag.
          ExecutionAgent places 'futures_leg' and 'spot_leg' separately.
          If the spot leg is not supported (no ETF token), only futures leg fires.

        Simulator mode:
          When _futures_tokens is empty (no live WebSocket for NFO),
          returns [] instead of generating fake signals.
          Real backtest data for futures is not in the local SQLite DB,
          so S4 is skipped during historical simulation (no false signals).
          To backtest S4, add Nifty futures 1-min data to historical.db.
        """
        # Guard: need live futures tokens loaded (not available in simulator)
        if not self._futures_tokens:
            return []

        if not self.is_in_trade_window():
            return []

        if not self.data.tick_store or not self.data.tick_store.is_fresh():
            return []

        signals = []
        import datetime as _dt

        for name, finfo in self._futures_tokens.items():
            fut_token  = finfo["token"]
            fut_symbol = finfo["symbol"]   # e.g. "NIFTY26APR FUT"
            expiry     = finfo["expiry"]   # datetime.date

            # ── 1. Get spot index price ──────────────────────────
            if name == "NIFTY":
                spot_token = S4_SPOT_TOKEN          # NIFTY50 index (256265)
            elif name == "BANKNIFTY":
                spot_token = BANKNIFTY_SPOT_TOKEN   # NIFTY BANK index (260105)
            else:
                continue

            spot = self.data.tick_store.get_ltp_if_fresh(spot_token)
            if spot <= 0:
                continue

            # ── 2. Get futures price (live tick from WebSocket) ──
            fut_price = self.data.tick_store.get_ltp_if_fresh(fut_token)
            if fut_price <= 0:
                continue   # Futures not ticking — no signal

            # ── 3. Fair value = Spot × (1 + r × T/365) ──────────
            today = now_ist().date()
            days_to_expiry = max(1, (expiry - today).days)
            r  = S4_RISK_FREE_RATE                  # 6.5% approx
            fv = spot * (1 + r * days_to_expiry / 365)

            # ── 4. Compute raw mispricing ─────────────────────────
            diff_pct = (fut_price - fv) / spot      # positive = futures rich

            # ── 5. Entry threshold: > 0.15% mispricing (MD line 115) ──
            if abs(diff_pct) < S4_MISPRINT_ENTRY_PCT:
                continue   # Spread too small — not worth entering

            # ── 6. ATR-equivalent: use spot range as stop proxy ──
            # For index futures, stop = half the remaining mispricing
            convergence_target = spot * S4_MISPRINT_EXIT_PCT

            if diff_pct > 0:
                # Futures overpriced → SHORT futures, buy spot (if available)
                # Entry: sell futures at fut_price
                # Target: when futures falls back to FV + exit_pct
                target_price  = round(fv + convergence_target, 2)
                stop_price    = round(fut_price * (1 + S4_MISPRINT_ENTRY_PCT), 2)
                signals.append({
                    "strategy":       "S4_ARBITRAGE",
                    "symbol":         fut_symbol,           # e.g. "NIFTY26APR FUT"
                    "token":          fut_token,
                    "regime":         "ANY",
                    "entry_price":    round(fut_price, 2),
                    "stop_price":     stop_price,
                    "target_price":   target_price,
                    "fair_value":     round(fv, 2),
                    "spot_price":     round(spot, 2),
                    "diff_pct":       round(diff_pct * 100, 4),  # e.g. 0.22%
                    "days_to_expiry": days_to_expiry,
                    "exchange":       "NFO",               # futures on NFO exchange
                    "product":        "MIS",
                    "is_short":       True,                # Short futures leg
                    "is_two_leg":     True,                # Signal engine to also hedge spot
                    "spot_leg": {
                        "token":      spot_token,
                        "symbol":     f"{name}_SPOT",
                        "direction":  "BUY",               # Buy spot to hedge
                        "exchange":   "NSE",
                    },
                    "max_hold_mins":  S4_MAX_HOLD_MINS,
                    "entry_time":     None,
                    "entry_date":     None,
                })

            else:
                # Futures underpriced → BUY futures, short spot (if available)
                # Entry: buy futures at fut_price
                target_price  = round(fv - convergence_target, 2)
                stop_price    = round(fut_price * (1 - S4_MISPRINT_ENTRY_PCT), 2)
                signals.append({
                    "strategy":       "S4_ARBITRAGE",
                    "symbol":         fut_symbol,
                    "token":          fut_token,
                    "regime":         "ANY",
                    "entry_price":    round(fut_price, 2),
                    "stop_price":     stop_price,
                    "target_price":   target_price,
                    "fair_value":     round(fv, 2),
                    "spot_price":     round(spot, 2),
                    "diff_pct":       round(diff_pct * 100, 4),
                    "days_to_expiry": days_to_expiry,
                    "exchange":       "NFO",
                    "product":        "MIS",
                    "is_short":       False,               # Buy futures leg
                    "is_two_leg":     True,
                    "spot_leg": {
                        "token":      spot_token,
                        "symbol":     f"{name}_SPOT",
                        "direction":  "SELL",              # Short spot to hedge
                        "exchange":   "NSE",
                    },
                    "max_hold_mins":  S4_MAX_HOLD_MINS,
                    "entry_time":     None,
                    "entry_date":     None,
                })

        # Return strongest mispricing first
        return sorted(signals, key=lambda x: abs(x["diff_pct"]), reverse=True)[:2]
