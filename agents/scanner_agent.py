"""
ScannerAgent v10 — all intraday data reads from tick_store and daily_cache.
No REST calls during scan loops.

detect_regime()         → tick_store (VIX LTP, AD ratio) + daily_cache (Nifty EMA)
scan_s1_ema_divergence() → daily_cache (history/indicators) + tick_store (current price)
scan_s2_overreaction()  → tick_store only (day_open, LTP, volume, depth, candles)
[V16] scan_s3_sepa()     → daily_cache + fundamental_agent + stage_agent + vcp_agent
[V16] scan_s4_leadership() → daily_cache + tick_store (RS + 52w high breakout)
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
        # [V16] Intelligence layer & Minervini agents
        self._fundamental = fundamental_agent
        self._stage       = stage_agent
        self._vcp         = vcp_agent
        self._mkt_status  = market_status_agent
        self._sector_agent = sector_agent

    def detect_regime(self) -> str:
        """
        tick_store: VIX LTP + AD ratio (zero REST calls if ready).
        daily_cache: Nifty EMA-25 (pre-computed at 8:45 AM).
        REST fallback used pre-market.

        Returns "EXTREME_PANIC" if VIX >= VIX_EXTREME_STOP (30).
        Callers must block all new entries on EXTREME_PANIC.
        Open positions are still monitored and exited normally.
        """
        vix = self.data.get_india_vix()

        # Hard block — checked before anything else
        if vix >= VIX_EXTREME_STOP:
            return "EXTREME_PANIC"
        ad_ratio = self.data.get_advance_decline_ratio()

        # Nifty position relative to EMA-25
        if (self.data.daily_cache and
                self.data.daily_cache.is_loaded()):
            ema_25    = self.data.daily_cache.get_ema25(NIFTY50_TOKEN)
            ts        = self.data.tick_store
            nifty_ltp = (ts.get_ltp_if_fresh(NIFTY50_TOKEN)
                         if ts and ts.is_fresh() else 0.0)
            above_ema = nifty_ltp > ema_25 if nifty_ltp > 0 else False
        else:
            # REST fallback — used pre-market before cache loaded
            hist = self.data.get_daily_ohlcv(NIFTY50_TOKEN, days=60)
            if len(hist) < 30:
                print(f"[Regime] CHOP — insufficient Nifty history ({len(hist)} bars)")
                return "CHOP"
            closes    = [d["close"] for d in hist]
            ema_25    = DataAgent.compute_ema(closes, 25)[-1]
            nifty_ltp = closes[-1]
            above_ema = closes[-1] > ema_25

        if vix > VIX_BEAR_PANIC and not above_ema and ad_ratio < 0.40:
            print(f"[Regime] BEAR_PANIC — VIX={vix:.1f} above_ema={above_ema} AD={ad_ratio:.2f}")
            return "BEAR_PANIC"
        # [v16 FIX] Check BULL before NORMAL — BULL requires VIX<18 + above_ema + AD>0.60
        # Previously NORMAL (VIX 12-22) caught this range first, making BULL unreachable.
        if vix < VIX_BULL_MAX and above_ema and ad_ratio > 0.60:
            print(f"[Regime] BULL — VIX={vix:.1f} above_ema={above_ema} AD={ad_ratio:.2f}")
            return "BULL"
        if VIX_NORMAL_LOW <= vix <= VIX_NORMAL_HIGH:
            print(f"[Regime] NORMAL — VIX={vix:.1f} above_ema={above_ema} AD={ad_ratio:.2f}")
            return "NORMAL"
        print(f"[Regime] CHOP — VIX={vix:.1f} above_ema={above_ema} AD={ad_ratio:.2f} nifty_ltp={nifty_ltp:.1f}")
        return "CHOP"

    def get_s1_min_deviation(self, regime: str) -> float:
        return {
            "BEAR_PANIC": S1_DEVIATION_MIN,
            "NORMAL":     S1_DEVIATION_NORMAL,
            "BULL":       S1_DEVIATION_BULL,
            "CHOP":       S1_DEVIATION_MAX,   # Strictest — only deep deviations
        }.get(regime, S1_DEVIATION_MAX)

    def is_valid_trading_time(self) -> tuple:
        now = now_ist()
        t   = now.time()
        if self.blackout.is_blackout(now.date()):
            return False, "BLACKOUT_DATE"
        if now.weekday() >= 5:
            return False, "WEEKEND"
        if t < datetime.time(9, 30):
            return False, "BEFORE_HUNT_WINDOW"
        if t >= datetime.time(15, 0):
            return False, "AFTER_LAST_ENTRY"
        # VIX hard block — checked last (requires a data call)
        vix = self.data.get_india_vix()
        if vix >= VIX_EXTREME_STOP:
            return False, f"EXTREME_PANIC_VIX_{vix:.1f}"
        return True, "VALID"

    def scan_s1_ema_divergence(self, regime: str) -> list:
        """
        [V16] Connors RSI(4) Pullback System — replaces old EMA divergence.
        High probability mean reversion (65-80% historic win rate).
        Rule 1: Price > 200 SMA (Primary Uptrend)
        Rule 2: Close < Lower Bollinger Band (20, 2) (Deep oversold)
        Rule 3: RSI(4) < 30 (Short-term panic)
        """
        now = now_ist().time()
        # Scan active primarily from 10:00 to 15:15 to allow intraday dips to form
        if not (datetime.time(10, 0) <= now <= datetime.time(15, 15)):
            return []

        if regime in ("EXTREME_PANIC", "CHOP", "BEAR_PANIC"):
            return []

        # ── MARKET TREND GATE: Only buy longs when Nifty is above its SMA50 ──
        # During broad market crashes (e.g. Mar 4-9), individual stock filters
        # are useless — everything falls. This is THE single filter that matters.
        nifty_sma50 = 0.0
        if self.data.daily_cache and self.data.daily_cache.is_loaded():
            nifty_sma50 = self.data.daily_cache.get_sma50(NIFTY50_TOKEN)
        nifty_ltp = 0.0
        if self.data.tick_store and self.data.tick_store.is_ready():
            nifty_ltp = self.data.tick_store.get_ltp_if_fresh(NIFTY50_TOKEN)
        if nifty_ltp > 0 and nifty_sma50 > 0 and nifty_ltp < nifty_sma50:
            return []  # Market is in downtrend — no longs

        signals = []
        universe = self.data.UNIVERSE
        cache_ready = self.data.daily_cache and self.data.daily_cache.is_loaded()
        ts_ready    = self.data.tick_store and self.data.tick_store.is_ready()

        fallback_prices = {}
        if not ts_ready:
            tokens = [f"NSE:{s}" for s in universe.values()]
            try:
                q = self.data.kite.quote(tokens)
                for k, v in q.items():
                    fallback_prices[k.split(":")[1]] = v.get("last_price", 0.0)
            except Exception:
                pass

        for token, symbol in universe.items():
            if self.blackout and self.blackout.is_blackout():
                continue

            current = self.data.tick_store.get_ltp_if_fresh(token) if ts_ready else 0.0
            if current <= 0:
                current = fallback_prices.get(symbol, 0.0)
            if current <= 0:
                continue

            if not cache_ready: 
                continue

            # ── 1. Primary Trend: Price > SMA200 ──
            sma200 = self.data.daily_cache.get_sma200(token)
            if current < sma200 or sma200 <= 0:
                continue

            # ── 1b. Golden Cross: SMA50 > SMA200 (confirms healthy uptrend) ──
            sma50 = self.data.daily_cache.get_sma50(token)
            if sma50 <= sma200 or sma50 <= 0:
                continue

            # ── 2. Structural Oversold (Bollinger) ──
            bb_lo = self.data.daily_cache.get_bb_lower(token)
            if current >= bb_lo or bb_lo <= 0:
                continue

            # ── 3. Short Term Panic (RSI-4 < 25 → deeper panic only) ──
            closes = self.data.daily_cache.get_closes(token)
            if len(closes) < 10: 
                continue
                
            live_closes = closes.copy()
            live_closes.append(current)
            
            rsi_4 = (DataAgent.compute_rsi(live_closes, S1_RSI_PERIOD) or [50])[-1]
            if rsi_4 >= 25:  # Tighter: only genuine panic
                continue

            # ── Stops & Targets ──
            atr = self.data.daily_cache.get_atr(token)
            if atr <= 0:
                atr = current * 0.03
                
            atr_stop = current - (atr * 1.5)  # Tighter stop
            hard_floor = current * (1 - 0.06)  # 6% max loss
            stop_price = max(atr_stop, hard_floor)
            
            if stop_price >= current * 0.995:
                continue   # skip, stop too tight

            signals.append({
                "strategy":       "S1_RSI_MEAN_REV",
                "symbol":         symbol,
                "token":          token,
                "regime":         regime,
                "entry_price":    current,
                "partial_target": round(current + (current - stop_price), 2),
                "target_price":   round(current + (current - stop_price) * 3, 2), # Open targets, closed by RSI reverse
                "stop_price":     round(stop_price, 2),
                "rsi_4":          round(rsi_4, 2),
                "atr":            round(atr, 2),
                "product":        "CNC",
                "max_hold_days":  S1_MAX_HOLD_DAYS,
                "entry_time":     None,
                "entry_date":     None,
            })

        # Buy the deepest panic first
        return sorted(signals, key=lambda x: x["rsi_4"])[:5]

    def scan_s6_rsi_short(self, regime: str) -> list:
        """
        [V16] S6: Connors RSI(4) Intraday Exhaustion Short (MIS)
        ELITE: Institutional-grade short selling in confirmed downtrends ONLY.
        Rule 1: Nifty below SMA50 (macro weakness — no shorting in bull markets)
        Rule 2: Price < SMA200 (Primary Downtrend)
        Rule 3: Death Cross: SMA50 < SMA200 (structural breakdown confirmed)
        Rule 4: Close > Upper Bollinger Band (20, 2) (exhaustion bounce)
        Rule 5: RSI(4) > 82 (extreme short-term overbought)
        Rule 6: 3-day symbol cooldown (no re-entering losers)
        Rule 7: Minimum Rs.100 Cr turnover (liquid names only)
        Rule 8: Intraday MIS only (square off at 15:15)
        """
        now = now_ist().time()
        # Active only in morning/mid-day to allow intraday trend exhaustion
        if not (datetime.time(9, 30) <= now <= datetime.time(14, 0)):
            return []

        # ── REGIME GATE: Block in bullish or extreme markets ──
        if regime in ("BULL", "EXTREME_PANIC"):
            return []

        # ── MARKET TREND GATE: Only short when Nifty is BELOW its SMA50 ──
        # This is THE edge: we only short individual stocks when the broad
        # market is weak. In bull markets, even weak stocks get lifted.
        nifty_sma50 = 0.0
        if self.data.daily_cache and self.data.daily_cache.is_loaded():
            nifty_sma50 = self.data.daily_cache.get_sma50(NIFTY50_TOKEN)
        nifty_ltp = 0.0
        if self.data.tick_store and self.data.tick_store.is_ready():
            nifty_ltp = self.data.tick_store.get_ltp_if_fresh(NIFTY50_TOKEN)
        if nifty_ltp > 0 and nifty_sma50 > 0 and nifty_ltp > nifty_sma50:
            return []  # Market is bullish — no shorts

        # Initialise cooldown tracker if not present
        if not hasattr(self, '_s6_cooldown'):
            self._s6_cooldown = {}  # symbol -> last_trade_date

        import config as cfg

        signals = []
        universe = self.data.UNIVERSE
        cache_ready = self.data.daily_cache and self.data.daily_cache.is_loaded()
        ts_ready    = self.data.tick_store and self.data.tick_store.is_ready()

        fallback_prices = {}
        if not ts_ready:
            tokens = [f"NSE:{s}" for s in universe.values()]
            try:
                q = self.data.kite.quote(tokens)
                for k, v in q.items():
                    fallback_prices[k.split(":")[1]] = v.get("last_price", 0.0)
            except Exception: pass

        today = now_ist().date()

        for token, symbol in universe.items():
            if self.blackout and self.blackout.is_blackout(): continue

            current = self.data.tick_store.get_ltp_if_fresh(token) if ts_ready else 0.0
            if current <= 0: current = fallback_prices.get(symbol, 0.0)
            if current <= 0: continue
            if not cache_ready: continue

            # ── COOLDOWN: Skip if S6-traded this symbol within last N days ──
            last_s6_date = self._s6_cooldown.get(symbol)
            if last_s6_date:
                days_since = (today - last_s6_date).days
                if days_since < cfg.S6_COOLDOWN_DAYS:
                    continue

            # ── TURNOVER FILTER: Only short liquid stocks ──
            if self.data.get_avg_daily_turnover_cr(token) < cfg.S6_MIN_TURNOVER_CR:
                continue

            # ── 1. Primary Downtrend Filter: Price < SMA200 ──
            sma200 = self.data.daily_cache.get_sma200(token)
            if current > sma200 or sma200 <= 0:
                continue

            # ── 1b. Death Cross: SMA50 < SMA200 (confirms structural breakdown) ──
            sma50 = self.data.daily_cache.get_sma50(token)
            if sma50 >= sma200 or sma50 <= 0:
                continue  # No death cross = stock may be recovering

            # ── 2. Structural Overbought: Price > Upper BB ──
            bb_hi = self.data.daily_cache.get_bb_upper(token)
            if current <= bb_hi or bb_hi <= 0:
                continue

            # ── 3. Short Term Exhaustion: RSI(4) > 82 ──
            closes = self.data.daily_cache.get_closes(token)
            if len(closes) < 10: continue

            live_closes = closes.copy()
            live_closes.append(current)

            rsi_4 = (DataAgent.compute_rsi(live_closes, cfg.S6_RSI_PERIOD) or [50])[-1]
            if rsi_4 <= cfg.S6_RSI_OVERBOUGHT:
                continue

            # ── Stops & Targets ──
            atr = self.data.daily_cache.get_atr(token)
            if atr <= 0: atr = current * 0.03

            atr_stop = current + (atr * 1.2)  # Tighter stop for shorts
            hard_ceiling = current * 1.03      # 3% hard stop
            stop_price = min(atr_stop, hard_ceiling)

            if stop_price <= current * 1.002:
                continue   # skip, stop too tight

            signals.append({
                "strategy":       "S6_RSI_SHORT",
                "symbol":         symbol,
                "token":          token,
                "regime":         regime,
                "entry_price":    current,
                "partial_target": round(current - (stop_price - current), 2),
                "target_price":   round(current - (stop_price - current) * 3, 2),
                "stop_price":     round(stop_price, 2),
                "rsi_4":          round(rsi_4, 2),
                "atr":            round(atr, 2),
                "product":        "MIS",      # Intraday
                "is_short":       True,       # Short Selling Flag
                "max_hold_days":  0,
                "entry_time":     None,
                "entry_date":     None,
            })

        # Short the most overbought first
        return sorted(signals, key=lambda x: x["rsi_4"], reverse=True)[:5]

    def scan_s7_rsi_long(self, regime: str) -> list:
        """
        [V16] S7: Connors RSI(4) Intraday Exhaustion Long (MIS)
        High probability long in primary uptrends (Normal/Bull markets).
        Rule 1: Price > 200 SMA (Primary Uptrend)
        Rule 2: Close < Lower Bollinger Band (20, 2) OR extended
        Rule 3: RSI(4) < 18 (Short-term exhaustion)
        Rule 4: Intraday MIS only
        """
        now = now_ist().time()
        if not (datetime.time(9, 30) <= now <= datetime.time(14, 0)):
            return []

        if regime in ("EXTREME_PANIC", "BEAR_PANIC"): # Do not catch knives in crashes
            return []

        signals = []
        universe = self.data.UNIVERSE
        cache_ready = self.data.daily_cache and self.data.daily_cache.is_loaded()
        ts_ready    = self.data.tick_store and self.data.tick_store.is_ready()

        fallback_prices = {}
        if not ts_ready:
            tokens = [f"NSE:{s}" for s in universe.values()]
            try:
                q = self.data.kite.quote(tokens)
                for k, v in q.items():
                    fallback_prices[k.split(":")[1]] = v.get("last_price", 0.0)
            except Exception: pass

        for token, symbol in universe.items():
            if self.blackout and self.blackout.is_blackout(): continue

            current = self.data.tick_store.get_ltp_if_fresh(token) if ts_ready else 0.0
            if current <= 0: current = fallback_prices.get(symbol, 0.0)
            if current <= 0: continue
            if not cache_ready: continue

            # ── 1. Primary Uptrend Filter ──
            sma200 = self.data.daily_cache.get_sma200(token)
            if current < sma200 or sma200 <= 0:
                continue

            # ── 2. Structural Oversold ──
            bb_lo = self.data.daily_cache.get_bb_lower(token)
            if current >= bb_lo or bb_lo <= 0:
                continue

            # ── 3. Short Term Exhaustion (RSI-4) ──
            closes = self.data.daily_cache.get_closes(token)
            if len(closes) < 10: continue
                
            live_closes = closes.copy()
            live_closes.append(current)
            
            import config
            rsi_4 = (DataAgent.compute_rsi(live_closes, config.S1_RSI_PERIOD) or [50])[-1]
            if rsi_4 >= 18: # Only extreme panic
                continue

            # ── Stops & Targets ──
            atr = self.data.daily_cache.get_atr(token)
            if atr <= 0: atr = current * 0.03
                
            atr_stop = current - (atr * 1.5)  # Let it breathe
            hard_floor = current * 0.97 # 3% hard stop
            stop_price = max(atr_stop, hard_floor)
            
            if stop_price >= current * 0.998:
                continue   # skip, stop too tight

            signals.append({
                "strategy":       "S7_RSI_LONG",
                "symbol":         symbol,
                "token":          token,
                "regime":         regime,
                "entry_price":    current,
                "partial_target": round(current + abs(current - stop_price), 2),
                "target_price":   round(current + abs(current - stop_price) * 3, 2), # Trail to EOD
                "stop_price":     round(stop_price, 2),
                "rsi_4":          round(rsi_4, 2),
                "atr":            round(atr, 2),
                "product":        "MIS",      # Intraday
                "max_hold_days":  0,
                "entry_time":     None,
                "entry_date":     None,
            })

        # Buy the deepest panic first
        return sorted(signals, key=lambda x: x["rsi_4"])[:5]

    def scan_s2_overreaction(self) -> list:
        """
        Entirely tick_store driven during trading hours.
        day_open, LTP, volume, depth, 5-min candles — all from TickStore.
        Zero REST calls per scan cycle when tick_store is ready.
        """
        now  = now_ist().time()
        in_p = datetime.time(9, 30) <= now <= datetime.time(11, 0)
        in_s = datetime.time(14, 0) <= now <= datetime.time(15, 0)
        if not (in_p or in_s):
            return []

        ts_ready = (self.data.tick_store and
                    self.data.tick_store.is_fresh())
        signals  = []

        for token, symbol in self.data.UNIVERSE.items():
            # ── Turnover — cache ──────────────────────────────────────
            if self.data.get_avg_daily_turnover_cr(token) < S2_MIN_TURNOVER_CR:
                continue

            # ── Current price and day open — fresh tick, REST fallback ──
            if ts_ready:
                current  = self.data.tick_store.get_ltp_if_fresh(token)
                day_open = self.data.tick_store.get_day_open(token)
            else:
                # WebSocket stale — do NOT call historical_data() per symbol.
                # S2 is a real-time intraday strategy. Without live ticks there
                # is no valid signal. Kite historical_data() is limited to ~3/sec
                # — 100 individual calls would cause a 429 ban and kill the engine.
                # Skip and wait for WebSocket to recover.
                continue

            if current <= 0 or day_open <= 0:
                continue

            # ── Gap-down filter ───────────────────────────────────────
            # Reject if stock gapped down >3% from previous close.
            # A gap-down indicates a structural/fundamental event (earnings,
            # news, macro). BNF avoided these — the bounce logic assumes
            # intraday panic, not overnight structural damage.
            prev_close = 0.0
            if self.data.daily_cache and self.data.daily_cache.is_loaded():
                closes = self.data.daily_cache.get_closes(token)
                if len(closes) >= 1:
                    # closes[-1] is yesterday's close — daily_cache loaded at
                    # 8:45 AM before market opens, so no today candle exists yet.
                    # closes[-2] would be day-before-yesterday — wrong.
                    prev_close = closes[-1]
            if prev_close > 0:
                gap_down_pct = (prev_close - day_open) / prev_close
                if gap_down_pct > 0.03:      # >3% gap — skip
                    continue

            # ── Circuit breaker — cache ───────────────────────────────
            if self.data.check_circuit_breaker(symbol):
                continue

            # ── Drop filter ───────────────────────────────────────────
            drop = (day_open - current) / day_open
            if not (S2_DROP_MIN <= drop <= S2_DROP_MAX):
                continue

            # ── RVOL — tick_store volume / daily_cache avg ────────────
            rvol = self.data.compute_rvol(token)
            if rvol < S2_RVOL_MIN:
                continue

            # ── Reversal candle — tick_store 5-min candles ────────────
            candles = self.data.get_intraday_ohlcv(token, "5minute")
            if len(candles) < 6:
                continue
            if not self._reversal_candle(candles):
                continue

            # ── Pivot support — soft scoring (not hard reject) ────────
            support_bonus = 0
            support = self.data.compute_pivot_support(token)
            if support > 0 and abs(current - support) / support <= 0.015:
                support_bonus = 1  # Near pivot support = bonus for ranking

            # ── Bid/ask depth — soft scoring (not hard reject) ────────
            depth = self.data.get_order_depth(token)
            depth_ratio = depth.get("bid_ask_ratio", 1.0) if depth else 1.0

            signals.append({
                "strategy":          "S2_OVERREACTION",
                "symbol":            symbol,
                "token":             token,
                "entry_price":       current,
                "partial_target_1":  round(current * (1 + S2_PARTIAL_TARGET_1), 2),
                "target_price":      round(current * (1 + S2_PARTIAL_TARGET_2), 2),
                "stop_price":        round(current * (1 - S2_HARD_STOP_PCT), 2),
                "drop_pct":          round(drop * 100, 2),
                "rvol":              round(rvol, 2),
                "bid_ask_ratio":     round(depth_ratio, 2),
                "_rank_score":       rvol + support_bonus + (depth_ratio - 1.0),
                "product":           "MIS",
                "time_stop_minutes": S2_TIME_STOP_MINUTES,
                "entry_time":        None,
            })

        return sorted(signals, key=lambda x: x.get("_rank_score", 0), reverse=True)[:3]

    def _reversal_candle(self, candles: list) -> bool:
        if len(candles) < 2:
            return False
        last, prev = candles[-1], candles[-2]
        body  = abs(last["close"] - last["open"])
        rng   = last["high"] - last["low"]
        if rng == 0:
            return False
        lo_wick = min(last["open"], last["close"]) - last["low"]
        hi_wick = last["high"] - max(last["open"], last["close"])
        hammer  = (last["close"] > last["open"] and
                   lo_wick >= 2 * body and hi_wick <= body * 0.5)
        engulf  = (prev["close"] < prev["open"] and
                   last["close"] > last["open"] and
                   last["open"] < prev["close"] and
                   last["close"] > prev["open"])
        return hammer or engulf

    # ── [V16] S3 SEPA + VCP Scan ──────────────────────────────────────────

    def scan_s3_sepa(self) -> list:
        """
        S3: SEPA + VCP swing (CNC multi-week).
        Runs once at ~9:00–9:30 AM pre-market.
        Requires: daily_cache (loaded), fundamental_agent, stage_agent, vcp_agent.
        """
        if not (self._fundamental and self._stage and self._vcp):
            return []
        if not (self.data.daily_cache and self.data.daily_cache.is_loaded()):
            return []

        signals = []

        for token, symbol in self.data.UNIVERSE.items():
            # ── Turnover filter ─────────────────────────────────────────
            if self.data.get_avg_daily_turnover_cr(token) < S3_MIN_TURNOVER_CR:
                continue

            # ── Stage 2 check ──────────────────────────────────────────
            if not self._stage.is_stage_2(token):
                continue

            # ── Fundamentals (EPS/Sales/ROE/DE + EPS acceleration) ─────
            passes_fund, reason = self._fundamental.passes_sepa_fundamentals(symbol)
            if not passes_fund:
                continue

            # ── VCP detection ──────────────────────────────────────────
            vcp = self._vcp.detect_vcp(token)
            if not vcp:
                continue

            # ── RS score ───────────────────────────────────────────────
            rs = self.data.daily_cache.get_rs_score(token)
            if rs < S3_MIN_RS_SCORE:
                continue

            pivot = vcp["pivot"]
            stop  = vcp["stop"]
            stop_pct = (pivot - stop) / pivot if pivot > 0 else 1.0
            if stop_pct > S3_MAX_STOP_PCT:
                continue

            signals.append({
                "strategy":        "S3_SEPA_VCP",
                "symbol":          symbol,
                "token":           token,
                "entry_price":     pivot,
                "stop_price":      round(stop, 2),
                "target_price":    round(pivot * (1 + S3_TARGET_SWING_PCT), 2),
                "partial_target":  round(pivot * (1 + S3_PARTIAL_EXIT_PCT), 2),
                "rs_score":        rs,
                "vcp_contractions": vcp["n_contractions"],
                "vcp_final_depth":  vcp["final_depth"],
                "product":         "CNC",
                "max_hold_days":   S3_MAX_HOLD_DAYS,
                "entry_time":      None,
                "entry_date":      None,
            })

        return sorted(signals, key=lambda x: x["rs_score"], reverse=True)[:5]

    def scan_s4_leadership(self) -> list:
        """
        S4: Leadership Breakout (CNC momentum swing).
        Runs every ~60s during trading hours (intraday scan).
        [V16] Regime-adaptive thresholds: BEAR_PANIC=90, BULL=75
        """
        if not (self.data.daily_cache and self.data.daily_cache.is_loaded()):
            return []
        ts_ready = self.data.tick_store and self.data.tick_store.is_fresh()
        if not ts_ready:
            return []

        # [V16] Regime-adaptive RS threshold
        regime = self._mkt_status.detect() if self._mkt_status else "NORMAL"
        rs_threshold = {
            "EXTREME_PANIC": 95,
            "BEAR_PANIC": 90,      # Top 10% only in panic
            "NORMAL": S4_MIN_RS_SCORE,  # 80
            "BULL": 75,            # Top 25% in bull
            "CHOP": 85
        }.get(regime, S4_MIN_RS_SCORE)

        signals = []

        for token, symbol in self.data.UNIVERSE.items():
            # ── Turnover filter ─────────────────────────────────────────
            if self.data.get_avg_daily_turnover_cr(token) < S4_MIN_TURNOVER_CR:
                continue

            # ── [V16] Regime-adaptive RS score ─────────────────────────
            rs = self.data.daily_cache.get_rs_score(token)
            if rs < rs_threshold:
                continue

            # ── Near 52-week high (tighten in BEAR) ────────────────────
            high_52w = self.data.daily_cache.get_high_52w(token)
            current = self.data.tick_store.get_ltp_if_fresh(token)
            if current <= 0 or high_52w <= 0:
                continue
            
            max_below_52w = {
                "BEAR_PANIC": 0.02,    # Within 2% (ultra-tight)
                "NORMAL": S4_MAX_BELOW_52W_HIGH,  # 5%
                "BULL": 0.07           # Within 7%
            }.get(regime, S4_MAX_BELOW_52W_HIGH)
            
            if current < high_52w * (1 - max_below_52w):
                continue

            # ── [v13 FIXED] Relative Performance vs Nifty ──────────────
            nifty_ltp = self.data.tick_store.get_ltp_if_fresh(NIFTY50_TOKEN)
            nifty_open = self.data.tick_store.get_day_open(NIFTY50_TOKEN)
            stock_open = self.data.tick_store.get_day_open(token)
            if nifty_ltp > 0 and nifty_open > 0 and stock_open > 0:
                nifty_chg = (nifty_ltp - nifty_open) / nifty_open
                stock_chg = (current - stock_open) / stock_open
                if stock_chg <= nifty_chg * 1.05:  # [V16] 5% outperformance req (was 10%)
                    continue

            # ── Volume confirmation: regime-adaptive ──────────────────
            day_vol = self.data.tick_store.get_volume(token) or 0
            avg_vol = self.data.daily_cache.get_avg_daily_vol(token)
            if avg_vol <= 0:
                continue
            rvol = day_vol / avg_vol
            
            vol_threshold = {
                "BEAR_PANIC": 2.0,     # 200% in panic
                "NORMAL": S4_BREAKOUT_VOL_MIN,  # 1.5x
                "BULL": 1.3            # 130% sufficient
            }.get(regime, S4_BREAKOUT_VOL_MIN)
            
            if rvol < vol_threshold:
                continue

            # ── Sector relative strength (soft filter) ─────────────────
            sector_rs = self.data.daily_cache.get_sector_rs(symbol)
            if sector_rs > 0 and sector_rs < 70:  # Only block if we have real data
                continue

            stop = round(current * (1 - S4_MAX_STOP_PCT), 2)

            signals.append({
                "strategy": "S4_LEADERSHIP",
                "symbol": symbol,
                "token": token,
                "entry_price": current * 1.005,  # 0.5% above LTP
                "stop_price": stop,
                "target_price": round(current * (1 + S4_TARGET_SWING_PCT), 2),
                "partial_target": round(current * (1 + S4_PARTIAL_EXIT_PCT), 2),
                "rs_score": rs,
                "rvol": round(rvol, 2),
                "pct_from_52wh": round((1 - current / high_52w) * 100, 2),
                "regime": regime,
                "sector_rs": sector_rs,
                "product": "CNC",
                "max_hold_days": S4_MAX_HOLD_DAYS,
                "entry_time": None,
                "entry_date": None,
            })

        return sorted(signals, key=lambda x: x["rs_score"], reverse=True)[:3]

    # ── [V16] S5: VWAP + Opening Range Breakout ─────────────────────

    def scan_s5_vwap_orb(self) -> list:
        """
        Professional intraday VWAP+ORB strategy.
        Runs 09:45–14:30 only (after ORB period is locked).

        Entry conditions (ALL must be true):
        1. ORB period complete (first 15 min candle locked)
        2. ORB range between 0.5%–3% of price
        3. Price breaks ABOVE ORB high
        4. Price is ABOVE VWAP (VWAP confirmation)
        5. Volume is ≥ 1.3× average daily volume by this time
        6. Stock is highly liquid (avg turnover > Rs.500 Cr)

        Stop: max(VWAP, ORB low, entry - ATR×1.5)
        Target: entry + 2× (entry - stop)
        Product: MIS (intraday, squared off by 15:00)
        """
        now_t = now_ist().time()
        # Only scan after ORB period (09:30) and before 14:30
        if now_t < datetime.time(9, 45) or now_t > datetime.time(14, 30):
            return []

        tick_store  = self.data.tick_store
        daily_cache = self.data.daily_cache
        if not tick_store or not daily_cache:
            return []

        signals = []
        universe = self.data.UNIVERSE if self.data else {}

        for token, symbol in universe.items():
            try:
                # Skip if blackout
                if self.blackout and self.blackout.is_blackout():
                    continue

                # Liquidity filter — only highly liquid stocks
                avg_turn = daily_cache.get_avg_turnover_cr(token)
                if avg_turn < S5_MIN_TURNOVER_CR:
                    continue

                # Get ORB data
                orb = tick_store.get_orb(token)
                if not orb.get("orb_locked"):
                    continue  # ORB period not complete yet

                orb_high = orb["orb_high"]
                orb_low  = orb["orb_low"]
                orb_range_pct = orb.get("orb_range_pct", 0)

                # ORB range filter
                if orb_range_pct < S5_MIN_ORB_PCT or orb_range_pct > S5_MAX_ORB_PCT:
                    continue

                # Get current price
                ltp = tick_store.get_ltp_if_fresh(token)
                if ltp <= 0:
                    continue

                # ORB breakout: price must be above ORB high
                if ltp <= orb_high:
                    continue

                # VWAP confirmation: price must be above VWAP
                vwap = tick_store.get_vwap(token)
                if vwap <= 0 or ltp < vwap:
                    continue

                # VWAP proximity: entry should be within 0.5% of VWAP
                vwap_dist = abs(ltp - vwap) / vwap
                if vwap_dist > S5_VWAP_PROXIMITY_PCT * 5:
                    # If price is too far above VWAP (>2.5%), skip
                    # This avoids chasing extended stocks
                    continue

                # Volume confirmation
                day_vol = tick_store.get_volume(token)
                avg_vol = daily_cache.get_avg_daily_vol(token)
                if avg_vol <= 0:
                    continue
                rvol = day_vol / avg_vol
                if rvol < S5_MIN_RVOL:
                    continue

                # Circuit breaker check
                if daily_cache.is_circuit_breaker(token, ltp):
                    continue

                # Compute stop price: max(VWAP, ORB low, entry - ATR×1.5)
                atr = daily_cache.get_atr(token)
                if atr <= 0:
                    atr = ltp * 0.01  # fallback 1%

                atr_stop  = ltp - (atr * S5_ATR_STOP_MULTIPLIER)
                hard_stop = ltp * (1 - S5_HARD_STOP_PCT)
                # Use the tightest reasonable stop
                stop_price = max(vwap, orb_low, atr_stop, hard_stop)

                # Stop must be below entry (sanity)
                if stop_price >= ltp * 0.998:
                    continue

                risk = ltp - stop_price
                if risk <= 0:
                    continue

                # Target: 2× risk (minimum 2:1 R:R)
                target_price = ltp + (risk * S5_TARGET_RR)

                signals.append({
                    "strategy":       "S5_VWAP_ORB",
                    "symbol":         symbol,
                    "token":          token,
                    "entry_price":    round(ltp, 2),
                    "stop_price":     round(stop_price, 2),
                    "target_price":   round(target_price, 2),
                    "partial_target": round(ltp + risk, 2),  # 1:1 partial
                    "vwap":           round(vwap, 2),
                    "orb_high":       round(orb_high, 2),
                    "orb_low":        round(orb_low, 2),
                    "orb_range_pct":  round(orb_range_pct * 100, 2),
                    "rvol":           round(rvol, 2),
                    "atr":            round(atr, 2),
                    "rr":             round(S5_TARGET_RR, 1),
                    "product":        "MIS",
                    "entry_time":     None,
                    "entry_date":     None,
                })

            except Exception as e:
                print(f"[Scanner] S5 error {symbol}: {e}")
                continue

        # Sort by R:R confirmation quality (closest to VWAP = best confirmation)
        signals.sort(key=lambda x: abs(x["entry_price"] - x["vwap"]))
        return signals[:S5_MAX_TRADES_PER_DAY]
