"""
ExecutionAgent V18 — Adaptive Intraday System (MIS Only)

Handles S6 (Trend Breakout Short) and S7 (Mean Reversion Long).
All swing strategies (S1-S5) and Minervini logic permanently removed.
"""

import datetime
import requests
from kiteconnect import KiteConnect
from agents.risk_agent import RiskAgent
from core.journal import Journal
from core.state_manager import StateManager
from scripts.fill_monitor import FillMonitor
from config import *


class ExecutionAgent:

    def __init__(self, kite: KiteConnect, risk: RiskAgent,
                 journal: Journal, state: StateManager):
        self.kite    = kite
        self.risk    = risk
        self.journal = journal
        self.state   = state
        self.fill_monitor = FillMonitor(kite, state, alert_fn=self.alert)
        self.active_trades = {}
        self.tick_store = None
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
                "is_short":    trade.get("is_short", False),
                "product":     trade.get("product", "MIS"),
            })
        self.alert(
            f"*CRASH RECOVERY*\n"
            f"Restored `{len(open_positions)}` open position(s) from state.\n"
            + "\n".join([f"  `{t['symbol']}` {t['strategy']}"
                          for t in open_positions])
        )
        print(f"[ExecutionAgent] Restored {len(open_positions)} positions from state")

    def execute(self, signal: dict, regime: str = "UNKNOWN") -> bool:
        signal["regime"] = regime

        # ── DUPLICATE SYMBOL GUARD ─────────────────────────────────
        sym = signal.get("symbol", "")
        for t in self.active_trades.values():
            if t.get("symbol") == sym:
                print(f"[Exec] REJECTED {sym}: Already holding an open position")
                return False

        approved, reason = self.risk.approve_trade(signal)
        if not approved:
            print(f"[Exec] REJECTED {signal['symbol']}: {reason}")
            return False

        qty = self.risk.calculate_position_size(
            signal["entry_price"], signal["stop_price"],
            regime=regime,
            strategy=signal.get("strategy", ""),
            symbol=signal.get("symbol", "")
        )
        if qty == 0:
            return False

        # All V18 strategies are MIS
        product = self.kite.PRODUCT_MIS

        # Short selling support
        is_short = signal.get("is_short", False)
        txn_type = (self.kite.TRANSACTION_TYPE_SELL if is_short
                    else self.kite.TRANSACTION_TYPE_BUY)

        # Route through Go executor if available, else Python
        go_bridge = getattr(self, '_go_bridge', None)
        
        strat = signal.get("strategy", "")
        # Momentum breakouts & Macro news must be MARKET orders to guarantee fill on volatile push.
        is_momentum = any(s in strat for s in ["S3", "S6", "S8", "MACRO"])
        
        go_order_type = "MARKET" if is_momentum else "LIMIT"
        py_order_type = self.kite.ORDER_TYPE_MARKET if is_momentum else self.kite.ORDER_TYPE_LIMIT
        py_order_price = 0 if is_momentum else round(signal["entry_price"] * (0.998 if is_short else 1.002), 1)

        if go_bridge and go_bridge.is_connected():
            try:
                go_signal = {
                    "action": "SELL" if is_short else "BUY",
                    "symbol": signal["symbol"],
                    "exchange": "NSE",
                    "qty": qty,
                    "price": py_order_price,
                    "trigger_price": 0,
                    "order_type": go_order_type,
                    "product": "MIS",
                    "validity": "DAY",
                    "tag": signal.get("strategy", "BNF"),
                }
                result = go_bridge.send_order(go_signal)
                if result.get("status") == "OK":
                    entry_oid = result.get("order_id", "GO_" + signal["symbol"])
                    latency = result.get("latency_us", 0)
                    print(f"[Exec] GO EXECUTOR: {signal['symbol']} order placed in {latency}us")
                else:
                    raise Exception(f"Go executor error: {result.get('message')}")
            except Exception as e:
                print(f"[Exec] Go bridge failed ({e}), falling back to Python...")
                try:
                    entry_oid = self.kite.place_order(
                        variety=self.kite.VARIETY_REGULAR,
                        exchange=self.kite.EXCHANGE_NSE,
                        tradingsymbol=signal["symbol"],
                        transaction_type=txn_type,
                        quantity=qty,
                        product=product,
                        order_type=py_order_type,
                        price=py_order_price,
                        validity=self.kite.VALIDITY_DAY
                    )
                except Exception as e2:
                    self.alert(f"ORDER FAILED: `{signal['symbol']}`\n`{e2}`")
                    return False
        else:
            try:
                entry_oid = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NSE,
                    tradingsymbol=signal["symbol"],
                    transaction_type=txn_type,
                    quantity=qty,
                    product=product,
                    order_type=py_order_type,
                    price=py_order_price,
                    validity=self.kite.VALIDITY_DAY
                )
            except Exception as e:
                self.alert(f"ORDER FAILED: `{signal['symbol']}`\n`{e}`")
                return False

        # After main futures leg placed successfully:
        if signal.get("is_two_leg") and signal.get("spot_leg"):
            spot_info = signal["spot_leg"]
            spot_txn = (self.kite.TRANSACTION_TYPE_BUY
                        if spot_info["direction"] == "BUY"
                        else self.kite.TRANSACTION_TYPE_SELL)
            spot_exchange = spot_info.get("exchange", "NSE")
            spot_symbol   = spot_info.get("symbol", "")
            # Only NIFTYBEES or equivalent ETF — not the index itself
            if "SPOT" in spot_symbol:
                pass  # Can't trade an index directly
            else:
                try:
                    self.kite.place_order(
                        variety=self.kite.VARIETY_REGULAR,
                        exchange=spot_exchange,
                        tradingsymbol=spot_symbol,
                        transaction_type=spot_txn,
                        quantity=qty,
                        product=product,
                        order_type=self.kite.ORDER_TYPE_MARKET,
                        validity=self.kite.VALIDITY_DAY
                    )
                except Exception as e:
                    # If spot leg fails, cancel futures leg immediately
                    try: self.kite.cancel_order(self.kite.VARIETY_REGULAR, entry_oid)
                    except: pass
                    self.alert(f"S4 SPOT LEG FAILED — futures cancelled: {e}")
                    return False

        now   = now_ist()
        trade = {
            **signal,
            "qty":            qty,
            "remaining_qty":  qty,
            "partial_filled": False,
            "entry_oid":      entry_oid,
            "sl_oid":         None,
            "partial_oid":    None,
            "target_oid":     None,
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
            "is_short":    is_short,
            "product":     "MIS",
        })

        # Background thread: polls until entry fills, then places SL + target
        import threading
        threading.Thread(
            target=self._monitor_fill,
            args=(entry_oid,),
            daemon=True
        ).start()

        # Calculate R:R for alert
        entry_px = signal["entry_price"]
        stop_px  = signal["stop_price"]
        tgt_px   = signal["target_price"]
        if is_short:
            risk_pts = abs(stop_px - entry_px)
            reward_pts = abs(entry_px - tgt_px)
        else:
            risk_pts = abs(entry_px - stop_px)
            reward_pts = abs(tgt_px - entry_px)
        rr = reward_pts / max(risk_pts, 0.01)

        strat_label = signal["strategy"]
        direction = "SHORT" if is_short else "LONG"
        self.alert(
            f"*EXECUTED {strat_label} ({direction}) -- Qty:{qty} | R:R {rr:.2f}*\n"
            f"`{signal['symbol']}` | `{regime}`\n"
            f"Entry: Rs.`{entry_px:.2f}` | Qty: `{qty}`\n"
            f"Target: Rs.`{tgt_px:.2f}` | "
            f"Stop: Rs.`{stop_px:.2f}`\n"
            f"VWAP: Rs.`{signal.get('vwap', 0):.2f}` | RVOL `{signal.get('rvol', 0):.2f}`\n"
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
            self.active_trades.pop(entry_oid, None)
            self.risk.open_positions.pop(entry_oid, None)
            self.state.close(entry_oid)
        else:
            self.active_trades[entry_oid] = updated_trade

    def monitor_positions(self, daily_cache=None, tick_store=None, current_regime=None):
        """
        Monitor all open MIS positions.
        Handles:
          - MIS EOD square-off at 15:14
          - RSI-based dynamic exits (S6/S7)
          - Structural Regime Shift Evacuations
          - Stop/Target hit checks
        """
        now = now_ist()

        # Pre-fetch all current orders ONCE per tick
        try:
            orders = self.kite.orders()
            order_map = {str(o.get("order_id")): o for o in orders}
        except Exception as e:
            print(f"[Execution] Orders pre-fetch failed: {e}")
            order_map = None

        for oid, trade in list(self.active_trades.items()):
            strat = trade.get("strategy", "")

            # Check partial fill completion
            if not trade.get("partial_filled"):
                if self.fill_monitor.check_partial_exit_filled(trade, order_map):
                    trade["partial_filled"] = True
                    self.state.mark_partial_filled(oid, trade["remaining_qty"])
                    
                    # Book partial P&L
                    partial_qty = trade.get("partial_qty", 0)
                    partial_price = trade.get("partial_target") or trade.get("partial_target_1")
                    if partial_qty > 0 and partial_price:
                        is_short = trade.get("is_short", False)
                        if is_short:
                            pt_gross = (trade["entry_price"] - partial_price) * partial_qty
                            buy_val, sell_val = partial_price * partial_qty, trade["entry_price"] * partial_qty
                        else:
                            pt_gross = (partial_price - trade["entry_price"]) * partial_qty
                            buy_val, sell_val = trade["entry_price"] * partial_qty, partial_price * partial_qty
                            
                        from core.charges import compute_trade_charges
                        charge_res = compute_trade_charges(buy_val, sell_val, trade.get("product", "MIS"))
                        pt_pnl = pt_gross - charge_res["total"]
                        
                        trade["realised_pnl"] = pt_pnl
                        if oid in self.risk.open_positions:
                            self.risk.open_positions[oid]["realised_pnl"] = pt_pnl
                            # CRITICAL: Update qty to remaining so close_position()
                            # calculates final leg P&L only on shares still held.
                            # Without this: close_position uses original qty → double-count.
                            self.risk.open_positions[oid]["qty"] = trade["remaining_qty"]
                        self.risk.daily_pnl += pt_pnl
                        self.risk.weekly_pnl += pt_pnl
                        self.alert(f"💰 *PARTIAL BOOKED* `{trade['symbol']}`\nLocked PnL: Rs.`{pt_pnl:.0f}`")

            # ── MIS EOD Square-off ──
            from config import EOD_SQUAREOFF_TIME, PREEMPTIVE_EXIT_TIME
            sq_h, sq_m = map(int, EOD_SQUAREOFF_TIME.split(":"))
            if now.time() >= datetime.time(sq_h, sq_m):
                self._force_exit(oid, trade, "MIS_EOD_SQUAREOFF")
                continue

            # ── Preemptive Loss Exit (14:30) ──
            # If trade is in loss after PREEMPTIVE_EXIT_TIME, exit immediately
            # rather than waiting for 15:05 forced squareoff. Prevents the #1
            # P&L killer: trades drifting to EOD with no momentum resolution.
            pe_h, pe_m = map(int, PREEMPTIVE_EXIT_TIME.split(":"))
            if now.time() >= datetime.time(pe_h, pe_m):
                token = trade.get("token")
                ltp = 0
                if self.tick_store and token:
                    ltp = self.tick_store.get_ltp(token)
                if ltp > 0:
                    is_short = trade.get("is_short", False)
                    active_qty = trade.get("remaining_qty", trade.get("qty", 0))
                    unrealised = ((trade["entry_price"] - ltp) * active_qty
                                  if is_short
                                  else (ltp - trade["entry_price"]) * active_qty)
                    if unrealised < 0:
                        self._force_exit(oid, trade, "PREEMPTIVE_LOSS_EXIT")
                        continue

            # ── Dynamic Regime Shift Evacuation ──
            is_pos_short = trade.get("is_short", False)
            if current_regime:
                if is_pos_short and current_regime == "BULL":
                    self.alert(f"⚠️ *MACRO EVACUATION* `{trade['symbol']}`\nRegime flipped to BULL. Killing SHORT.")
                    self._force_exit(oid, trade, "MACRO_FLIP_BULL")
                    continue
                elif not is_pos_short and current_regime == "BEAR_PANIC":
                    self.alert(f"⚠️ *MACRO EVACUATION* `{trade['symbol']}`\nRegime flipped to BEAR_PANIC. Killing LONG.")
                    self._force_exit(oid, trade, "MACRO_FLIP_BEAR")
                    continue

            # ── Dynamic RSI Exits ──
            if daily_cache and tick_store:
                token = trade.get("token")
                if not token and trade.get("symbol"):
                    from agents.data_agent import DataAgent
                    for t, s in DataAgent.UNIVERSE.items():
                        if s == trade["symbol"]:
                            token = t
                            trade["token"] = t
                            break

                if token:
                    close_px = tick_store.get_ltp_if_fresh(token)
                    if close_px > 0:
                        cache_closes = daily_cache.get_closes(token)
                        if len(cache_closes) > 0:
                            live_closes = cache_closes.copy()
                            live_closes.append(close_px)
                            from agents import data_agent
                            # Use S6_RSI_PERIOD if S6, else default to S7's
                            rsi_period = S6_RSI_PERIOD if "S6" in strat else S7_RSI_PERIOD
                            rsi_live = (data_agent.DataAgent.compute_rsi(
                                live_closes, rsi_period) or [50])[-1]

                            # S6 Short: exit when RSI cools below exit threshold
                            if "S6" in strat and rsi_live <= S6_RSI_EXIT:
                                self._force_exit(oid, trade, "S6_RSI_EXIT")
                                continue
                            # S7 Long: exit when RSI recovers above exit threshold
                            if "S7" in strat and rsi_live >= S7_RSI_EXIT:
                                self._force_exit(oid, trade, "S7_RSI_EXIT")
                                continue



    def _force_exit(self, oid: str, trade: dict, reason: str):
        # Cancel any pending orders
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

        product  = self.kite.PRODUCT_MIS
        exit_qty = trade.get("remaining_qty", trade["qty"])

        # Short positions close with BUY, long positions with SELL
        is_short = trade.get("is_short", False)
        exit_txn = (self.kite.TRANSACTION_TYPE_BUY if is_short
                    else self.kite.TRANSACTION_TYPE_SELL)

        try:
            exit_oid = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NSE,
                tradingsymbol=trade["symbol"],
                transaction_type=exit_txn,
                quantity=exit_qty, product=product,
                order_type=self.kite.ORDER_TYPE_MARKET,
                validity=self.kite.VALIDITY_DAY
            )
        except Exception as e:
            self.alert(f"FORCE EXIT FAILED: `{trade['symbol']}` -- {e}")
            return

        # Fetch actual exit price from the order, falling back to LTP if it fails
        exit_est = 0.0
        try:
            import time
            time.sleep(1) # Give Kite a moment to process the market order
            orders = self.kite.orders()
            for o in orders:
                if str(o.get("order_id")) == str(exit_oid) and o.get("status") == "COMPLETE":
                    exit_est = float(o.get("average_price", 0.0))
                    break
        except Exception as e:
            print(f"[Exec] Failed to fetch exit average price for {exit_oid}: {e}")

        if not exit_est:
            # Estimate exit price from live tick (market order fallback)
            token     = trade.get("token")
            exit_est  = trade["entry_price"]  # safe fallback
            if self.tick_store and token:
                ltp = self.tick_store.get_ltp(token)
                if ltp > 0:
                    exit_est = ltp

        pnl = self.risk.close_position(oid, exit_est)
        self.state.close(oid)
        self.journal.log_trade({
            **trade,
            "full_exit_price": exit_est,
            "pnl":             pnl,
            "exit_reason":     reason,
            "exit_time":       now_ist(),
            "daily_pnl_after": self.risk.daily_pnl,
        })
        streak = (f"\nStreak: `{self.risk.consecutive_losses}/{MAX_CONSECUTIVE_LOSSES}`"
                  if self.risk.consecutive_losses > 0 else "")
        self.alert(
            f"*FORCE EXIT*\n"
            f"`{trade['symbol']}` | `{reason}`\n"
            f"Est. PnL: Rs.`{pnl:+.0f}`{streak}"
        )
        self.active_trades.pop(oid, None)

    def flatten_all(self, reason: str = "EOD_FORCED"):
        """Forcefully square off all active trades."""
        flattened = 0
        for oid, trade in list(self.active_trades.items()):
            self._force_exit(oid, trade, reason)
            flattened += 1
        if flattened > 0:
            self.alert(f"*[flatten_all]* Force exited `{flattened}` positions for reason: `{reason}`")

    def daily_summary_alert(self, regime: str, total_scans: int = 0, real_capital: float = None):
        from agents.report_agent import build_daily_report
        stats = self.risk.get_daily_stats()
        self.journal.log_daily_summary(
            stats, regime,
            self.risk.engine_stopped, self.risk.stop_reason
        )
        trades_today = self.journal.get_all_trades_for_date()
        msg = build_daily_report(
            stats=stats,
            regime=regime,
            trades_today=trades_today,
            capital=self.risk.total_capital,
            total_scans=total_scans,
            real_capital=real_capital
        )
        self.alert(msg)

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
