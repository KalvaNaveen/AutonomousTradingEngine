"""
BNF Engine v6 — 100% Autonomous + WebSocket Tick Engine
Startup sequence:
  8:30 AM  → Auto token refresh (headless Zerodha login)
  8:45 AM  → Blackout calendar refresh
  9:00 AM  → Crash recovery + pre-market scan
  9:30 AM  → Trading begins
  Every 60s → Tick: scan + execute + monitor
  Every 15m → Regime re-check
  Every Mon → Blackout calendar refresh
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
from config import (
    KITE_API_KEY, TOTAL_CAPITAL, NIFTY50_TOKEN, INDIA_VIX_TOKEN,
    MAX_OPEN_POSITIONS, S1_MAX_HOLD_DAYS, S2_TIME_STOP_MINUTES
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

        self.regime     = "UNKNOWN"
        self.s1_signals = []
        self.token_ok   = False

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
        from config import KITE_ACCESS_TOKEN, PAPER_MODE

        # 1 — real_kite always used for REST (data reads + order placement)
        real_kite = KiteConnect(api_key=KITE_API_KEY)
        real_kite.set_access_token(KITE_ACCESS_TOKEN)

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
        self.ticker = KiteTicker(KITE_API_KEY, KITE_ACCESS_TOKEN)
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

        # 10 — Scanner and ExecutionAgent
        self.scanner   = ScannerAgent(self.data, self.blackout)
        self.execution = ExecutionAgent(
            self.kite, self.risk, self.journal, self.state
        )

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
        if self.execution:
            self.blackout.refresh(alert_fn=self.execution.alert)
        else:
            self.blackout.refresh()
        print("[Engine] Blackout calendar refreshed")

    def preload_cache(self):
        """
        Called at 8:45 AM — after token refresh, before market open.
        Fetches 75 days of daily OHLCV for all 100 universe symbols via REST.
        Also batch-fetches circuit limits via quote().
        After this: all scanner calls read from memory. Zero historical REST
        during trading hours.
        ~35 seconds total. Must complete before pre_market() at 9:00 AM.
        """
        if not self.daily_cache or not self.data:
            print("[Engine] preload_cache: daily_cache not ready, skipping")
            return
        alert_fn = self.execution.alert if self.execution else self._raw_alert
        alert_fn("📦 *Loading daily cache...*\n~35 seconds. Stand by.")
        ok = self.daily_cache.preload(self.data.UNIVERSE, alert_fn=alert_fn)
        if not ok:
            alert_fn("⚠️ *Daily cache load failed or partial.*\n"
                     "Engine will fall back to REST per symbol — scans will be slower.")

    # ── 9:00 AM: Pre-market ───────────────────────────────────────

    def pre_market(self):
        if not self.token_ok:
            self._raw_alert("🚨 *ENGINE ABORTED* — Token refresh failed. No trades today.")
            return

        # Crash recovery: reload any open positions from yesterday/today
        self.execution.restore_from_state()

        # WebSocket + cache status
        ws_ok    = self.tick_store and self.tick_store.is_ready()
        cache_ok = self.daily_cache and self.daily_cache.is_loaded()
        self.execution.alert(
            f"📡 *Engine ready*\n"
            f"WebSocket: `{'✅ live' if ws_ok else '⚠️ not connected'}`\n"
            f"Daily cache: `{'✅ loaded' if cache_ok else '⚠️ not loaded — REST fallback active'}`"
        )

        # Regime detection
        self.regime = self.scanner.detect_regime()
        self.execution.alert(f"📍 *Regime: `{self.regime}`*")

        # Pre-scan S1 candidates
        if self.regime != "CHOP":
            self.s1_signals = self.scanner.scan_s1_ema_divergence(self.regime)
            if self.s1_signals:
                lines = "\n".join([
                    f"• `{s['symbol']}` Dev:{s['deviation_pct']}% "
                    f"RSI:{s['rsi']} RVOL:{s['rvol']}"
                    for s in self.s1_signals[:5]
                ])
                self.execution.alert(
                    f"🔍 *S1 WATCHLIST ({len(self.s1_signals)})*\n{lines}"
                )
            else:
                self.execution.alert("🔍 No S1 setups. S2 only.")
        else:
            self.execution.alert("⏸ CHOP regime. S1 inactive.")

    # ── Every 60 seconds: main tick ──────────────────────────────

    def tick(self):
        if not self.token_ok or not self.execution:
            return
        if self.risk.engine_stopped:
            return

        can_trade, reason = self.scanner.is_valid_trading_time()
        if not can_trade:
            # For EXTREME_PANIC specifically: alert once per regime change,
            # then continue to monitor_positions() so open trades are still
            # managed. All other non-trade reasons skip everything.
            if reason.startswith("EXTREME_PANIC") and self.regime != "EXTREME_PANIC":
                self.regime = "EXTREME_PANIC"
                self.s1_signals = []
                self.execution.alert(
                    f"🚨 *EXTREME PANIC — TRADING HALTED*\n"
                    f"VIX: `{reason.split('_')[-1]}`\n"
                    f"No new entries. Open positions monitored normally.\n"
                    f"Engine resumes when VIX drops below `{VIX_EXTREME_STOP}`."
                )
            self.execution.monitor_positions()
            return

        now_t = now_ist().time()

        # Re-check regime every 15 minutes + refresh circuit limits
        if now_ist().minute % 15 == 0:
            new_regime = self.scanner.detect_regime()
            if new_regime != self.regime:
                prev_regime = self.regime
                self.execution.alert(
                    f"⚡ *REGIME CHANGE*: `{self.regime}` → `{new_regime}`"
                )
                self.regime = new_regime
                if self.regime == "EXTREME_PANIC":
                    self.s1_signals = []
                elif self.regime != "CHOP":
                    self.s1_signals = self.scanner.scan_s1_ema_divergence(
                        self.regime
                    )
                    # Notify only when specifically recovering from EXTREME_PANIC
                    if prev_regime == "EXTREME_PANIC":
                        self.execution.alert(
                            f"✅ *EXTREME PANIC CLEARED*\n"
                            f"VIX back below `{VIX_EXTREME_STOP}` — "
                            f"trading resumed. New regime: `{self.regime}`."
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
        for sig in s2_signals:
            if len(self.execution.active_trades) < MAX_OPEN_POSITIONS:
                s1_open = sum(
                    1 for p in self.risk.open_positions.values()
                    if p.get("strategy") == "S1_EMA_DIVERGENCE"
                )
                if s1_open < 2:
                    self.execution.execute(sig, regime=self.regime)
                    break

        # Monitor open positions
        self.execution.monitor_positions()

    # ── 15:30 PM: End of day ─────────────────────────────────────

    def end_of_day(self):
        if not self.execution:
            return
        self.execution.daily_summary_alert(self.regime)

        # Tomorrow's watchlist
        tmr = self.scanner.scan_s1_ema_divergence(self.regime)
        if tmr:
            lines = "\n".join([
                f"• `{s['symbol']}` Dev:{s['deviation_pct']}%"
                for s in tmr[:5]
            ])
            self.execution.alert(f"🌙 *TOMORROW S1 WATCHLIST*\n{lines}")
        if PAPER_MODE and hasattr(self.kite, "get_paper_summary"):
            s = self.kite.get_paper_summary()
            self.execution.alert(
                f"📄 *PAPER SESSION SUMMARY*\n"
                f"Orders: `{s['total_orders']}` | Filled: `{s['filled']}`\n"
                f"Realised PnL: ₹`{s['realised_pnl']:+,.2f}`\n"
                f"Margin deployed: ₹`{s['capital_deployed']:,.0f}`"
            )
        self.execution.alert("🔴 *BNF ENGINE v6 — MARKET CLOSED*")

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

    def run(self):
        mode = "PAPER" if PAPER_MODE else "LIVE"
        print(f"[BNF ENGINE v6] Starting. Mode: {mode}. "
              f"Capital: ₹{self.capital:,.0f}")

        # Schedule all tasks
        schedule.every().day.at("08:30").do(self.auto_token_refresh)
        schedule.every().day.at("08:45").do(self.refresh_calendar)
        schedule.every().day.at("08:45").do(self.preload_cache)
        schedule.every().day.at("09:00").do(self.pre_market)
        schedule.every(1).minutes.do(self.tick)
        schedule.every().day.at("15:30").do(self.end_of_day)
        schedule.every().monday.at("08:00").do(self.refresh_calendar)

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
