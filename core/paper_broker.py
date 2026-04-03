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

SLIPPAGE_PCT = 0.0004  # 0.04% slippage
BROKERAGE = 20.0       # Per order flat fee

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
        # Signed qty: positive = long, negative = short
        # _positions[sym] = {qty: signed int, avg_price: float, product: str}
        self._positions        = {}   # symbol → {qty, avg_price, product}
        self._realised_pnl     = 0.0
        self._total_brokerage  = 0.0  # track total brokerage separately for clarity
        self._trades_completed = 0    # complete round-trip trades
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

            tt = o["transaction_type"]

            # Add explicit slippage (buy costs more, sell receives less)
            if tt == self.TRANSACTION_TYPE_BUY:
                exec_price = fill_price * (1 + SLIPPAGE_PCT)
            else:
                exec_price = fill_price * (1 - SLIPPAGE_PCT)

            o["status"]           = "COMPLETE"
            o["filled_quantity"]  = o["quantity"]
            o["pending_quantity"] = 0
            o["average_price"]    = exec_price

            sym = o["tradingsymbol"]
            qty = o["quantity"]   # always positive from place_order

            if sym not in self._positions:
                self._positions[sym] = {"qty": 0, "avg_price": 0.0,
                                        "product": o["product"]}
            pos = self._positions[sym]

            # Brokerage charged once per fill
            self._total_brokerage  += BROKERAGE
            self._realised_pnl     -= BROKERAGE  # cost of doing business

            prev_qty = pos["qty"]   # signed: +ve = long held, -ve = short held

            if tt == self.TRANSACTION_TYPE_BUY:
                # ── BUY order ──────────────────────────────────────────
                # Case A: opening a long (prev_qty == 0 or increasing long)
                # Case B: closing a short (prev_qty < 0)
                if prev_qty < 0:
                    # Closing/reducing a short position → realise PnL
                    closing_qty = min(qty, abs(prev_qty))  # shares being closed
                    # Short PnL = (entry_price - exit_price) * qty
                    realised = (pos["avg_price"] - exec_price) * closing_qty
                    self._realised_pnl += realised
                    pos["qty"] += qty   # move toward zero / positive
                    self.available_margin += exec_price * closing_qty  # margin released
                    if pos["qty"] >= 0:
                        self._trades_completed += 1
                        pos["avg_price"] = exec_price if pos["qty"] > 0 else 0.0
                else:
                    # Opening / adding to long
                    total_cost = pos["avg_price"] * pos["qty"] + exec_price * qty
                    pos["qty"] += qty
                    pos["avg_price"] = total_cost / pos["qty"] if pos["qty"] else 0.0
                    self.available_margin -= exec_price * qty  # margin consumed
            else:
                # ── SELL order ─────────────────────────────────────────
                # Case A: closing a long (prev_qty > 0)
                # Case B: opening a short (prev_qty == 0 or increasing short)
                if prev_qty > 0:
                    # Closing / reducing a long position → realise PnL
                    closing_qty = min(qty, prev_qty)
                    realised = (exec_price - pos["avg_price"]) * closing_qty
                    self._realised_pnl += realised
                    pos["qty"] -= qty   # move toward zero / negative
                    self.available_margin += exec_price * closing_qty
                    if pos["qty"] <= 0:
                        self._trades_completed += 1
                        pos["avg_price"] = exec_price if pos["qty"] < 0 else 0.0
                else:
                    # Opening / adding to short (short-sell: margin consumed)
                    total_cost = pos["avg_price"] * abs(pos["qty"]) + exec_price * qty
                    pos["qty"] -= qty   # goes more negative
                    pos["avg_price"] = total_cost / abs(pos["qty"]) if pos["qty"] else 0.0
                    self.available_margin -= exec_price * qty  # margin consumed for short

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
            total_orders = len(self._orders)
            filled    = sum(1 for o in self._orders.values()
                            if o["status"] == "COMPLETE")
            cancelled = sum(1 for o in self._orders.values()
                            if o["status"] == "CANCELLED")
            open_cnt  = sum(1 for o in self._orders.values()
                            if o["status"] == "OPEN")
            # Only count actual entry+exit fills (MARKET/LIMIT orders, not SL/target legs)
            # Round-trips: each completed trade = 1 entry fill + 1 exit fill = 2 fills
            round_trips = self._trades_completed
            # Capital deployed = sum of currently open position value (unrealised exposure)
            open_exposure = sum(
                abs(pos["qty"]) * pos["avg_price"]
                for pos in self._positions.values()
                if pos["qty"] != 0
            )
        return {
            "total_orders":     total_orders,
            "filled":           filled,
            "cancelled":        cancelled,
            "open":             open_cnt,
            "trades_completed": round_trips,
            "realised_pnl":     round(self._realised_pnl, 2),
            "total_brokerage":  round(self._total_brokerage, 2),
            "available_margin": round(self.available_margin, 2),
            "capital_deployed": round(open_exposure, 2),
        }

    def stop(self):
        self._running = False
