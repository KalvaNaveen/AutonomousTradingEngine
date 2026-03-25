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
        self.tg_base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
        # [V16] Minervini agents — injected later via _init_kite() in main.py
        self._stage_agent       = None
        self._fundamental_agent = None

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

    def rearm_s1_exits(self):
        """
        Called at 9:15 AM (after pre-market, before 9:30 AM trading).
        Re-places S1 partial and target limit orders for multi-day CNC holds.
        SL-M is NOT placed for S1 (checked in memory at 3:15 PM instead).
        Handles partially filled positions correctly by checking state.
        """
        for oid, trade in list(self.active_trades.items()):
            if trade["strategy"] != "S1_EMA_DIVERGENCE":
                continue
            
            # Re-place partial if not already filled
            if not trade.get("partial_filled") and trade.get("partial_qty", 0) > 0:
                try:
                    p_oid = self.kite.place_order(
                        variety=self.kite.VARIETY_REGULAR,
                        exchange=self.kite.EXCHANGE_NSE,
                        tradingsymbol=trade["symbol"],
                        transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                        quantity=trade["partial_qty"],
                        product=self.kite.PRODUCT_CNC,
                        order_type=self.kite.ORDER_TYPE_LIMIT,
                        price=round(trade.get("partial_target") or trade.get("partial_target_1"), 1),
                        validity=self.kite.VALIDITY_DAY
                    )
                    trade["partial_oid"] = p_oid
                except Exception as e:
                    self.alert(f"[WARN] S1 Re-arm Partial Failed: `{trade['symbol']}`\n`{e}`")

            # Re-place final target
            try:
                t_oid = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NSE,
                    tradingsymbol=trade["symbol"],
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=trade.get("remaining_qty", trade["qty"]),
                    product=self.kite.PRODUCT_CNC,
                    order_type=self.kite.ORDER_TYPE_LIMIT,
                    price=round(trade["target_price"], 1),
                    validity=self.kite.VALIDITY_DAY
                )
                trade["target_oid"] = t_oid
            except Exception as e:
                self.alert(f"[WARN] S1 Re-arm Target Failed: `{trade['symbol']}`\n`{e}`")

        # No need to persist state -> OIDs are intraday temporary
        self.alert(f"🔫 *Rearmed exits* for `{len(self.active_trades)}` hold positions.")

    def execute(self, signal: dict, regime: str = "UNKNOWN") -> bool:
        signal["regime"] = regime

        # ── DUPLICATE SYMBOL GUARD ─────────────────────────────────
        # Prevents the engine from ever holding 2+ positions in the same stock.
        # This was the root cause of the 4x HINDPETRO crash-recovery bug.
        sym = signal.get("symbol", "")
        for t in self.active_trades.values():
            if t.get("symbol") == sym:
                print(f"[Exec] REJECTED {sym}: Already holding an open position")
                return False

        approved, reason = self.risk.approve_trade(signal)
        if not approved:
            print(f"[Exec] REJECTED {signal['symbol']}: {reason}")
            return False

        # [v13 Phase 3] "Sit on Hands" Sector Guard for Swing Trades
        strat = signal.get("strategy", "")
        if strat in ["S1_EMA_DIVERGENCE", "S1_RSI_MEAN_REV", "S3_SEPA_VCP", "S4_LEADERSHIP"]:
            if getattr(self, '_sector_agent', None):
                sector = getattr(self, '_symbol_to_sector', {}).get(sym)
                if sector and self._sector_agent.is_cold(sector):
                    print(f"[Exec] REJECTED {sym}: Sector {sector} is COLD (Sit on Hands)")
                    return False
            
            # [v13 Phase 3] Earnings Event Guard
            if getattr(self, '_earnings_agent', None):
                if self._earnings_agent.is_earnings_imminent(sym, days=5):
                    print(f"[Exec] REJECTED {sym}: Earnings imminent within 5 days (Event Guard)")
                    return False

            # [v14 Phase 4] Global Macro Liquidity Guard
            if getattr(self, '_macro_agent', None):
                if self._macro_agent.is_bearish:
                    print(f"[Exec] REJECTED {sym}: MACRO BEARISH (DXY/US10Y high) (Liquidity Guard)")
                    return False

        # [v14 Phase 4] L2 Order Flow Guard — blocks S5 day trades into sell pressure
        if strat == "S5_VWAP_ORB":
            token = signal.get("token", 0)
            if token and getattr(self, '_order_flow_agent', None):
                if self._order_flow_agent.is_sell_pressure(token):
                    flow = self._order_flow_agent.get_flow_label(token)
                    print(f"[Exec] REJECTED {sym}: L2 SELL PRESSURE ({flow}) — no breakout confirmation")
                    return False

        qty = self.risk.calculate_position_size(
            signal["entry_price"], signal["stop_price"],
            regime=regime,
            strategy=signal.get("strategy", "")
        )
        if qty == 0:
            return False

        product = (self.kite.PRODUCT_CNC if signal["product"] == "CNC"
                   else self.kite.PRODUCT_MIS)

        # [V16] Short selling support: S6_RSI_SHORT signals have is_short=True
        is_short = signal.get("is_short", False)
        txn_type = (self.kite.TRANSACTION_TYPE_SELL if is_short
                    else self.kite.TRANSACTION_TYPE_BUY)

        # [V16] Route through Go executor if bridge is connected, else Python fallback
        go_bridge = getattr(self, '_go_bridge', None)
        if go_bridge and go_bridge.is_connected():
            try:
                go_signal = {
                    "action": "SELL" if is_short else "BUY",
                    "symbol": signal["symbol"],
                    "exchange": "NSE",
                    "qty": qty,
                    "price": round(signal["entry_price"] * (0.998 if is_short else 1.002), 1),
                    "trigger_price": 0,
                    "order_type": "LIMIT",
                    "product": "CNC" if signal["product"] == "CNC" else "MIS",
                    "validity": "DAY",
                    "tag": signal.get("strategy", "BNF"),
                }
                result = go_bridge.send_order(go_signal)
                if result.get("status") == "OK":
                    entry_oid = result.get("order_id", "GO_" + signal["symbol"])
                    latency = result.get("latency_us", 0)
                    print(f"[Exec] ⚡ GO EXECUTOR: {signal['symbol']} order placed in {latency}μs")
                else:
                    raise Exception(f"Go executor error: {result.get('message')}")
            except Exception as e:
                # Fallback to Python if Go fails
                print(f"[Exec] Go bridge failed ({e}), falling back to Python...")
                try:
                    entry_oid = self.kite.place_order(
                        variety=self.kite.VARIETY_REGULAR,
                        exchange=self.kite.EXCHANGE_NSE,
                        tradingsymbol=signal["symbol"],
                        transaction_type=txn_type,
                        quantity=qty,
                        product=product,
                        order_type=self.kite.ORDER_TYPE_LIMIT,
                        price=round(signal["entry_price"] * (0.998 if is_short else 1.002), 1),
                        validity=self.kite.VALIDITY_DAY
                    )
                except Exception as e2:
                    self.alert(f"[FAIL] ORDER FAILED: `{signal['symbol']}`\n`{e2}`")
                    return False
        else:
            # Standard Python path (no Go executor running)
            try:
                entry_oid = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NSE,
                    tradingsymbol=signal["symbol"],
                    transaction_type=txn_type,
                    quantity=qty,
                    product=product,
                    order_type=self.kite.ORDER_TYPE_LIMIT,
                    price=round(signal["entry_price"] * (0.998 if is_short else 1.002), 1),
                    validity=self.kite.VALIDITY_DAY
                )
            except Exception as e:
                self.alert(f"[FAIL] ORDER FAILED: `{signal['symbol']}`\n`{e}`")
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

        strat_label = signal["strategy"].split("_")[0] if "_" in signal["strategy"] else signal["strategy"]
        partial_str = f"Rs.{partial_price:.2f}" if partial_price else "Rs.0"
        self.alert(
            f"🟢 *EXECUTED {strat_label} — Qty:{qty} | R:R {rr:.2f}*\n"
            f"`{signal['symbol']}` | `{regime}` | `{signal['strategy']}`\n"
            f"Entry: Rs.`{signal['entry_price']:.2f}` | Qty: `{qty}`\n"
            f"Partial: `{partial_str}`\n"
            f"Target: Rs.`{signal['target_price']:.2f}` | "
            f"Stop: Rs.`{signal['stop_price']:.2f}`\n"
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

    def monitor_positions(self, daily_cache=None, tick_store=None):
        now = now_ist()

        # Pre-fetch all current orders ONCE per tick.
        # Kite API limit is ~10/sec, loop previously called 1/sec * open trades
        try:
            orders = self.kite.orders()
            order_map = {str(o.get("order_id")): o for o in orders}
        except Exception as e:
            print(f"[Execution] Orders pre-fetch failed: {e}")
            order_map = None  # Fallback to individual REST inside check_partial

        for oid, trade in list(self.active_trades.items()):

            # Check if partial fill has completed
            if not trade.get("partial_filled"):
                if self.fill_monitor.check_partial_exit_filled(trade, order_map):
                    trade["partial_filled"] = True
                    self.state.mark_partial_filled(oid, trade["remaining_qty"])

                    # Immediately realise partial profit in risk agent today
                    # instead of waiting for final leg close. Prevents false
                    # daily_loss_limit stops.
                    partial_px = trade.get("partial_target") or trade.get("partial_target_1")
                    if partial_px:
                        pnl = (partial_px - trade["entry_price"]) * trade["partial_qty"]
                        trade["realised_pnl"] = trade.get("realised_pnl", 0.0) + pnl
                        self.risk.daily_pnl += pnl
                    
                    # Update risk agent's open position qty reference
                    if oid in self.risk.open_positions:
                        self.risk.open_positions[oid]["qty"] = trade["remaining_qty"]

            if trade["strategy"] == "S2_OVERREACTION":
                elapsed = (now - trade["entry_time"]).seconds / 60
                if elapsed >= S2_TIME_STOP_MINUTES:
                    self._force_exit(oid, trade, "TIME_STOP_45MIN")
                    continue
                if now.time() >= datetime.time(15, 14):
                    self._force_exit(oid, trade, "EOD_MIS_SQUAREOFF")
                    continue

            # [V16] S5 VWAP/ORB intraday monitoring
            if trade["strategy"] == "S5_VWAP_ORB":
                # Auto-exit at 15:00 (before exchange 15:15 auto-squareoff)
                if now.time() >= datetime.time(15, 0):
                    self._force_exit(oid, trade, "S5_EOD_EXIT_15:00")
                    continue

            # [V16] S6/S7 MIS EOD square-off (exit at 15:15, same as S2)
            if trade["strategy"] in ("S6_RSI_SHORT", "S7_RSI_LONG"):
                if now.time() >= datetime.time(15, 14):
                    self._force_exit(oid, trade, "MIS_EOD_SQUAREOFF")
                    continue

            # S1: Check both old and new strategy name
            if trade["strategy"] in ("S1_EMA_DIVERGENCE", "S1_RSI_MEAN_REV"):
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

            # [V16] Parity Fix — S1/S6/S7 Dynamic Oscillator Exits (Like Simulator)
            if trade["strategy"] in ("S1_RSI_MEAN_REV", "S6_RSI_SHORT", "S7_RSI_LONG"):
                if daily_cache and tick_store:
                    token = trade.get("token")
                    if token:
                        close_px = tick_store.get_ltp_if_fresh(token)
                        if close_px > 0:
                            cache_closes = daily_cache.get_closes(token)
                            if len(cache_closes) > 0:
                                live_closes = cache_closes.copy()
                                live_closes.append(close_px)
                                from agents import data_agent
                                rsi_live = (data_agent.DataAgent.compute_rsi(live_closes, S1_RSI_PERIOD) or [50])[-1]
                                
                                if trade["strategy"] == "S1_RSI_MEAN_REV" and rsi_live >= S1_RSI_OVERBOUGHT:
                                    self._force_exit(oid, trade, "S1_RSI_EXIT")
                                    continue
                                if trade["strategy"] == "S6_RSI_SHORT" and rsi_live <= S6_RSI_EXIT:
                                    self._force_exit(oid, trade, "S6_RSI_EXIT")
                                    continue
                                if trade["strategy"] == "S7_RSI_LONG" and rsi_live >= 60:
                                    self._force_exit(oid, trade, "S7_RSI_EXIT")
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

        # [V16] Short positions close with BUY, long positions with SELL
        is_short = trade.get("is_short", False)
        exit_txn = (self.kite.TRANSACTION_TYPE_BUY if is_short
                    else self.kite.TRANSACTION_TYPE_SELL)

        try:
            self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NSE,
                tradingsymbol=trade["symbol"],
                transaction_type=exit_txn,
                quantity=exit_qty, product=product,
                order_type=self.kite.ORDER_TYPE_MARKET,
                validity=self.kite.VALIDITY_DAY
            )
        except Exception as e:
            self.alert(f"[WARN] FORCE EXIT FAILED: `{trade['symbol']}` — {e}")
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
        streak = (f"\n[WARN] Streak: `{self.risk.consecutive_losses}/3`"
                  if self.risk.consecutive_losses > 0 else "")
        self.alert(
            f"🔴 *FORCE EXIT*\n"
            f"`{trade['symbol']}` | `{reason}`\n"
            f"Est. PnL: Rs.`{pnl:+.0f}`{streak}"
        )
        self.active_trades.pop(oid, None)

    def daily_summary_alert(self, regime: str, total_scans: int = 0):
        from agents.report_agent import build_daily_report
        stats = self.risk.get_daily_stats()
        self.journal.log_daily_summary(
            stats, regime,
            self.risk.engine_stopped, self.risk.stop_reason
        )

        # All trades for the trade-log section
        trades_today = self.journal.get_all_trades_for_date()

        msg = build_daily_report(
            stats=stats,
            regime=regime,
            trades_today=trades_today,
            capital=self.risk.capital,
            total_scans=total_scans,
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

    # ── [V16] Master Checklist — 10 Minervini Hard Gates ────────────

    def master_checklist(self, signal: dict) -> tuple:
        """
        [V16] All 10 Minervini gates. Returns (passes: bool, reason: str).
        Called at top of execute_minervini() — blocks before approve_trade().
        Each rejection sends a Telegram alert with specific reason.
        """
        sym  = signal.get("symbol", "")
        strat = signal.get("strategy", "")

        # ── DUPLICATE SYMBOL GUARD ─────────────────────────────────
        for t in self.active_trades.values():
            if t.get("symbol") == sym:
                self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: Already holding an open position")
                return False, "DUPLICATE_SYMBOL"

        # Gate 1: Market Status must be BULL or BULL_WATCH
        mkt = self.state.get_kv("market_status", "BULL")
        if mkt in ("BEAR", "CHOP", "RALLY_ATTEMPT"):
            self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: Market Status `{mkt}`")
            return False, f"MARKET_{mkt}"

        # Gate 2: Stage 2 confirmed
        token = signal.get("token", 0)
        if self._stage_agent and not self._stage_agent.is_stage_2(token):
            self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: Not Stage 2")
            return False, "NOT_STAGE_2"

        # Gate 3: EPS growth ≥ 25%
        fund = {}
        if self._fundamental_agent:
            fund = self._fundamental_agent.get(sym)
        eps_g = fund.get("eps_growth_pct")
        if eps_g is not None and eps_g < S3_MIN_EPS_GROWTH:
            self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: EPS {eps_g:.0f}% < {S3_MIN_EPS_GROWTH}%")
            return False, f"EPS_{eps_g:.0f}%"

        # Gate 4: EPS accelerating
        if not fund.get("eps_accelerating", True):
            self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: EPS not accelerating")
            return False, "EPS_NOT_ACCEL"

        # Gate 5: Sales growth ≥ 20%
        sal_g = fund.get("sales_growth_pct")
        if sal_g is not None and sal_g < S3_MIN_SALES_GROWTH:
            self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: Sales {sal_g:.0f}% < {S3_MIN_SALES_GROWTH}%")
            return False, f"SALES_{sal_g:.0f}%"

        # Gate 6: ROE > 17%
        roe = fund.get("roe_pct")
        if roe is not None and roe < S3_MIN_ROE:
            self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: ROE {roe:.0f}% < {S3_MIN_ROE}%")
            return False, f"ROE_{roe:.0f}%"

        # Gate 7: VCP ≥ 2 contractions (S3 only)
        if strat == "S3_SEPA_VCP":
            vcp_n = signal.get("vcp_contractions", 0)
            if vcp_n < S3_VCP_MIN_CONTRACTIONS:
                self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: VCP {vcp_n} < {S3_VCP_MIN_CONTRACTIONS}")
                return False, f"VCP_{vcp_n}"

        # Gate 8: RS score ≥ 70 (S3) or ≥ 80 (S4)
        rs = signal.get("rs_score", 0)
        
        rs_min = S4_MIN_RS_SCORE if strat == "S4_LEADERSHIP" else S3_MIN_RS_SCORE
        if rs < rs_min:
            self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: RS {rs} < {rs_min}")
            return False, f"RS_{rs}"

        # [v13 Phase 3] Sector Guard (Sit on Hands / Leadership requirement)
        if getattr(self, '_sector_agent', None):
            sector = getattr(self, '_symbol_to_sector', {}).get(sym)
            if sector:
                if self._sector_agent.is_cold(sector):
                    self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: Sector {sector} is COLD")
                    return False, f"COLD_SECTOR_{sector}"
                if strat == "S4_LEADERSHIP" and not self._sector_agent.is_hot(sector):
                    self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: S4 requires HOT sector ({sector} is not hot)")
                    return False, f"NOT_HOT_SECTOR"

        # [v13 Phase 3] Earnings Event Guard
        if getattr(self, '_earnings_agent', None):
            if self._earnings_agent.is_earnings_imminent(sym, days=5):
                self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: Earnings imminent within 5 days")
                return False, "EARNINGS_IMMINENT"

        # [v14 Phase 4] Global Macro Liquidity Guard
        if getattr(self, '_macro_agent', None):
            if self._macro_agent.is_bearish:
                self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: MACRO BEARISH (DXY/US10Y high)")
                return False, "MACRO_BEARISH"

        # Gate 9: Stop ≤ 8%
        entry = signal.get("entry_price", 0)
        stop  = signal.get("stop_price", 0)
        stop_pct = (entry - stop) / entry if entry > 0 else 1.0
        max_stop = S4_MAX_STOP_PCT if strat == "S4_LEADERSHIP" else S3_MAX_STOP_PCT
        if stop_pct > max_stop:
            self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: Stop {stop_pct*100:.1f}% > {max_stop*100}%")
            return False, f"STOP_{stop_pct*100:.1f}%"

        # Gate 10: D/E < 0.5
        de = fund.get("debt_equity")
        if de is not None and de > S3_MAX_DEBT_EQUITY:
            self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: D/E {de:.2f} > {S3_MAX_DEBT_EQUITY}")
            return False, f"DE_{de:.2f}"

        # Gate 10.5 (bonus): Superperformance profile
        if self._fundamental_agent:
            sp_ok, sp_summary = self._fundamental_agent.is_superperformance_profile(sym)
            if not sp_ok:
                # Non-blocking: log but don't reject — superperf is bonus gate
                print(f"[Checklist] {sym} not superperf profile: {sp_summary}")
                # Only block S4 on superperf failure — S4 requires leadership stocks
                if strat == "S4_LEADERSHIP":
                    return False, f"S4_NOT_SUPERPERF"

        # Gate 11 (bonus): CNC product
        if signal.get("product") != "CNC":
            self.alert(f"[FAIL] *CHECKLIST REJECT — {sym}*\nGate: Product not CNC")
            return False, "NOT_CNC"

        return True, "ALL_GATES_PASS"

    # ── [V16] Minervini Entry ───────────────────────────────────────

    def execute_minervini(self, signal: dict):
        """
        S3/S4 entry handler. Trail mode after entry.
        [V16] master_checklist() called first — blocks before approve_trade().
        """
        sym   = signal["symbol"]
        strat = signal["strategy"]
        entry = signal["entry_price"]
        stop  = signal["stop_price"]
        target = signal["target_price"]

        # [V16] Master checklist — all 10 gates must pass
        passes, reason = self.master_checklist(signal)
        if not passes:
            print(f"[ExecutionAgent] {sym} REJECTED by master_checklist: {reason}")
            return

        approved, appr_reason = self.risk.approve_trade(signal)
        if not approved:
            self.alert(f"[WARN] *{strat} BLOCKED*\n`{sym}` — {appr_reason}")
            return

        qty = self.risk.calculate_position_size(
            entry, stop,
            regime=signal.get("regime", "NORMAL"),
            strategy=strat
        )
        if qty <= 0:
            return

        now = now_ist()
        trigger_px = round(signal["entry_price"], 1)          # The exact pivot crossing
        limit_px   = round(signal["entry_price"] * 1.002, 1)  # Capped at 0.2% slippage
        try:
            entry_oid = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NSE,
                tradingsymbol=sym,
                transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                quantity=qty,
                product=self.kite.PRODUCT_CNC,
                order_type=self.kite.ORDER_TYPE_SL,
                price=limit_px,
                trigger_price=trigger_px,
                validity=self.kite.VALIDITY_DAY
            )
        except Exception as e:
            self.alert(f"[FAIL] *{strat} ORDER FAILED*\n`{sym}`: {e}")
            return

        trade = {
            "symbol":         sym,
            "strategy":       strat,
            "product":        "CNC",
            "regime":         self.state.get_kv("market_status", ""),
            "entry_price":    entry,
            "stop_price":     stop,
            "partial_target": signal.get("partial_target", target),
            "target_price":   target,
            "qty":            qty,
            "remaining_qty":  qty,
            "entry_time":     now,
            "entry_date":     now.date(),
            # [V16] Minervini-specific fields
            "trail_stop":       stop,
            "pyramid_added":    0,
            "rs_score":         signal.get("rs_score", 0),
            "market_status":    self.state.get_kv("market_status", ""),
            "weeks_no_progress": 0,
        }
        self.active_trades[entry_oid] = trade
        self.state.save(entry_oid, trade)
        self.risk.register_open(entry_oid, trade)

        self.alert(
            f"[PASS] *{strat} ENTRY*\n"
            f"`{sym}` @ Rs.`{entry:,.2f}`\n"
            f"Qty: `{qty}` | Stop: Rs.`{stop:,.2f}` | Target: Rs.`{target:,.2f}`\n"
            f"RS: `{signal.get('rs_score', 0)}` | Product: CNC"
        )

    # ── [V16] Minervini Position Monitor ──────────────────────────

    def monitor_minervini_positions(self, daily_cache=None, tick_store=None):
        """
        Called every tick cycle. Manages all S3/S4 positions:
        - Trail stop below 10d/21d SMA
        - Move to breakeven at +12% (S3) / +10% (S4)
        - 1/3 partial at +22% (S3) / +20% (S4) if move < 3 weeks
        - Pyramid at +12% from entry **ON NEW PIVOT** (FIXED)
        - Stall exit: no new high 3+ weeks (FIXED)
        - Max-hold exit
        """
        now = now_ist()
        
        for oid, trade in list(self.active_trades.items()):
            strat = trade.get("strategy", "")
            if strat not in ("S3_SEPA_VCP", "S4_LEADERSHIP"):
                continue

            sym = trade["symbol"]
            entry = trade["entry_price"]
            stop = trade.get("trail_stop", trade["stop_price"])
            qty = trade.get("remaining_qty", trade["qty"])
            token = next((t for t, s in (getattr(self, '_data_universe', None) or {}).items() if s == sym), 0)

            # Get live price
            ltp = 0.0
            if tick_store and tick_store.is_fresh():
                ltp = tick_store.get_ltp_if_fresh(token) or 0.0
            if ltp <= 0:
                continue

            gain_pct = (ltp - entry) / entry if entry > 0 else 0.0

            # ── Trail stop: below 10d/21d SMA ───────────────────
            if daily_cache and daily_cache.is_loaded():
                closes = daily_cache.get_closes(token)
                if len(closes) >= 21:
                    sma10 = float(sum(closes[-10:]) / 10)
                    sma21 = float(sum(closes[-21:]) / 21)
                    new_trail = min(sma10, sma21) * 0.99
                    if new_trail > stop:
                        trade["trail_stop"] = round(new_trail, 2)
                        self.state.save(oid, trade)

            # ── Breakeven ─────────────────────────────────────
            be_pct = S4_BREAKEVEN_MOVE_PCT if strat == "S4_LEADERSHIP" else S3_BREAKEVEN_MOVE_PCT
            if gain_pct >= be_pct and trade.get("trail_stop", 0) < entry:
                trade["trail_stop"] = entry
                self.state.save(oid, trade)
                self.alert(f"🟢 *BREAKEVEN* `{sym}` stop → Rs.{entry:,.2f}")

            # ── Partial at +22%/+20% if < 3 weeks ───────────────
            partial_pct = S4_PARTIAL_EXIT_PCT if strat == "S4_LEADERSHIP" else S3_PARTIAL_EXIT_PCT
            if (gain_pct >= partial_pct and not trade.get("partial_filled", False)):
                entry_date = trade.get("entry_date")
                if entry_date:
                    try:
                        weeks = (now.date() - entry_date).days / 7
                    except Exception:
                        weeks = 999
                    if weeks < 3:
                        partial_qty = max(1, qty // 3)
                        try:
                            self.kite.place_order(
                                variety="regular", exchange="NSE",
                                tradingsymbol=sym, transaction_type="SELL",
                                quantity=partial_qty, product="CNC",
                                order_type="LIMIT", price=ltp
                            )
                            trade["partial_filled"] = True
                            trade["remaining_qty"] = qty - partial_qty
                            self.state.save(oid, trade)
                            self.alert(
                                f"🟡 *PARTIAL EXIT* `{sym}` 1/3 @ Rs.{ltp:,.2f} "
                                f"(+{gain_pct*100:.1f}% in {weeks:.1f}w)"
                            )
                        except Exception as e:
                            print(f"[ExecutionAgent] Partial exit error {sym}: {e}")

            # ── Pyramid at +12% ON NEW PIVOT (MINERVINI RULE) ──────── FIXED
            if (gain_pct >= S3_PYRAMID_ADD_PCT and 
                not trade.get("pyramid_added", False)):
                # NEW: Check for new pivot high (Minervini rule)
                highs_21d = daily_cache.get_highs(token)[-21:] if daily_cache else []
                if len(highs_21d) >= 21:
                    new_pivot = max(highs_21d[-10:])  # 10d new high
                    entry_high = trade.get("entry_high", entry)
                    if new_pivot > entry_high * 1.05:  # 5% new pivot
                        add_qty = max(1, trade["qty"] // 2)
                        try:
                            self.kite.place_order(
                                variety="regular", exchange="NSE",
                                tradingsymbol=sym, transaction_type="BUY",
                                quantity=add_qty, product="CNC",
                                order_type="LIMIT", price=ltp
                            )
                            trade["pyramid_added"] = True
                            trade["qty"] += add_qty
                            trade["remaining_qty"] += add_qty
                            trade["entry_high"] = new_pivot  # Track for next
                            self.state.save(oid, trade)
                            self.alert(
                                f"🟣 *PYRAMID NEW PIVOT* `{sym}` +{add_qty} @ Rs.{ltp:,.2f} "
                                f"(new high Rs.{new_pivot:,.2f}, +{gain_pct*100:.1f}%)"
                            )
                        except Exception as e:
                            print(f"[ExecutionAgent] Pyramid error {sym}: {e}")

            # ── Stall exit: NO NEW HIGH 3+ weeks (MINERVINI RULE) ───── FIXED
            entry_date = trade.get("entry_date")
            if entry_date:
                try:
                    weeks_held = (now.date() - entry_date).days / 7
                except Exception:
                    weeks_held = 0
                stall_weeks = S4_STALL_WEEKS if strat == "S4_LEADERSHIP" else S3_STALL_WEEKS
                
                # FIXED: Track max high since entry vs current
                highs_since_entry = daily_cache.get_highs(token)[-int(weeks_held*5):] if daily_cache else []
                max_high_since = max(highs_since_entry) if highs_since_entry else ltp
                
                if (max_high_since > 0 and weeks_held >= stall_weeks and
                        (ltp / max_high_since) < 0.97):
                    self._close_minervini(oid, trade, ltp, "STALL_NO_NEW_HIGH")
                    continue

            # ── Max-hold exit ─────────────────────────────────
            max_hold = S4_MAX_HOLD_DAYS if strat == "S4_LEADERSHIP" else S3_MAX_HOLD_DAYS
            if entry_date:
                try:
                    days_held = (now.date() - entry_date).days
                except Exception:
                    days_held = 0
                if days_held >= max_hold:
                    self._close_minervini(oid, trade, ltp, "MAX_HOLD_EXIT")
                    continue

            # ── Trail stop hit ─────────────────────────────────
            trail = trade.get("trail_stop", trade["stop_price"])
            if ltp <= trail:
                self._close_minervini(oid, trade, ltp, "TRAIL_STOP")


    def _close_minervini(self, oid: str, trade: dict, ltp: float, reason: str):
        """Close a Minervini S3/S4 position."""
        sym = trade["symbol"]
        qty = trade.get("remaining_qty", trade["qty"])
        try:
            self.kite.place_order(
                variety="regular", exchange="NSE",
                tradingsymbol=sym, transaction_type="SELL",
                quantity=qty, product="CNC",
                order_type="LIMIT", price=ltp
            )
        except Exception as e:
            self.alert(f"[FAIL] *{reason} SELL FAILED* `{sym}`: {e}")
            return

        pnl = (ltp - trade["entry_price"]) * qty
        self.risk.close_position(oid, ltp)   # pass exit price, not pre-computed pnl
        self.state.close(oid)
        self.journal.log_exit(oid, trade, ltp, reason)
        self.active_trades.pop(oid, None)

        self.alert(
            f"🔴 *{reason}* `{sym}`\n"
            f"Exit @ Rs.`{ltp:,.2f}` | PnL: Rs.`{pnl:+,.0f}`\n"
            f"Strategy: `{trade['strategy']}`"
        )
