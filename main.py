"""
BNF Engine v12 — 100% Autonomous + WebSocket Tick Engine + Minervini Dual Strategy
Startup sequence:
  8:30 AM  → Auto token refresh (headless Zerodha login)
  8:45 AM  → Blackout calendar refresh + daily cache preload (260d)
  9:00 AM  → Crash recovery + pre-market scan (S1 + S3)
  9:30 AM  → Trading begins
  Every 60s → Tick: S1+S2+S4 scan + execute + monitor (Kotegawa + Minervini)
  Every 15m → Regime re-check
  Every 30m → Market status re-check (MarketStatusAgent)
  Every Mon → Blackout calendar refresh
  Sunday 06:00 → Fundamental data refresh (screener.in)
  15:30 PM  → Daily summary + journal
"""

import os
import datetime
import time
import schedule
from dotenv import load_dotenv
from kiteconnect import KiteConnect

from auto_login import AutoLogin
from blackout_calendar import BlackoutCalendar
from state_manager import StateManager
from tick_store import TickStore
from daily_cache import DailyCache
from paper_broker import PaperBroker
from data_agent import DataAgent
from scanner_agent import ScannerAgent
from risk_agent import RiskAgent
from journal import Journal
from execution_agent import ExecutionAgent
from kiteconnect import KiteTicker
# [v10] Minervini agents
from fundamental_agent import FundamentalAgent
from stage_agent import StageAgent
from vcp_agent import VCPAgent
from market_status_agent import MarketStatusAgent
from config import (
    KITE_API_KEY, TOTAL_CAPITAL, NIFTY50_TOKEN, INDIA_VIX_TOKEN,
    MAX_OPEN_POSITIONS, S1_MAX_HOLD_DAYS, S2_TIME_STOP_MINUTES,
    PAPER_MODE, VIX_EXTREME_STOP, now_ist, today_ist
)


class BNFEngine:

    def __init__(self):
        # Modules that don't need Kite yet
        self.auto_login = AutoLogin()
        self.blackout   = BlackoutCalendar()
        self.state      = StateManager()
        self.journal    = Journal()

        # Capital resolved at _init_kite() time:
        #   LIVE  → kite.margins() live_balance
        #   PAPER → TOTAL_CAPITAL from .env
        self.capital    = TOTAL_CAPITAL  # placeholder until _init_kite() runs
        self.risk       = RiskAgent(self.capital)

        # Kite + WebSocket + cache initialised after auto-login
        self.kite         = None
        self.ticker       = None   # KiteTicker WebSocket connection
        self.tick_store   = None   # TickStore — in-memory live data
        self.daily_cache  = None   # DailyCache — pre-market historical batch
        self.data         = None
        self.scanner      = None
        self.execution    = None

        # [v10] Minervini agents
        self.fundamental_agent  = None
        self.stage_agent        = None
        self.vcp_agent          = None
        self.market_status_agent = None
        self.market_status      = "BULL"   # [v10] Minervini market timing

        self.regime     = "UNKNOWN"
        self.s1_signals = []
        self.token_ok   = False
        self.scan_count = 0   # [v9] total S1+S2 scan cycles run today
        self._ws_was_fresh = True

    def _fetch_live_capital(self, real_kite: KiteConnect) -> float:
        """
        Fetches deployable cash from Kite margins API.
        Uses live_balance — pure cash, excludes pledged collateral.
        Falls back to TOTAL_CAPITAL from .env if the call fails.

        Called once at _init_kite() time. Fixed for the rest of the day.
        Not re-fetched mid-session — intraday unrealised PnL would otherwise
        cause position sizes to drift while trades are open.

        In PAPER MODE: always returns TOTAL_CAPITAL (fixed baseline needed
        for consistent 30-session comparison).
        """
        from config import PAPER_MODE
        if PAPER_MODE:
            return TOTAL_CAPITAL

        try:
            margins   = real_kite.margins(segment="equity")
            available = float(margins["available"]["live_balance"])
            if available <= 0:
                raise ValueError(f"live_balance={available} — unusable")
            print(f"[Capital] Live balance from Kite: ₹{available:,.0f}")
            return available
        except Exception as e:
            print(f"[Capital] margins() failed: {e} — "
                  f"using .env fallback ₹{TOTAL_CAPITAL:,.0f}")
            return TOTAL_CAPITAL

    def _init_kite(self):
        """
        Called after successful token refresh (8:30 AM daily).

        Sequence:
          1. real_kite — KiteConnect for REST (historical + order placement)
          2. Capital   — live_balance from kite.margins() (live) or .env (paper)
          3. RiskAgent — re-initialised with correct capital for today
          4. DataAgent — loads 100-symbol universe via instruments() REST
          5. TickStore — in-memory store, receives WebSocket ticks
          6. KiteTicker — WebSocket connection; on_ticks wired to tick_store
          7. DailyCache — pre-market historical batch (preload() at 8:45 AM)
          8. DataAgent updated with tick_store + daily_cache references
          9. Order broker — PaperBroker (paper) or real_kite (live)
         10. ScannerAgent + ExecutionAgent wired
        """
        load_dotenv(override=True)
        from config import PAPER_MODE
        access_token = os.getenv("KITE_ACCESS_TOKEN")

        # 1 — real_kite always used for REST (data reads + order placement)
        real_kite = KiteConnect(api_key=KITE_API_KEY)
        real_kite.set_access_token(access_token)

        # 2 — Capital: live Kite balance (live mode) or fixed .env (paper)
        self.capital = self._fetch_live_capital(real_kite)

        # 3 — RiskAgent re-initialised with today's correct capital
        # This ensures daily loss limit (2.5%) and position sizing (1%)
        # are based on actual available funds, not a stale .env value.
        self.risk = RiskAgent(self.capital)

        # 4 — DataAgent loads universe (instruments REST, once)
        self.data = DataAgent(real_kite)

        # Build subscription list: all universe tokens + index + VIX
        sub_tokens = (list(self.data.UNIVERSE.keys()) +
                      [NIFTY50_TOKEN, INDIA_VIX_TOKEN])

        # 5 — TickStore: receives and stores all tick data
        self.tick_store = TickStore()

        # 6 — KiteTicker: WebSocket connection
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

        # Wait up to 15 seconds for first tick
        import time as _time
        for _ in range(30):
            if self.tick_store.is_ready():
                break
            _time.sleep(0.5)
        ws_status = "connected" if self.tick_store.is_ready() else "NOT connected"
        print(f"[BNFEngine] WebSocket: {ws_status} | "
              f"Tokens subscribed: {len(sub_tokens)}")

        # 7 — DailyCache: preload() called separately at 8:45 AM
        self.daily_cache = DailyCache(real_kite)

        # 8 — Inject both caches into DataAgent
        self.data.tick_store  = self.tick_store
        self.data.daily_cache = self.daily_cache

        # 9 — Order broker
        if PAPER_MODE:
            # symbol → token reverse map for tick_store LTP lookups
            symbol_token = {v: k for k, v in self.data.UNIVERSE.items()}
            self.kite = PaperBroker(
                real_kite,
                capital=self.capital,
                tick_store=self.tick_store,
                symbol_token=symbol_token,
            )
            print(f"[BNFEngine] PAPER MODE — orders virtual, "
                  f"capital fixed ₹{self.capital:,.0f}")
        else:
            self.kite = real_kite
            print(f"[BNFEngine] LIVE MODE — real orders, "
                  f"capital ₹{self.capital:,.0f}")

        # 10 — [v10] Minervini agents
        self.fundamental_agent   = FundamentalAgent()
        self.stage_agent         = StageAgent(self.daily_cache)
        self.vcp_agent           = VCPAgent(self.daily_cache)
        self.market_status_agent = MarketStatusAgent(
            self.daily_cache, self.tick_store, NIFTY50_TOKEN
        )

        # 11 — Scanner and ExecutionAgent (with Minervini agents injected)
        self.scanner = ScannerAgent(
            self.data, self.blackout,
            fundamental_agent=self.fundamental_agent,
            stage_agent=self.stage_agent,
            vcp_agent=self.vcp_agent,
            market_status_agent=self.market_status_agent,
        )
        self.execution = ExecutionAgent(
            self.kite, self.risk, self.journal, self.state
        )
        # [v11] Inject agents into execution for master_checklist()
        self.execution._stage_agent       = self.stage_agent
        self.execution._fundamental_agent = self.fundamental_agent
        self.execution._data_universe     = self.data.UNIVERSE

    # ── 8:30 AM: Auto token refresh ───────────────────────────────

    def auto_token_refresh(self):
        print(f"[Engine] Auto token refresh starting...")
        success = self.auto_login.run(alert_fn=self._raw_alert)
        if success:
            self._init_kite()
            self.token_ok = True
        else:
            self.token_ok = False
            # Alert already sent by auto_login.run()

    # ── 8:45 AM: Refresh blackout calendar + load daily cache ────

    def refresh_calendar(self):
        self.blackout.refresh()
        print("[Engine] Blackout calendar refreshed")

        if self.blackout.is_blackout():
            alert_fn = self.execution.alert if self.execution else self._raw_alert
            alert_fn(
                f"📅 *BLACKOUT DAY — ENGINE OFF*\n"
                f"Date: `{today_ist()}`\n"
                f"Reason: NSE holiday or RBI policy day.\n"
                f"No trades today. Engine will restart tomorrow."
            )
            import sys
            sys.exit(0)

    def preload_cache(self):
        """
        Called at 8:45 AM — after token refresh, before market open.
        Fetches 260 days of daily OHLCV for all universe symbols via REST.
        Also batch-fetches circuit limits via quote().
        After this: all scanner calls read from memory. Zero historical REST
        during trading hours.
        """
        if not self.daily_cache or not self.data:
            print("[Engine] preload_cache: daily_cache not ready, skipping")
            return
        print("[Engine] Loading daily cache...")
        ok = self.daily_cache.preload(self.data.UNIVERSE)
        if not ok:
            print("[Engine] Daily cache load failed or partial — REST fallback active")

    # ── 9:00 AM: Pre-market ───────────────────────────────────────

    def pre_market(self):
        if not self.token_ok:
            self._raw_alert("🚨 *ENGINE ABORTED* — Token refresh failed. No trades today.")
            return

        # Crash recovery: reload any open positions from yesterday/today
        self.execution.restore_from_state()

        # Regime detection
        self.regime = self.scanner.detect_regime()

        # WebSocket + cache status
        ws_ok    = self.tick_store and self.tick_store.is_ready()
        cache_ok = self.daily_cache and self.daily_cache.is_loaded()
        universe_count = len(self.data.UNIVERSE) if self.data else 0
        fund_loaded = self.fundamental_agent and self.fundamental_agent.is_loaded()
        self.execution.alert(
            f"🔔 *BNF ENGINE v14 — ARMED*\n"
            f"Date: `{today_ist()}`\n"
            f"Regime: `{self.regime}`\n"
            f"Universe: `{universe_count}` symbols\n"
            f"Cache: `{'✅' if cache_ok else '⚠️'}`  "
            f"WS: `{'✅' if ws_ok else '⚠️'}`  "
            f"Fund: `{'✅' if fund_loaded else '⚠️'}`"
        )

        # Pre-scan S1 candidates
        if self.regime != "CHOP":
            self.s1_signals = self.scanner.scan_s1_ema_divergence(self.regime)
            print(f"[Engine] S1 scan: {len(self.s1_signals)} signals")
        else:
            print("[Engine] CHOP regime — S1 inactive")

        # [v10] S3 SEPA scan (pre-market, ~9:00 AM)
        s3_signals = self.scanner.scan_s3_sepa()
        print(f"[Engine] S3 SEPA scan: {len(s3_signals)} signals")
        if s3_signals:
            # Store for execution at 9:30
            self.state.set_kv("s3_signals", str(len(s3_signals)))
            for sig in s3_signals[:2]:
                if len(self.execution.active_trades) < MAX_OPEN_POSITIONS:
                    self.execution.execute_minervini(sig)

        # [v10] Initial market status
        self._refresh_market_status()

        # Re-arm yesterday's hold orders at the exchange
        self.execution.rearm_s1_exits()

    # ── Every 60 seconds: main tick ──────────────────────────────

    def tick(self):
        if not self.token_ok or not self.execution:
            return
        if self.risk.engine_stopped:
            return

        if self.tick_store:
            now_fresh = self.tick_store.is_fresh()
            if self._ws_was_fresh and not now_fresh:
                self.execution.alert(
                    "🚨 *WEBSOCKET DISCONNECTED*\n"
                    "No ticks in 10+ seconds.\n"
                    "Falling back to REST. Scan accuracy reduced.\n"
                    "Check network / Kite connection."
                )
            elif not self._ws_was_fresh and now_fresh:
                self.execution.alert("✅ *WEBSOCKET RECONNECTED* — live ticks restored.")
            self._ws_was_fresh = now_fresh

        can_trade, reason = self.scanner.is_valid_trading_time()
        if not can_trade:
            if reason.startswith("EXTREME_PANIC") and self.regime != "EXTREME_PANIC":
                self.regime = "EXTREME_PANIC"
                self.s1_signals = []
                print(f"[Engine] EXTREME PANIC — trading halted. VIX: {reason.split('_')[-1]}")
            self.execution.monitor_positions()
            return

        now_t = now_ist().time()

        # Re-check regime every 15 minutes + refresh circuit limits
        if now_ist().minute % 15 == 0:
            new_regime = self.scanner.detect_regime()
            if new_regime != self.regime:
                print(f"[Engine] Regime: {self.regime} → {new_regime}")
                self.regime = new_regime
                if self.regime == "EXTREME_PANIC":
                    self.s1_signals = []
                elif self.regime != "CHOP":
                    self.s1_signals = self.scanner.scan_s1_ema_divergence(
                        self.regime
                    )
            # Save regime to state
            self.state.set_kv("last_regime", self.regime)
            # Refresh circuit breaker limits — infrequent REST, 1 batch call
            if self.daily_cache and self.data:
                self.daily_cache.refresh_circuit_limits(self.data.UNIVERSE)

        # S1: Execute pre-scanned signals 9:30–10:00 AM
        if (datetime.time(9, 30) <= now_t <= datetime.time(10, 0) and
                self.s1_signals and self.regime != "CHOP"):
            for sig in self.s1_signals[:2]:
                if len(self.execution.active_trades) < MAX_OPEN_POSITIONS:
                    self.execution.execute(sig, regime=self.regime)
            self.s1_signals = []

        # S2: Live scan
        s2_signals = self.scanner.scan_s2_overreaction()
        self.scan_count += 1   # [v9] count every S2 scan cycle

        for sig in s2_signals:
            if len(self.execution.active_trades) < MAX_OPEN_POSITIONS:
                s1_open = sum(
                    1 for p in self.risk.open_positions.values()
                    if p.get("strategy") == "S1_EMA_DIVERGENCE"
                )
                if s1_open < 2:
                    self.execution.execute(sig, regime=self.regime)
                    break

        # [v10] S4: Live leadership breakout scan (every tick)
        if datetime.time(9, 30) <= now_t <= datetime.time(15, 0):
            s4_signals = self.scanner.scan_s4_leadership()
            for sig in s4_signals:
                if len(self.execution.active_trades) < MAX_OPEN_POSITIONS:
                    self.execution.execute_minervini(sig)
                    break   # One S4 entry per tick cycle

        # [v10] Refresh market status every 30 minutes
        if now_ist().minute % 30 == 0:
            self._refresh_market_status()

        # Monitor open positions (Kotegawa S1/S2)
        self.execution.monitor_positions()

        # [v10] Monitor Minervini S3/S4 positions
        self.execution.monitor_minervini_positions(
            daily_cache=self.daily_cache,
            tick_store=self.tick_store
        )

    # ── 15:30 PM: End of day ─────────────────────────────────────

    def end_of_day(self):
        if not self.execution:
            return
        self.execution.daily_summary_alert(self.regime,
                                            total_scans=self.scan_count)
        self.scan_count = 0   # [v9] reset daily counter

        if PAPER_MODE and hasattr(self.kite, "get_paper_summary"):
            s = self.kite.get_paper_summary()
            self.execution.alert(
                f"📄 *PAPER SESSION SUMMARY*\n"
                f"Orders: `{s['total_orders']}` | Filled: `{s['filled']}`\n"
                f"Realised PnL: ₹`{s['realised_pnl']:+,.2f}`\n"
                f"Margin deployed: ₹`{s['capital_deployed']:,.0f}`"
            )
        self.execution.alert("🔴 *BNF ENGINE v14 — MARKET CLOSED*")

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

    def _build_period_summary_msg(self, title: str,
                                   from_date: str, to_date: str) -> str:
        """
        [v9] Build a clean Markdown table summary for a date range.
        Used by both weekly_summary() and monthly_summary().
        """
        if not self.journal:
            return f"{title}\n_(journal not ready)_"
        s = self.journal.get_period_summary(from_date, to_date)

        # Top-5 symbols table
        top5_lines = ""
        if s["top5_symbols"]:
            top5_lines = "\n\n*🏆 Top 5 by PnL:*\n"
            top5_lines += "```\n"
            top5_lines += f"{'Symbol':<12} {'PnL':>10}\n"
            top5_lines += f"{'-'*12} {'-'*10}\n"
            for sym, pnl in s["top5_symbols"]:
                top5_lines += f"{sym:<12} ₹{pnl:>+,.0f}\n"
            top5_lines += "```"

        pnl_sign = "📈" if s["gross_pnl"] >= 0 else "📉"
        return (
            f"📅 *{title}*\n"
            f"`{from_date}` → `{to_date}`\n\n"
            f"```\n"
            f"{'Trades':<18} {s['total']:>6}\n"
            f"{'Wins / Losses':<18} {s['wins']:>3} / {s['losses']:<3}\n"
            f"{'Win Rate':<18} {s['win_rate']:>5.1f}%\n"
            f"{'Gross PnL':<18} ₹{s['gross_pnl']:>+,.0f}\n"
            f"{'Best Regime':<18} {s['best_regime']:>8}\n"
            f"{'Worst Regime':<18} {s['worst_regime']:>8}\n"
            f"```"
            f"\n{pnl_sign} Net: ₹`{s['gross_pnl']:+,.0f}`"
            f"{top5_lines}"
        )

    def weekly_summary(self):
        """
        [v9] Every Sunday at 16:00 IST.
        Sends a Markdown table of the past 7 trading days to Telegram.
        """
        if not self.execution:
            return
        today     = today_ist()
        from_date = (today - datetime.timedelta(days=6)).isoformat()
        to_date   = today.isoformat()
        msg = self._build_period_summary_msg(
            "BNF ENGINE v12 — WEEKLY SUMMARY", from_date, to_date
        )
        self.execution.alert(msg)
        print(f"[Engine] Weekly summary sent: {from_date} → {to_date}")

    def monthly_summary(self):
        """
        [v9] 1st of month at 16:00 IST.
        Sends a Markdown table of the prior calendar month to Telegram.
        """
        if not self.execution:
            return
        today = today_ist()
        # Last month: go back to the 1st of the previous month
        first_this_month = today.replace(day=1)
        last_month_end   = first_this_month - datetime.timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        from_date = last_month_start.isoformat()
        to_date   = last_month_end.isoformat()
        msg = self._build_period_summary_msg(
            "BNF ENGINE v12 — MONTHLY SUMMARY", from_date, to_date
        )
        self.execution.alert(msg)
        print(f"[Engine] Monthly summary sent: {from_date} → {to_date}")

    # [v10] Market status refresh
    def _refresh_market_status(self):
        """Called at pre-market and every 30 minutes."""
        if not self.market_status_agent:
            return
        new_status = self.market_status_agent.detect()
        if new_status != self.market_status:
            print(f"[Engine] Market status: {self.market_status} → {new_status}")
            self.market_status = new_status
        self.state.set_kv("market_status", new_status)

    # [v10] Weekly fundamental refresh
    def refresh_fundamentals(self):
        """Called Sunday 06:00 AM. Fetches screener.in data for all symbols."""
        if not self.fundamental_agent or not self.data:
            return
        print("[Engine] Fundamental refresh starting...")
        symbols = list(self.data.UNIVERSE.values())
        self.fundamental_agent.preload(symbols)

    def run(self):
        from config import PAPER_MODE
        mode = "PAPER" if PAPER_MODE else "LIVE"
        print(f"[BNF ENGINE v12] Starting. Mode: {mode}. "
              f"Capital: ₹{self.capital:,.0f}")

        # Schedule all tasks
        schedule.every().day.at("08:30").do(self.auto_token_refresh)
        schedule.every().day.at("08:45").do(self.refresh_calendar)
        schedule.every().day.at("08:45").do(self.preload_cache)
        schedule.every().day.at("09:00").do(self.pre_market)
        schedule.every(1).minutes.do(self.tick)
        schedule.every().day.at("15:30").do(self.end_of_day)
        schedule.every().monday.at("08:00").do(self.refresh_calendar)
        # [v9] Weekly summary — every Sunday at 16:00 IST
        schedule.every().sunday.at("16:00").do(self.weekly_summary)
        # [v9] Monthly summary — every day at 16:00; fires only on 1st of month
        schedule.every().day.at("16:00").do(
            lambda: self.monthly_summary() if today_ist().day == 1 else None
        )
        # [v10] Sunday 06:00 — weekly fundamental data refresh from screener.in
        schedule.every().sunday.at("06:00").do(self.refresh_fundamentals)

        # If engine starts after 8:30 but before market open (recovery scenario)
        now = now_ist().time()
        if datetime.time(8, 31) <= now <= datetime.time(9, 14):
            print("[Engine] Late start detected — running token refresh + cache load now")
            self.auto_token_refresh()
            self.refresh_calendar()
            self.preload_cache()
        elif now >= datetime.time(9, 15):
            print("[Engine] Crash recovery start — assuming token already valid")
            self._init_kite()
            self.token_ok = True
            # Cache may not be loaded on mid-session crash recovery —
            # preload it now so scans are fast after restart
            self.preload_cache()
            self.pre_market()

        while True:
            schedule.run_pending()
            time.sleep(30)


if __name__ == "__main__":
    BNFEngine().run()
