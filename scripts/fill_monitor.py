import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

"""
Polls Zerodha order status every 30 seconds after entry placement.
Handles:
  - Fully filled: SL + target orders confirmed active
  - Partially filled after timeout: cancel remainder, adjust SL/target qty
  - Not filled after timeout: cancel entire entry order
  - Partial exit filled: update remaining_qty in state
"""

import datetime
import time
from kiteconnect import KiteConnect
from core.state_manager import StateManager
from config import FILL_POLL_INTERVAL_SEC, FILL_TIMEOUT_MINUTES, now_ist


class FillMonitor:

    def __init__(self, kite: KiteConnect, state: StateManager, alert_fn=None):
        self.kite  = kite
        self.state = state
        self._alert = alert_fn or (lambda msg: print(f"[FillMonitor] {msg}"))

    def get_order_status(self, order_id: str) -> dict:
        """
        Returns order dict with keys: status, filled_quantity, pending_quantity
        Kite status values: OPEN, COMPLETE, CANCELLED, REJECTED
        """
        try:
            orders = self.kite.orders()
            for o in orders:
                if str(o.get("order_id")) == str(order_id):
                    return {
                        "status":           o.get("status", "UNKNOWN"),
                        "filled_qty":       o.get("filled_quantity", 0),
                        "pending_qty":      o.get("pending_quantity", 0),
                        "average_price":    o.get("average_price", 0),
                    }
        except Exception as e:
            print(f"[FillMonitor] Order status error: {e}")
        return {"status": "UNKNOWN", "filled_qty": 0,
                "pending_qty": 0, "average_price": 0}

    def wait_for_fill(self, trade: dict, alert_fn=None) -> dict:
        """
        Polls entry order until filled, partially filled+timeout, or timeout.
        On fill: places SL-M + partial + target orders for the first time.
        On partial fill: places them at the actual filled quantity.
        On timeout with zero fill: cancels entry, no position opened.

        This is called from a background thread per trade.
        SL and target are never placed before entry confirms — no naked short risk.
        """
        entry_oid   = trade["entry_oid"]
        symbol      = trade["symbol"]
        timeout_at  = now_ist() + datetime.timedelta(minutes=FILL_TIMEOUT_MINUTES)
        product = (self.kite.PRODUCT_CNC if trade["product"] == "CNC"
                   else self.kite.PRODUCT_MIS)

        while now_ist() < timeout_at:
            status = self.get_order_status(entry_oid)

            # ── Fully filled ──────────────────────────────────────
            if status["status"] == "COMPLETE":
                actual_qty = status["filled_qty"]
                actual_price = status["average_price"] or trade["entry_price"]

                # Adjust SL and target to actual filled qty
                trade = self._adjust_order_quantities(
                    trade, actual_qty, actual_price, product
                )
                if alert_fn:
                    alert_fn(
                        f"[PASS] *FILLED*: `{symbol}` "
                        f"Qty:`{actual_qty}` @ Rs.`{actual_price:.2f}`"
                    )
                return trade

            # ── Already cancelled/rejected ────────────────────────
            if status["status"] in ("CANCELLED", "REJECTED"):
                if alert_fn:
                    alert_fn(
                        f"[WARN] *ENTRY {status['status']}*: `{symbol}`\n"
                        f"No position opened."
                    )
                trade["entry_cancelled"] = True
                return trade

            time.sleep(FILL_POLL_INTERVAL_SEC)

        # ── Timeout: partial or zero fill ─────────────────────────
        status = self.get_order_status(entry_oid)
        filled_qty = status.get("filled_qty", 0)

        if filled_qty == 0:
            # Cancel entry — no position at all
            try:
                self.kite.cancel_order(self.kite.VARIETY_REGULAR, entry_oid)
            except Exception:
                pass
            if alert_fn:
                alert_fn(
                    f"⏱️ *ENTRY TIMEOUT (0 filled)*: `{symbol}`\n"
                    f"Order cancelled. No position."
                )
            trade["entry_cancelled"] = True
            return trade

        # Partial fill — cancel remaining, adjust SL/target
        try:
            self.kite.cancel_order(self.kite.VARIETY_REGULAR, entry_oid)
        except Exception:
            pass

        actual_price = status.get("average_price") or trade["entry_price"]
        trade = self._adjust_order_quantities(
            trade, filled_qty, actual_price, product
        )
        if alert_fn:
            alert_fn(
                f"[WARN] *PARTIAL FILL*: `{symbol}`\n"
                f"Filled: `{filled_qty}` of `{trade['qty']}` @ Rs.`{actual_price:.2f}`\n"
                f"SL and target adjusted to filled qty."
            )
        return trade

    def _adjust_order_quantities(self, trade: dict, actual_qty: int,
                                  actual_price: float,
                                  product) -> dict:
        """
        Called after entry fill confirms.
        Cancels any existing SL/target (None on first call, real oids on
        partial-fill re-adjustment), then places correct-sized exit orders.
        """
        symbol = trade["symbol"]

        # Cancel existing exit orders if any (partial fill re-adjustment path)
        for oid_key, variety in [
            ("sl_oid",      self.kite.VARIETY_SL),
            ("partial_oid", self.kite.VARIETY_REGULAR),
            ("target_oid",  self.kite.VARIETY_REGULAR),
        ]:
            oid = trade.get(oid_key)
            if oid:   # None on first call — skip
                try:
                    self.kite.cancel_order(variety, oid)
                except Exception:
                    pass

        partial_qty   = max(1, actual_qty // 2)
        remaining_qty = actual_qty - partial_qty

        # V18: All strategies are MIS intraday — place SL-M at exchange
        # for all positions. Intraday hard stop is always correct for MIS.
        new_sl_oid = None
        is_short = trade.get("is_short", False)
        sl_txn = (self.kite.TRANSACTION_TYPE_BUY if is_short
                  else self.kite.TRANSACTION_TYPE_SELL)
        exit_txn = sl_txn
        try:
            new_sl_oid = self.kite.place_order(
                variety=self.kite.VARIETY_SL,
                exchange=self.kite.EXCHANGE_NSE,
                tradingsymbol=symbol,
                transaction_type=sl_txn,
                quantity=actual_qty,
                product=product,
                order_type=self.kite.ORDER_TYPE_SLM,
                trigger_price=round(trade["stop_price"], 1),
                validity=self.kite.VALIDITY_DAY
            )
        except Exception as e:
            print(f"[FillMonitor] SL place failed: {e}")
            new_sl_oid = None

        # Re-place partial exit
        new_partial_oid = None
        partial_price = trade.get("partial_target") or trade.get("partial_target_1")
        if partial_price and partial_qty > 0:
            try:
                new_partial_oid = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NSE,
                    tradingsymbol=symbol,
                    transaction_type=exit_txn,
                    quantity=partial_qty,
                    product=product,
                    order_type=self.kite.ORDER_TYPE_LIMIT,
                    price=round(partial_price, 1),
                    validity=self.kite.VALIDITY_DAY
                )
            except Exception as e:
                print(f"[FillMonitor] Partial re-place failed: {e}")

        # Re-place full target
        try:
            new_target_oid = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NSE,
                tradingsymbol=symbol,
                transaction_type=exit_txn,
                quantity=remaining_qty if new_partial_oid else actual_qty,
                product=product,
                order_type=self.kite.ORDER_TYPE_LIMIT,
                price=round(trade["target_price"], 1),
                validity=self.kite.VALIDITY_DAY
            )
        except Exception as e:
            print(f"[FillMonitor] Target re-place failed: {e}")
            new_target_oid = None

        # Update trade dict
        trade.update({
            "qty":           actual_qty,
            "partial_qty":   partial_qty,
            "remaining_qty": remaining_qty,
            "entry_price":   actual_price,
            "sl_oid":        new_sl_oid,
            "partial_oid":   new_partial_oid,
            "target_oid":    new_target_oid,
        })

        # Persist adjusted state
        self.state.save(trade["entry_oid"], trade)
        return trade

    def check_partial_exit_filled(self, trade: dict,
                                   order_map: dict = None) -> bool:
        """
        Called during position monitoring.
        Returns True if partial exit has filled — so we stop tracking that order.
        Accepts order_map pre-fetched by monitor_positions — zero extra REST.
        Falls back to get_order_status() (one REST call) if map not provided.
        CRITICAL: Also reduces SL-M qty at exchange after partial fill.
        """
        partial_oid = trade.get("partial_oid")
        if not partial_oid or trade.get("partial_filled"):
            return False

        # Use pre-fetched order_map if available — avoids redundant kite.orders() call
        if order_map and str(partial_oid) in order_map:
            raw = order_map[str(partial_oid)]
            status = {
                "status":     raw.get("status", "UNKNOWN"),
                "filled_qty": raw.get("filled_quantity", 0),
            }
        else:
            status = self.get_order_status(partial_oid)
            
        if status["status"] == "COMPLETE":
            remaining = trade["remaining_qty"]
            self.state.mark_partial_filled(trade["entry_oid"], remaining)
            trade["partial_filled"] = True

            # Reduce SL quantity to remaining shares
            # Example: entry 100 shares, partial target filled 50,
            # remaining = 50. SL-M must be modified to 50 or it will
            # sell 100 when only 50 are held -> naked short.
            if trade.get("sl_oid") and remaining > 0:
                try:
                    self.kite.modify_order(
                        variety=self.kite.VARIETY_SL,
                        order_id=trade["sl_oid"],
                        quantity=remaining
                    )
                except Exception as e:
                    self._alert(
                        f"[WARN] *SL MODIFY FAILED* `{trade.get('symbol', '?')}`\n"
                        f"Remaining qty {remaining} not applied to SL-M.\n"
                        f"Manual intervention may be needed.\n`{e}`"
                    )
                    # Non-fatal: log it. _check_exit_filled will still catch
                    # the SL fill and clean up the trade correctly.
            return True
        return False
