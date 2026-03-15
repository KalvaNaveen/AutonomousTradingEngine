import datetime
import requests
from kiteconnect import KiteConnect
from risk_agent import RiskAgent
from journal import Journal
from state_manager import StateManager
from fill_monitor import FillMonitor
from config import *


class ExecutionAgent:

    def __init__(self, kite: KiteConnect, risk: RiskAgent,
                 journal: Journal, state: StateManager):
        self.kite    = kite
        self.risk    = risk
        self.journal = journal
        self.state   = state
        self.fill_monitor = FillMonitor(kite, state)
        self.active_trades = {}
        self.tg_base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    def restore_from_state(self):
        """
        Called at startup after crash.
        Reloads open positions from SQLite and resumes monitoring.
        """
        open_positions = self.state.load_open_positions()
        if not open_positions:
            return
        for trade in open_positions:
            oid = trade["entry_oid"]
            self.active_trades[oid] = trade
            self.risk.register_open(oid, {
                "symbol":      trade["symbol"],
                "entry_price": trade["entry_price"],
                "qty":         trade["qty"],
                "strategy":    trade["strategy"],
            })
        self.alert(
            f"♻️ *CRASH RECOVERY*\n"
            f"Restored `{len(open_positions)}` open position(s) from state.\n"
            + "\n".join([f"• `{t['symbol']}` {t['strategy']}"
                          for t in open_positions])
        )
        print(f"[ExecutionAgent] Restored {len(open_positions)} positions from state")

    def execute(self, signal: dict, regime: str = "UNKNOWN") -> bool:
        signal["regime"] = regime
        approved, reason = self.risk.approve_trade(signal)
        if not approved:
            print(f"[Exec] REJECTED {signal['symbol']}: {reason}")
            return False

        qty = self.risk.calculate_position_size(
            signal["entry_price"], signal["stop_price"]
        )
        if qty == 0:
            return False

        product = (self.kite.PRODUCT_CNC if signal["product"] == "CNC"
                   else self.kite.PRODUCT_MIS)

        try:
            entry_oid = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NSE,
                tradingsymbol=signal["symbol"],
                transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                quantity=qty,
                product=product,
                order_type=self.kite.ORDER_TYPE_LIMIT,
                price=round(signal["entry_price"] * 1.002, 1),
                validity=self.kite.VALIDITY_DAY
            )
        except Exception as e:
            self.alert(f"❌ ORDER FAILED: `{signal['symbol']}`\n`{e}`")
            return False

        # SL and target orders are placed ONLY after entry fill confirms.
        # Reason: placing sell orders before the buy fills creates a race.
        # For MIS: the SL-M could trigger before entry fills → naked short.
        # For CNC: Zerodha rejects sell orders before stock arrives in demat.
        # _monitor_fill() runs in a background thread and places them on fill.
        # sl_oid / partial_oid / target_oid are None until then.

        partial_qty   = max(1, qty // 2)
        remaining_qty = qty - partial_qty
        partial_price = signal.get("partial_target") or signal.get("partial_target_1")

        now   = now_ist()
        trade = {
            **signal,
            "qty":            qty,
            "partial_qty":    partial_qty,
            "remaining_qty":  remaining_qty,
            "partial_filled": False,
            "entry_oid":      entry_oid,
            "sl_oid":         None,     # filled in by _monitor_fill
            "partial_oid":    None,     # filled in by _monitor_fill
            "target_oid":     None,     # filled in by _monitor_fill
            "entry_time":     now,
            "entry_date":     today_ist(),
            "regime":         regime,
        }

        # Persist to SQLite immediately
        self.state.save(entry_oid, trade)
        self.active_trades[entry_oid] = trade
        self.risk.register_open(entry_oid, {
            "symbol":      signal["symbol"],
            "entry_price": signal["entry_price"],
            "qty":         qty,
            "strategy":    signal["strategy"],
        })

        # Background thread: polls until entry fills, then places SL + target
        import threading
        threading.Thread(
            target=self._monitor_fill,
            args=(entry_oid,),
            daemon=True
        ).start()

        rr = ((signal["target_price"] - signal["entry_price"]) /
              max(signal["entry_price"] - signal["stop_price"], 0.01))

        self.alert(
            f"🟢 *ENTRY — BNF v6*\n"
            f"`{signal['symbol']}` | `{regime}` | `{signal['strategy']}`\n"
            f"Entry: ₹`{signal['entry_price']:.2f}` | Qty: `{qty}`\n"
            f"Partial: ₹`{partial_price:.2f if partial_price else 0}`\n"
            f"Target: ₹`{signal['target_price']:.2f}` | "
            f"Stop: ₹`{signal['stop_price']:.2f}`\n"
            f"R:R `{rr:.2f}` | RVOL `{signal.get('rvol', 0):.2f}`\n"
            f"SL + target placed on fill confirmation."
        )
        return True

    def _monitor_fill(self, entry_oid: str):
        """Runs in background thread. Adjusts orders if partial fill."""
        trade = self.active_trades.get(entry_oid)
        if not trade:
            return
        updated_trade = self.fill_monitor.wait_for_fill(trade, self.alert)
        if updated_trade.get("entry_cancelled"):
            # Remove cancelled trade
            self.active_trades.pop(entry_oid, None)
            self.risk.open_positions.pop(entry_oid, None)
            self.state.close(entry_oid)
        else:
            self.active_trades[entry_oid] = updated_trade

    def monitor_positions(self):
        now = now_ist()
        for oid, trade in list(self.active_trades.items()):

            # Check if partial fill has completed
            if not trade.get("partial_filled"):
                if self.fill_monitor.check_partial_exit_filled(trade):
                    trade["partial_filled"] = True
                    self.state.mark_partial_filled(oid, trade["remaining_qty"])

            if trade["strategy"] == "S2_OVERREACTION":
                elapsed = (now - trade["entry_time"]).seconds / 60
                if elapsed >= S2_TIME_STOP_MINUTES:
                    self._force_exit(oid, trade, "TIME_STOP_45MIN")
                    continue
                if now.time() >= datetime.time(15, 14):
                    self._force_exit(oid, trade, "EOD_MIS_SQUAREOFF")
                    continue

            if trade["strategy"] == "S1_EMA_DIVERGENCE":
                days = (today_ist() - trade["entry_date"].date()
                        if hasattr(trade["entry_date"], "date")
                        else today_ist() - trade["entry_date"]).days
                if days >= S1_MAX_HOLD_DAYS:
                    self._force_exit(oid, trade, f"MAX_HOLD_{S1_MAX_HOLD_DAYS}D")
                    continue

                # S1 stop is checked on daily close at 3:15 PM only.
                # Exchange SL-M is intentionally not placed for CNC swing —
                # intraday wicks would stop out valid multi-day setups.
                if now.time() >= datetime.time(15, 14):
                    # Get today's closing price via kite (paper or live)
                    close_px = 0.0
                    try:
                        q = self.kite.quote([f"NSE:{trade['symbol']}"])
                        close_px = q.get(
                            f"NSE:{trade['symbol']}", {}
                        ).get("last_price", 0.0)
                    except Exception:
                        pass

                    if close_px > 0 and close_px <= trade["stop_price"]:
                        self._force_exit(oid, trade, "S1_DAILY_CLOSE_STOP")
                        continue

    def _force_exit(self, oid: str, trade: dict, reason: str):
        for o, v in [
            (trade.get("sl_oid"),      self.kite.VARIETY_SL),
            (trade.get("partial_oid"), self.kite.VARIETY_REGULAR),
            (trade.get("target_oid"),  self.kite.VARIETY_REGULAR),
        ]:
            if o:
                try:
                    self.kite.cancel_order(v, o)
                except Exception:
                    pass

        product  = (self.kite.PRODUCT_CNC if trade["product"] == "CNC"
                    else self.kite.PRODUCT_MIS)
        exit_qty = trade.get("remaining_qty", trade["qty"])

        try:
            self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NSE,
                tradingsymbol=trade["symbol"],
                transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                quantity=exit_qty, product=product,
                order_type=self.kite.ORDER_TYPE_MARKET,
                validity=self.kite.VALIDITY_DAY
            )
        except Exception as e:
            self.alert(f"⚠️ FORCE EXIT FAILED: `{trade['symbol']}` — {e}")
            return

        pnl = self.risk.close_position(oid, trade["entry_price"])
        self.state.close(oid)
        self.journal.log_trade({
            **trade,
            "full_exit_price": trade["entry_price"],
            "pnl":             pnl,
            "exit_reason":     reason,
            "exit_time":       now_ist(),
            "daily_pnl_after": self.risk.daily_pnl,
        })
        streak = (f"\n⚠️ Streak: `{self.risk.consecutive_losses}/3`"
                  if self.risk.consecutive_losses > 0 else "")
        self.alert(
            f"🔴 *FORCE EXIT*\n"
            f"`{trade['symbol']}` | `{reason}`\n"
            f"Est. PnL: ₹`{pnl:+.0f}`{streak}"
        )
        self.active_trades.pop(oid, None)

    def daily_summary_alert(self, regime: str):
        stats = self.risk.get_daily_stats()
        self.journal.log_daily_summary(
            stats, regime,
            self.risk.engine_stopped, self.risk.stop_reason
        )
        regime_data = self.journal.win_rate_by_regime()
        regime_lines = ""
        if regime_data:
            regime_lines = "\n*All-time by regime:*\n" + "\n".join([
                f"• `{r[0]}`: {r[2]}% WR | ₹{r[3]} avg | {r[1]} trades"
                for r in regime_data
            ])
        self.alert(
            f"📊 *BNF ENGINE v6 — DAILY SUMMARY*\n"
            f"`{today_ist()}` | Regime: `{regime}`\n"
            f"Trades: `{stats['total']}` | "
            f"W:`{stats['wins']}` L:`{stats['losses']}` | "
            f"WR:`{stats['win_rate']:.1f}%`\n"
            f"PnL: ₹`{stats['gross_pnl']:+,.0f}`\n"
            f"Streak: `{stats['loss_streak']}/3`"
            f"{regime_lines}"
        )

    def alert(self, msg: str):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
            print(f"[ALERT] {msg}")
            return
        for chat_id in TELEGRAM_CHAT_IDS:
            try:
                requests.post(
                    f"{self.tg_base}/sendMessage",
                    json={"chat_id": chat_id, "text": msg,
                          "parse_mode": "Markdown"},
                    timeout=5
                )
            except Exception:
                pass
