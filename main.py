"""
BNF Engine v4 — 100% Autonomous
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
from data_agent import DataAgent
from scanner_agent import ScannerAgent
from risk_agent import RiskAgent
from journal import Journal
from execution_agent import ExecutionAgent
from config import (
    KITE_API_KEY, TOTAL_CAPITAL,
    MAX_OPEN_POSITIONS, S1_MAX_HOLD_DAYS, S2_TIME_STOP_MINUTES
)


class BNFEngine:

    def __init__(self):
        # Modules that don't need Kite yet
        self.auto_login = AutoLogin()
        self.blackout   = BlackoutCalendar()
        self.state      = StateManager()
        self.journal    = Journal()
        self.risk       = RiskAgent(TOTAL_CAPITAL)

        # Kite initialised after auto-login
        self.kite       = None
        self.data       = None
        self.scanner    = None
        self.execution  = None

        self.regime     = "UNKNOWN"
        self.s1_signals = []
        self.token_ok   = False

    def _init_kite(self):
        """Called after successful token refresh."""
        load_dotenv(override=True)
        from config import KITE_ACCESS_TOKEN
        self.kite = KiteConnect(api_key=KITE_API_KEY)
        self.kite.set_access_token(KITE_ACCESS_TOKEN)
        self.data      = DataAgent(self.kite)
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

    # ── 8:45 AM: Refresh blackout calendar ───────────────────────

    def refresh_calendar(self):
        if self.execution:
            self.blackout.refresh(alert_fn=self.execution.alert)
        else:
            self.blackout.refresh()
        print("[Engine] Blackout calendar refreshed")

    # ── 9:00 AM: Pre-market ───────────────────────────────────────

    def pre_market(self):
        if not self.token_ok:
            self._raw_alert("🚨 *ENGINE ABORTED* — Token refresh failed. No trades today.")
            return

        # Crash recovery: reload any open positions from yesterday/today
        self.execution.restore_from_state()

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
            return

        now_t = datetime.datetime.now().time()

        # Re-check regime every 15 minutes
        if datetime.datetime.now().minute % 15 == 0:
            new_regime = self.scanner.detect_regime()
            if new_regime != self.regime:
                self.execution.alert(
                    f"⚡ *REGIME CHANGE*: `{self.regime}` → `{new_regime}`"
                )
                self.regime = new_regime
                if self.regime != "CHOP":
                    self.s1_signals = self.scanner.scan_s1_ema_divergence(
                        self.regime
                    )
            # Save regime to state
            self.state.set_kv("last_regime", self.regime)

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
        self.execution.alert("🔴 *BNF ENGINE v4 — MARKET CLOSED*")

    # ── Helper ────────────────────────────────────────────────────

    def _raw_alert(self, msg: str):
        """Send Telegram without needing execution agent."""
        import requests as req
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print(f"[ALERT] {msg}")
            return
        try:
            req.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                      "parse_mode": "Markdown"},
                timeout=5
            )
        except Exception:
            pass

    # ── Scheduler ─────────────────────────────────────────────────

    def run(self):
        print(f"[BNF ENGINE v4] Starting. Capital: ₹{TOTAL_CAPITAL:,.0f}")

        # Schedule all tasks
        schedule.every().day.at("08:30").do(self.auto_token_refresh)
        schedule.every().day.at("08:45").do(self.refresh_calendar)
        schedule.every().day.at("09:00").do(self.pre_market)
        schedule.every(1).minutes.do(self.tick)
        schedule.every().day.at("15:30").do(self.end_of_day)
        schedule.every().monday.at("08:00").do(self.refresh_calendar)

        # If engine starts after 8:30 but before market open (recovery scenario)
        now = datetime.datetime.now().time()
        if datetime.time(8, 31) <= now <= datetime.time(9, 14):
            print("[Engine] Late start detected — running token refresh now")
            self.auto_token_refresh()
            self.refresh_calendar()
        elif now >= datetime.time(9, 15):
            print("[Engine] Crash recovery start — assuming token already valid")
            self._init_kite()
            self.token_ok = True
            self.pre_market()

        while True:
            schedule.run_pending()
            time.sleep(30)


if __name__ == "__main__":
    BNFEngine().run()
