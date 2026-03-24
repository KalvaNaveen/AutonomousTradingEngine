"""
PaperBroker — drop-in replacement for KiteConnect in paper mode.

All data calls (instruments, historical_data, quote, ltp) pass through
to real Kite — your scanner, regime detection, and signal logic run on
actual live NSE data.

All order calls (place_order, cancel_order, orders) are intercepted and
routed to an in-memory virtual order book. No real order ever reaches Zerodha.

Fill simulation:
  MARKET        → fills immediately at real LTP
  LIMIT BUY     → fills when real LTP <= limit price
  LIMIT SELL    → fills when real LTP >= limit price
  SL-M SELL     → fills when real LTP <= trigger price

orders() returns virtual orders in the exact Kite format so FillMonitor
works without any modification.
"""

import uuid
import time
import datetime
import threading
from kiteconnect import KiteConnect
from config import TOTAL_CAPITAL


class PaperBroker:
    """Wraps KiteConnect. Data = real. Orders = virtual."""

    # ── KiteConnect constants (must match exactly) ────────────────────
    PRODUCT_CNC        = "CNC"
    PRODUCT_MIS        = "MIS"
    PRODUCT_NRML       = "NRML"
    EXCHANGE_NSE       = "NSE"
    EXCHANGE_BSE       = "BSE"
    VARIETY_REGULAR    = "regular"
    VARIETY_SL         = "sl"
    VARIETY_SLM        = "sl-m"
    ORDER_TYPE_MARKET  = "MARKET"
    ORDER_TYPE_LIMIT   = "LIMIT"
    ORDER_TYPE_SLM     = "SL-M"
    ORDER_TYPE_SL      = "SL"      # stop-limit (trigger + limit price)
    TRANSACTION_TYPE_BUY  = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    VALIDITY_DAY = "DAY"
    VALIDITY_IOC = "IOC"

    def __init__(self, real_kite: KiteConnect, capital: float = TOTAL_CAPITAL,
                 tick_store=None, symbol_token: dict = None):
        self.real_kite         = real_kite
        self.capital           = capital
        self.available_margin  = capital
        self._orders           = {}   # order_id → order dict
        self._positions        = {}   # symbol → {qty, avg_price, product}
        self._realised_pnl     = 0.0
        self._lock             = threading.Lock()
        self._running          = True
        self.tick_store        = tick_store      # TickStore — zero-HTTP fill checks
        self._symbol_token     = symbol_token or {}  # symbol → instrument token
        threading.Thread(target=self._fill_loop, daemon=True).start()

    # ── Data pass-through (ALL real Kite) ────────────────────────────

    def instruments(self, exchange=None):
        return self.real_kite.instruments(exchange)

    def historical_data(self, instrument_token, from_date, to_date,
                        interval, continuous=False, oi=False):
        return self.real_kite.historical_data(
            instrument_token, from_date, to_date, interval, continuous, oi
        )

    def quote(self, instruments):
        return self.real_kite.quote(instruments)

    def ltp(self, instruments):
        return self.real_kite.ltp(instruments)

    def set_access_token(self, token):
        self.real_kite.set_access_token(token)

    # ── Order interception ────────────────────────────────────────────

    def place_order(self, variety, exchange, tradingsymbol,
                    transaction_type, quantity, product, order_type,
                    price=None, trigger_price=None,
                    validity=None, **kwargs) -> str:
        oid = f"PAPER_{uuid.uuid4().hex[:10].upper()}"
        order = {
            "order_id":          oid,
            "variety":           variety,
            "tradingsymbol":     tradingsymbol,
            "exchange":          exchange,
            "transaction_type":  transaction_type,
            "quantity":          quantity,
            "product":           product,
            "order_type":        order_type,
            "price":             price or 0.0,
            "trigger_price":     trigger_price or 0.0,
            "status":            "OPEN",
            "filled_quantity":   0,
            "pending_quantity":  quantity,
            "average_price":     0.0,
            "placed_at":         datetime.datetime.now(),
        }
        with self._lock:
            self._orders[oid] = order

        # MARKET orders fill immediately at real LTP
        if order_type == self.ORDER_TYPE_MARKET:
            ltp = self._ltp(tradingsymbol)
            if ltp > 0:
                self._fill(oid, ltp)

        return oid

    def cancel_order(self, variety, order_id):
        with self._lock:
            o = self._orders.get(str(order_id))
            if o and o["status"] == "OPEN":
                o["status"] = "CANCELLED"
                o["pending_quantity"] = 0
        return order_id

    def modify_order(self, variety, order_id, quantity=None,
                     price=None, trigger_price=None, **kwargs):
        """
        Paper-mode simulation of Kite's modify_order.
        Used by FillMonitor to reduce SL-M qty after partial fill.
        """
        with self._lock:
            o = self._orders.get(str(order_id))
            if o and o["status"] == "OPEN":
                if quantity is not None:
                    o["quantity"] = quantity
                    o["pending_quantity"] = quantity
        return order_id

    def orders(self) -> list:
        """
        Returns virtual order book in exact Kite format.
        FillMonitor.get_order_status() calls this — must match.
        """
        with self._lock:
            return list(self._orders.values())

    def positions(self) -> dict:
        day = []
        with self._lock:
            for sym, pos in self._positions.items():
                if pos["qty"] != 0:
                    ltp = self._ltp(sym)
                    day.append({
                        "tradingsymbol": sym,
                        "quantity":      pos["qty"],
                        "average_price": pos["avg_price"],
                        "last_price":    ltp,
                        "pnl":           (ltp - pos["avg_price"]) * pos["qty"],
                        "product":       pos["product"],
                    })
        return {"day": day, "net": day}

    # ── Fill engine ───────────────────────────────────────────────────

    def _fill_loop(self):
        """Polls real LTP every 30 seconds. Fills pending orders if conditions met."""
        while self._running:
            time.sleep(30)
            with self._lock:
                open_orders = [o for o in self._orders.values()
                               if o["status"] == "OPEN"
                               and o["order_type"] != self.ORDER_TYPE_MARKET]
            for order in open_orders:
                try:
                    ltp = self._ltp(order["tradingsymbol"])
                    if ltp <= 0:
                        continue
                    self._check_fill(order["order_id"], ltp)
                except Exception:
                    pass

    def _check_fill(self, oid: str, ltp: float):
        with self._lock:
            o = self._orders.get(oid)
            if not o or o["status"] != "OPEN":
                return

        ot = o["order_type"]
        tt = o["transaction_type"]

        should_fill = False
        fill_price  = ltp

        if ot == self.ORDER_TYPE_LIMIT:
            if tt == self.TRANSACTION_TYPE_BUY and ltp <= o["price"]:
                should_fill = True
                fill_price  = min(ltp, o["price"])
            elif tt == self.TRANSACTION_TYPE_SELL and ltp >= o["price"]:
                should_fill = True
                fill_price  = max(ltp, o["price"])

        elif ot in (self.ORDER_TYPE_SLM, self.ORDER_TYPE_SL):
            if tt == self.TRANSACTION_TYPE_SELL and ltp <= o["trigger_price"]:
                should_fill = True   # SL triggered — market fill at LTP
                # SL (stop-limit): cap fill at limit price, don't fill below it
                if ot == self.ORDER_TYPE_SL and o["price"] > 0:
                    fill_price = max(ltp, o["price"])
            elif tt == self.TRANSACTION_TYPE_BUY and ltp >= o["trigger_price"]:
                should_fill = True
                if ot == self.ORDER_TYPE_SL and o["price"] > 0:
                    fill_price = min(ltp, o["price"])

        if should_fill:
            self._fill(oid, fill_price)

    def _fill(self, oid: str, fill_price: float):
        with self._lock:
            o = self._orders.get(oid)
            if not o or o["status"] != "OPEN":
                return
            o["status"]           = "COMPLETE"
            o["filled_quantity"]  = o["quantity"]
            o["pending_quantity"] = 0
            o["average_price"]    = fill_price

            sym = o["tradingsymbol"]
            qty = o["quantity"]
            if sym not in self._positions:
                self._positions[sym] = {"qty": 0, "avg_price": 0.0,
                                        "product": o["product"]}
            pos = self._positions[sym]

            if o["transaction_type"] == self.TRANSACTION_TYPE_BUY:
                total = pos["avg_price"] * pos["qty"] + fill_price * qty
                pos["qty"] += qty
                pos["avg_price"] = total / pos["qty"] if pos["qty"] else 0
                self.available_margin -= fill_price * qty
            else:
                realised = (fill_price - pos["avg_price"]) * qty
                self._realised_pnl += realised
                pos["qty"] -= qty
                self.available_margin += fill_price * qty
                if pos["qty"] == 0:
                    pos["avg_price"] = 0.0

    def _ltp(self, symbol: str) -> float:
        """
        tick_store first — zero HTTP, sub-millisecond.
        symbol_token map injected at init from DataAgent.UNIVERSE reverse.
        Falls back to REST only if WebSocket not ready.
        """
        if self.tick_store and self.tick_store.is_ready():
            token = self._symbol_token.get(symbol)
            if token:
                ltp = self.tick_store.get_ltp(token)
                if ltp > 0:
                    return ltp
        try:
            q = self.real_kite.ltp([f"NSE:{symbol}"])
            return q.get(f"NSE:{symbol}", {}).get("last_price", 0.0)
        except Exception:
            return 0.0

    def get_paper_summary(self) -> dict:
        with self._lock:
            filled    = sum(1 for o in self._orders.values()
                            if o["status"] == "COMPLETE")
            cancelled = sum(1 for o in self._orders.values()
                            if o["status"] == "CANCELLED")
            open_cnt  = sum(1 for o in self._orders.values()
                            if o["status"] == "OPEN")
        return {
            "total_orders":     len(self._orders),
            "filled":           filled,
            "cancelled":        cancelled,
            "open":             open_cnt,
            "realised_pnl":     round(self._realised_pnl, 2),
            "available_margin": round(self.available_margin, 2),
            "capital_deployed": round(self.capital - self.available_margin, 2),
        }

    def stop(self):
        self._running = False
