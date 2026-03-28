"""
BNF Engine V19 — New Strategies.MD (10-Strategy Adaptive Intraday System)

Implements 8 active MIS strategies (6 new from MD + 2 retained from V18):
  S1_MA_CROSS      — 9/21 EMA Crossover + ADX(14) + 200 EMA filter (MD Strategy 1)
  S2_BB_MEAN_REV   — BB(20,2σ) + RSI(14) + VWAP (MD Strategy 2)
  S3_ORB           — Opening Range Breakout 9:15-9:30 (MD Strategy 3)
  S6_TREND_SHORT   — Intraday Short: VWAP + RSI + relative weakness (V18 retained)
  S6_VWAP_BAND     — VWAP ± 1.5 SD mean reversion (MD Strategy 6)
  S7_MEAN_REV_LONG — Oversold bounce in uptrends (V18 retained)
  S8_VOL_PIVOT     — Volume Profile + Pivot Point Breakout (MD Strategy 8)
  S9_MTF_MOMENTUM  — Daily 200 EMA + 15-min RSI + MACD (MD Strategy 9)

NOT implemented (require external infrastructure):
  Strategy 4: Cash-Futures Arbitrage
  Strategy 5: Pairs Trading / StatArb
  Strategy 7: Options Iron Condor
  Strategy 10: ML Hybrid (Random Forest)

Startup sequence:
  8:30 AM  -> Auto token refresh (headless Zerodha login)
  8:45 AM  -> Blackout calendar refresh + daily cache preload (260d)
  9:00 AM  -> Crash recovery + regime detection
  9:20 AM  -> Trading begins (Window 1: 9:20-11:30)
  Every 60s -> Tick: All 8 strategy scans + execute + monitor
  Every 15m -> Regime re-check
  11:30-13:15 -> No Trade Zone (midday chop)
  13:15 PM -> Trading resumes (Window 2: 13:15-15:00)
  15:15 PM -> MIS Square-off
  15:30 PM -> Daily summary + journal
"""

import os
import datetime
import time
import schedule
from dotenv import load_dotenv
from kiteconnect import KiteConnect

from core.auto_login import AutoLogin
from core.blackout_calendar import BlackoutCalendar
from core.state_manager import StateManager
from storage.tick_store import TickStore
from storage.daily_cache import DailyCache
from core.paper_broker import PaperBroker
from agents.data_agent import DataAgent
from agents.scanner_agent import ScannerAgent
from agents.risk_agent import RiskAgent
from core.journal import Journal
from agents.execution_agent import ExecutionAgent
from kiteconnect import KiteTicker
from config import (
    KITE_API_KEY, TOTAL_CAPITAL, NIFTY50_TOKEN, INDIA_VIX_TOKEN,
    MAX_OPEN_POSITIONS, MAX_TRADES_PER_DAY,
    PAPER_MODE, VIX_EXTREME_STOP, now_ist, today_ist
)


class BNFEngine:

    def __init__(self):
        # Core modules
        self.auto_login = AutoLogin()
        self.blackout   = BlackoutCalendar()
        self.state      = StateManager()
        self.journal    = Journal()

        # Capital resolved at _init_kite() time
        self.capital    = TOTAL_CAPITAL
        self.risk       = RiskAgent(self.capital)

        # Kite + WebSocket + cache
        self.kite         = None
        self.ticker       = None
        self.tick_store   = None
        self.daily_cache  = None
        self.data         = None
        self.scanner      = None
        self.execution    = None

        self.regime     = "UNKNOWN"
        self.token_ok   = False
        self.scan_count = 0
        self._ws_was_fresh = True

    def _fetch_live_capital(self, real_kite: KiteConnect) -> float:
        """
        Fetches deployable cash from Kite margins API.
        PAPER MODE: returns TOTAL_CAPITAL.
        LIVE MODE: returns live_balance from margins().
        """
        from config import PAPER_MODE
        if PAPER_MODE:
            return TOTAL_CAPITAL

        try:
            margins   = real_kite.margins(segment="equity")
            available = float(margins["available"]["live_balance"])
            if available <= 0:
                raise ValueError(f"live_balance={available}")
            print(f"[Capital] Live balance from Kite: Rs.{available:,.0f}")
            return available
        except Exception as e:
            print(f"[Capital] margins() failed: {e} -- "
                  f"using .env fallback Rs.{TOTAL_CAPITAL:,.0f}")
            return TOTAL_CAPITAL

    def _init_kite(self):
        """
        Called after successful token refresh (8:30 AM daily).
        Sets up: KiteConnect, TickStore, KiteTicker, DailyCache, 
                 DataAgent, PaperBroker, ScannerAgent, ExecutionAgent.
        """
        load_dotenv(override=True)
        from config import PAPER_MODE
        access_token = os.getenv("KITE_ACCESS_TOKEN")

        # 1 — KiteConnect for REST
        real_kite = KiteConnect(api_key=KITE_API_KEY)
        real_kite.set_access_token(access_token)

        # 2 — Capital
        self.capital = self._fetch_live_capital(real_kite)

        # 3 — DataAgent
        self.data = DataAgent(real_kite)

        # 4 — RiskAgent (needs data for live VIX checking)
        self.risk = RiskAgent(self.capital, data_agent=self.data)

        # Subscription list
        sub_tokens = (list(self.data.UNIVERSE.keys()) +
                      [NIFTY50_TOKEN, INDIA_VIX_TOKEN])

        # 5 — TickStore
        self.tick_store = TickStore()

        # 6 — KiteTicker
        if self.ticker:
            try:
                self.ticker.close()
            except Exception:
                pass
        self.ticker = KiteTicker(KITE_API_KEY, access_token)
        self.ticker.on_ticks   = self.tick_store.on_ticks
        self.ticker.on_connect = lambda ws, r: (
            ws.subscribe(sub_tokens),
            ws.set_mode(ws.MODE_FULL, sub_tokens)
        )
        self.ticker.on_close   = lambda ws, c, r: print(
            f"[Ticker] Closed: {c} {r}"
        )
        self.ticker.on_error   = lambda ws, c, r: print(
            f"[Ticker] Error: {c} {r}"
        )
        self.ticker.connect(threaded=True)

        # Wait for first tick
        import time as _time
        for _ in range(30):
            if self.tick_store.is_ready():
                break
            _time.sleep(0.5)
        ws_status = "connected" if self.tick_store.is_ready() else "NOT connected"
        print(f"[BNFEngine] WebSocket: {ws_status} | "
              f"Tokens subscribed: {len(sub_tokens)}")

        # 7 — DailyCache
        self.daily_cache = DailyCache(real_kite)

        # 8 — Inject caches into DataAgent
        self.data.tick_store  = self.tick_store
        self.data.daily_cache = self.daily_cache

        # 9 — Order broker
        if PAPER_MODE:
            symbol_token = {v: k for k, v in self.data.UNIVERSE.items()}
            self.kite = PaperBroker(
                real_kite,
                capital=self.capital,
                tick_store=self.tick_store,
                symbol_token=symbol_token,
            )
            print(f"[BNFEngine] PAPER MODE -- orders virtual, "
                  f"capital fixed Rs.{self.capital:,.0f}")
        else:
            self.kite = real_kite
            print(f"[BNFEngine] LIVE MODE -- real orders, "
                  f"capital Rs.{self.capital:,.0f}")

        # 10 — ScannerAgent (all strategies)
        self.scanner = ScannerAgent(self.data, self.blackout)

        # 11 — S4: Load index futures tokens (FUTURES ONLY — no options)
        #       DataAgent.load_futures_tokens() already filters instrument_type == FUT
        futures_map = self.data.load_futures_tokens()
        if futures_map:
            # Subscribe futures tokens to existing WebSocket
            fut_tokens = [v["token"] for v in futures_map.values()]
            try:
                self.ticker.subscribe(fut_tokens)
                self.ticker.set_mode(self.ticker.MODE_FULL, fut_tokens)
                print(f"[BNFEngine] S4 futures subscribed: "
                      f"{[v['symbol'] for v in futures_map.values()]}")
            except Exception as e:
                print(f"[BNFEngine] S4 futures subscription failed: {e}")
            self.scanner.set_futures_tokens(futures_map)
        else:
            print("[BNFEngine] S4: No futures tokens loaded — arbitrage disabled today")

        # 12 — ExecutionAgent
        self.execution = ExecutionAgent(
            self.kite, self.risk, self.journal, self.state
        )
        self.execution._data_universe = self.data.UNIVERSE

    # ── 8:30 AM: Auto token refresh ───────────────────────────────

    def auto_token_refresh(self):
        print(f"[Engine] Auto token refresh starting...")
        success = self.auto_login.run(alert_fn=self._raw_alert)
        if success:
            self._init_kite()
            self.token_ok = True
        else:
            self.token_ok = False

    # ── 8:45 AM: Refresh blackout calendar + load daily cache ────

    def refresh_calendar(self):
        self.blackout.refresh()
        print("[Engine] Blackout calendar refreshed")

        if self.blackout.is_blackout():
            alert_fn = self.execution.alert if self.execution else self._raw_alert
            alert_fn(
                f"*BLACKOUT DAY -- ENGINE OFF*\n"
                f"Date: `{today_ist()}`\n"
                f"Reason: NSE holiday or RBI policy day.\n"
                f"No trades today. Engine will restart tomorrow."
            )
            os._exit(0)

    def preload_cache(self):
        """
        Called at 8:45 AM -- loads 260 days of daily OHLCV for all symbols.
        After this: all scanner calls read from memory.
        """
        if not self.daily_cache or not self.data:
            print("[Engine] preload_cache: daily_cache not ready, skipping")
            return

        print("[Engine] Loading daily cache (technical data)...")
        ok = self.daily_cache.preload(self.data.UNIVERSE)
        if not ok:
            print("[Engine] Daily cache load failed or partial -- REST fallback active")

    # ── 9:00 AM: Pre-market ───────────────────────────────────────

    def pre_market(self):
        if not self.token_ok:
            self._raw_alert("ENGINE ABORTED -- Token refresh failed. No trades today.")
            return

        # Crash recovery
        self.execution.restore_from_state()

        # Reset VWAP/ORB data for new trading day
        if self.tick_store:
            self.tick_store.reset_daily()

        # Regime detection
        self.regime = self.scanner.detect_regime()

        # Status
        ws_ok    = self.tick_store and self.tick_store.is_ready()
        cache_ok = self.daily_cache and self.daily_cache.is_loaded()
        universe_count = len(self.data.UNIVERSE) if self.data else 0
        self.execution.alert(
            f"*BNF Engine V19 -- ARMED* (10-Strategy System)\n"
            f"Date: `{today_ist()}`\n"
            f"Regime: `{self.regime}`\n"
            f"Strategies: S1(MA) S2(BB) S3(ORB) S4(Arb*FUT) S6(Short/VWAP) S7(MR) S8(Pivot) S9(MTF)\n"
            f"Universe: `{universe_count}` symbols\n"
            f"Cache: `{'OK' if cache_ok else 'WARN'}`  "
            f"WS: `{'OK' if ws_ok else 'WARN'}`\n"
            f"Max trades/day: `{MAX_TRADES_PER_DAY}`"
        )

    # ── Every 60 seconds: main tick ──────────────────────────────

    def tick(self):
        try:
            self._tick_inner()
        except Exception as e:
            print(f"[Engine] tick() ERROR: {type(e).__name__}: {e}")
            if self.execution:
                try:
                    self.execution.alert(
                        f"*ENGINE TICK ERROR*\n"
                        f"`{type(e).__name__}: {str(e)[:200]}`"
                    )
                except Exception:
                    pass

    def _tick_inner(self):
        if not self.token_ok or not self.execution:
            return
            
        # ── EXTERNAL MANUAL KILL SWITCH ──
        import os, config
        kill_file = os.path.join(config.BASE_DIR, "data", "kill_switch.txt")
        if os.path.exists(kill_file):
            self.risk.engine_stopped = True
            self.risk.stop_reason = "MANUAL_KILL_SWITCH_FILE_TRIGGERED"
        
        
        # ── KILL SWITCH GUARD ──
        if self.risk.engine_stopped:
            if len(self.execution.active_trades) > 0:
                if not getattr(self, '_stop_flattened', False):
                    self.execution.alert(f"*ENGINE STOPPED*: `{self.risk.stop_reason}` - Flattening all active trades instantly!")
                    self.execution.flatten_all(f"KILL_SWITCH: {self.risk.stop_reason}")
                    self._stop_flattened = True
                
                # We must continue monitoring positions until they are successfully cleared
                self.execution.monitor_positions(
                    daily_cache=self.daily_cache,
                    tick_store=self.tick_store
                )
            return

        # WebSocket health check
        if self.tick_store:
            now_fresh = self.tick_store.is_fresh()
            if self._ws_was_fresh and not now_fresh:
                self.execution.alert(
                    "*WEBSOCKET DISCONNECTED*\n"
                    "No ticks in 10+ seconds.\n"
                    "Falling back to REST. Scan accuracy reduced."
                )
            elif not self._ws_was_fresh and now_fresh:
                self.execution.alert("*WEBSOCKET RECONNECTED* -- live ticks restored.")
            self._ws_was_fresh = now_fresh

        # Valid trading time check
        can_trade, reason = self.scanner.is_valid_trading_time()
        if not can_trade:
            if reason.startswith("EXTREME_PANIC") and self.regime != "EXTREME_PANIC":
                self.regime = "EXTREME_PANIC"
                print(f"[Engine] EXTREME PANIC -- trading halted.")
            # Still monitor open positions even outside trading hours
            self.execution.monitor_positions(
                daily_cache=self.daily_cache,
                tick_store=self.tick_store
            )
            return

        self.scan_count += 1

        # Re-check regime every 15 minutes
        if now_ist().minute % 15 == 0:
            new_regime = self.scanner.detect_regime()
            if new_regime != self.regime:
                print(f"[Engine] Regime: {self.regime} -> {new_regime}")
                _emoji = {"BULL": "green", "NORMAL": "blue",
                          "BEAR_PANIC": "red", "CHOP": "black",
                          "EXTREME_PANIC": "SOS"
                         }.get(new_regime, "?")
                self.execution.alert(
                    f"*REGIME CHANGE* [{_emoji}]\n`{self.regime}` -> `{new_regime}`"
                )
                self.regime = new_regime
            self.state.set_kv("last_regime", self.regime)
            # Refresh circuit breaker limits
            if self.daily_cache and self.data:
                self.daily_cache.refresh_circuit_limits(self.data.UNIVERSE)

        # ── S1: Moving Average Crossover (MD Strategy 1) ──
        try:
            s1_signals = self.scanner.scan_s1_ma_cross(self.regime)
            for sig in s1_signals:
                if len(self.execution.active_trades) < MAX_OPEN_POSITIONS:
                    ok = self.execution.execute(sig, regime=self.regime)
                    if ok:
                        self.scanner.register_trade()
                    break
        except Exception as e:
            print(f"[Engine] S1 scan ERROR: {type(e).__name__}: {e}")

        # ── S2: BB + RSI Mean Reversion (MD Strategy 2) ──
        try:
            s2_signals = self.scanner.scan_s2_bb_mean_rev(self.regime)
            for sig in s2_signals:
                if len(self.execution.active_trades) < MAX_OPEN_POSITIONS:
                    ok = self.execution.execute(sig, regime=self.regime)
                    if ok:
                        self.scanner.register_trade()
                    break
        except Exception as e:
            print(f"[Engine] S2 scan ERROR: {type(e).__name__}: {e}")

        # ── S3: Opening Range Breakout (MD Strategy 3) ──
        try:
            s3_signals = self.scanner.scan_s3_orb()
            for sig in s3_signals:
                if len(self.execution.active_trades) < MAX_OPEN_POSITIONS:
                    ok = self.execution.execute(sig, regime=self.regime)
                    if ok:
                        self.scanner.register_trade()
                        self.scanner.register_s3_trade()
                    break
        except Exception as e:
            print(f"[Engine] S3 scan ERROR: {type(e).__name__}: {e}")

        # ── S6: Trend Breakout Short (V18 retained) ──
        try:
            s6_signals = self.scanner.scan_s6_trend_short(self.regime)
            for sig in s6_signals:
                if len(self.execution.active_trades) < MAX_OPEN_POSITIONS:
                    ok = self.execution.execute(sig, regime=self.regime)
                    if ok:
                        self.scanner.register_trade()
                        self.scanner._s6_cooldown[sig["symbol"]] = now_ist().date()
                    break
        except Exception as e:
            print(f"[Engine] S6 scan ERROR: {type(e).__name__}: {e}")


        # ── S4: Index Futures Arbitrage (MD Strategy 4 — FUTURES ONLY) ──
        try:
            s4_signals = self.scanner.scan_s4_arbitrage()
            for sig in s4_signals:
                if len(self.execution.active_trades) < MAX_OPEN_POSITIONS:
                    ok = self.execution.execute(sig, regime=self.regime)
                    if ok:
                        self.scanner.register_trade()
                    break
        except Exception as e:
            print(f"[Engine] S4 scan ERROR: {type(e).__name__}: {e}")

        # ── S6_VWAP_BAND: VWAP Mean Reversion (MD Strategy 6) ──
        try:
            s6v_signals = self.scanner.scan_s6_vwap_band(self.regime)
            for sig in s6v_signals:
                if len(self.execution.active_trades) < MAX_OPEN_POSITIONS:
                    ok = self.execution.execute(sig, regime=self.regime)
                    if ok:
                        self.scanner.register_trade()
                    break
        except Exception as e:
            print(f"[Engine] S6_VWAP scan ERROR: {type(e).__name__}: {e}")

        # ── S7: Mean Reversion Long (V18 retained) ──
        try:
            s7_signals = self.scanner.scan_s7_mean_rev_long(self.regime)
            for sig in s7_signals:
                if len(self.execution.active_trades) < MAX_OPEN_POSITIONS:
                    ok = self.execution.execute(sig, regime=self.regime)
                    if ok:
                        self.scanner.register_trade()
                    break
        except Exception as e:
            print(f"[Engine] S7 scan ERROR: {type(e).__name__}: {e}")


        # ── S8: Volume Profile + Pivot Breakout (MD Strategy 8) ──
        try:
            s8_signals = self.scanner.scan_s8_vol_pivot(self.regime)
            for sig in s8_signals:
                if len(self.execution.active_trades) < MAX_OPEN_POSITIONS:
                    ok = self.execution.execute(sig, regime=self.regime)
                    if ok:
                        self.scanner.register_trade()
                    break
        except Exception as e:
            print(f"[Engine] S8 scan ERROR: {type(e).__name__}: {e}")

        # ── S9: Multi-Timeframe Trend + Momentum (MD Strategy 9) ──
        try:
            s9_signals = self.scanner.scan_s9_mtf_momentum(self.regime)
            for sig in s9_signals:
                if len(self.execution.active_trades) < MAX_OPEN_POSITIONS:
                    ok = self.execution.execute(sig, regime=self.regime)
                    if ok:
                        self.scanner.register_trade()
                    break
        except Exception as e:
            print(f"[Engine] S9 scan ERROR: {type(e).__name__}: {e}")

        # Monitor open positions
        self.execution.monitor_positions(
            daily_cache=self.daily_cache,
            tick_store=self.tick_store
        )

    # ── 15:20 PM: Force EOD Flatten ──────────────────────────────
    
    def force_eod_exit(self):
        if self.execution:
            print("[Engine] 15:20 EOD FORCED FLATTEN executed.")
            self.execution.flatten_all("EOD_FORCED")

    # ── 15:30 PM: End of day ─────────────────────────────────────

    def end_of_day(self):
        if not self.execution:
            return
        self.execution.daily_summary_alert(self.regime,
                                            total_scans=self.scan_count)
        self.scan_count = 0

        if PAPER_MODE and hasattr(self.kite, "get_paper_summary"):
            s = self.kite.get_paper_summary()
            self.execution.alert(
                f"*PAPER SESSION SUMMARY*\n"
                f"Orders: `{s['total_orders']}` | Filled: `{s['filled']}`\n"
                f"Realised PnL: Rs.`{s['realised_pnl']:+,.2f}`\n"
                f"Margin deployed: Rs.`{s['capital_deployed']:,.0f}`"
            )
        self.execution.alert("*BNF Engine V19 -- MARKET CLOSED*")

    def stop_websocket(self):
        """Close the websocket connection after market hours."""
        if self.ticker:
            try:
                self.ticker.close()
                if self.execution:
                    self.execution.alert("*WEBSOCKET DISCONNECTED* -- Session complete.")
                print("[Engine] WebSocket cleanly disconnected at EOD.")
            except Exception as e:
                print(f"[Engine] Error closing websocket: {e}")

    def update_historical_db(self):
        """Automatically append today's EOD data to SQLite history."""
        if not self.execution:
            return
        self.execution.alert("*EOD DATA UPDATE* -- Initiating SQLite historical sync...")
        try:
            from scripts import update_eod_data
            update_eod_data.main()
            self.execution.alert("*EOD DATA UPDATE* -- SQLite synced successfully.")
        except Exception as e:
            self.execution.alert(f"*EOD DATA ERROR*\nFailed to update SQLite DB: {e}")

    # ── Helper ────────────────────────────────────────────────────

    def _raw_alert(self, msg: str):
        """Send Telegram without needing execution agent."""
        import requests as req
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
            print(f"[ALERT] {msg}")
            return
        for chat_id in TELEGRAM_CHAT_IDS:
            try:
                req.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": msg,
                          "parse_mode": "Markdown"},
                    timeout=5
                )
            except Exception:
                pass

    # ── Scheduler ─────────────────────────────────────────────────

    def weekly_summary(self):
        from agents.report_agent import build_weekly_report
        if not self.execution:
            return
        today     = today_ist()
        from_date = (today - datetime.timedelta(days=6)).isoformat()
        to_date   = today.isoformat()
        period_stats = self.journal.get_period_summary(from_date, to_date)
        trades = self.journal.get_period_trades(from_date, to_date)
        msg = build_weekly_report(period_stats, from_date, to_date,
                                   self.capital, trades=trades)
        if not trades:
            self.execution.alert(msg)
        print(f"[Engine] Weekly summary sent: {from_date} -> {to_date}")

    def monthly_summary(self):
        from agents.report_agent import build_monthly_report
        if not self.execution:
            return
        today = today_ist()
        first_this_month = today.replace(day=1)
        last_month_end   = first_this_month - datetime.timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        from_date = last_month_start.isoformat()
        to_date   = last_month_end.isoformat()
        period_stats = self.journal.get_period_summary(from_date, to_date)
        trades = self.journal.get_period_trades(from_date, to_date)
        msg = build_monthly_report(period_stats, from_date, to_date,
                                    self.capital, trades=trades)
        if not trades:
            self.execution.alert(msg)
        print(f"[Engine] Monthly summary sent: {from_date} -> {to_date}")

    def run(self):
        from config import PAPER_MODE
        mode = "PAPER" if PAPER_MODE else "LIVE"
        print(f"[BNF Engine V19] Starting. Mode: {mode}. "
              f"Capital: Rs.{self.capital:,.0f}")

        # ── IMMEDIATE HOLIDAY/WEEKEND CHECK ──
        today = today_ist()
        if today.weekday() >= 5:
            day_name = "Saturday" if today.weekday() == 5 else "Sunday"
            self._raw_alert(
                f"*WEEKEND -- ENGINE OFF*\n"
                f"Date: `{today}`\n"
                f"Reason: {day_name} -- market closed.\n"
                f"No trades today. Engine will restart on Monday."
            )
            print(f"[Engine] {day_name} detected -- exiting immediately.")
            os._exit(0)

        if self.blackout.is_blackout():
            self._raw_alert(
                f"*BLACKOUT DAY -- ENGINE OFF*\n"
                f"Date: `{today}`\n"
                f"Reason: NSE holiday or RBI policy day.\n"
                f"No trades today. Engine will restart tomorrow."
            )
            print(f"[Engine] Blackout day detected -- exiting immediately.")
            os._exit(0)

        print(f"[Engine] Trading day confirmed: {today} ({today.strftime('%A')})")

        # Schedule all tasks
        schedule.every().day.at("08:30").do(self.auto_token_refresh)
        schedule.every().day.at("08:45").do(self.refresh_calendar)
        schedule.every().day.at("08:45").do(self.preload_cache)
        schedule.every().day.at("09:00").do(self.pre_market)
        schedule.every().day.at("15:20").do(self.force_eod_exit)
        schedule.every().day.at("15:30").do(self.end_of_day)
        schedule.every().day.at("15:45").do(self.stop_websocket)
        schedule.every().day.at("15:45").do(self.update_historical_db)
        schedule.every().monday.at("08:00").do(self.refresh_calendar)
        schedule.every().sunday.at("16:00").do(self.weekly_summary)
        schedule.every().day.at("16:00").do(
            lambda: self.monthly_summary() if today_ist().day == 1 else None
        )

        # Late start / crash recovery
        now = now_ist().time()
        if datetime.time(8, 31) <= now <= datetime.time(9, 14):
            print("[Engine] Late start detected -- running token refresh + cache load now")
            self.auto_token_refresh()
            self.refresh_calendar()
            self.preload_cache()
        elif now >= datetime.time(9, 15):
            print("[Engine] Crash recovery start -- assuming token already valid")
            self._init_kite()
            self.token_ok = True
            self.preload_cache()
            self.pre_market()

        print("[BNFEngine] Entering main execution loop...")
        while True:
            schedule.run_pending()

            # Precision Tick: run at 1 second past every minute
            if now_ist().second == 1:
                self.tick()
                time.sleep(1)  # Prevent double-firing

            time.sleep(0.2)


if __name__ == "__main__":
    BNFEngine().run()
