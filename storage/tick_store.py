"""
TickStore — central in-memory store fed by KiteTicker (FULL mode).

Receives live ticks for all 100 universe tokens + VIX + Nifty50.
Everything that was previously a REST API call per symbol is now
a dictionary lookup: sub-millisecond instead of ~300ms per HTTP round-trip.

Provides:
  get_ltp(token)          → float  — real-time last traded price
  get_depth(token)        → dict   — bid/ask quantities + bid_ask_ratio
  get_volume(token)       → int    — accumulated intraday volume
  get_day_open(token)     → float  — day's opening price
  get_candles_5min(token) → list   — [{open, high, low, close, volume}, ...]
  get_advance_count(tokens) → (int, int)  — (advancing, declining) count
  is_ready()              → bool  — True once first tick batch received

5-min candle building:
  Ticks are bucketed into 5-minute windows based on their timestamp.
  Each new bucket closes the previous candle and starts a fresh one.
  This replaces get_intraday_ohlcv(token, "5minute") entirely.
"""

import datetime
import threading
from collections import defaultdict
from config import now_ist


def _bucket_5min(ts: datetime.datetime) -> datetime.datetime:
    """Round timestamp down to nearest 5-minute boundary."""
    return ts.replace(second=0, microsecond=0,
                      minute=(ts.minute // 5) * 5)


class TickStore:

    STALE_THRESHOLD_SECS = 10   # price older than this is treated as stale

    def __init__(self):
        self._store  = defaultdict(lambda: {
            "last_price": 0.0,
            "volume":     0,
            "day_open":   0.0,
            "day_high":   0.0,
            "day_low":    0.0,
            "change_pct": 0.0,
            "depth":      {"bids": [], "asks": [], "bid_ask_ratio": 1.0},
            "candles_5min":    [],
            "_current_candle": None,
            "last_tick_at":    None,   # datetime of last received tick
        })
        # [V16] VWAP: running cumulative price×volume and cumulative volume
        self._vwap = defaultdict(lambda: {
            "cum_pv": 0.0,       # Σ(price × volume)
            "cum_vol": 0,        # Σ(volume)
            "vwap": 0.0,         # current VWAP
            "sum_sq_dev": 0.0,   # for standard deviation band
            "tick_count": 0,
        })
        # [V16] ORB: Opening Range high/low (first 15 min: 09:15–09:30)
        self._orb = defaultdict(lambda: {
            "orb_high": 0.0,
            "orb_low":  999999.0,
            "orb_locked": False,  # True after 09:30
        })
        self._lock         = threading.Lock()
        self._ready        = False
        self._last_tick_at = None   # most recent tick time across all tokens

    # ── KiteTicker callback ───────────────────────────────────────────

    def on_ticks(self, ws, ticks: list):
        """
        Called by KiteTicker on every tick event.
        Lock held only per-token for price/depth writes.
        Candle building happens outside the global lock to reduce contention.
        """
        candle_updates = []   # collected outside lock, processed after

        for tick in ticks:
            token = tick.get("instrument_token")
            if not token:
                continue

            ltp = tick.get("last_price", 0)
            vol = tick.get("last_quantity", 0)
            ts  = tick.get("exchange_timestamp") or tick.get("last_trade_time")

            # Write price/depth under lock — fast, no computation
            with self._lock:
                s = self._store[token]
                if ltp:
                    s["last_price"]    = ltp
                    s["last_tick_at"]  = now_ist()
                s["volume"]     = tick.get("volume", s["volume"])
                s["change_pct"] = tick.get("change", s["change_pct"])

                ohlc = tick.get("ohlc", {})
                if ohlc:
                    s["day_open"] = ohlc.get("open", s["day_open"])
                    s["day_high"] = ohlc.get("high", s["day_high"])
                    s["day_low"]  = ohlc.get("low",  s["day_low"])

                depth = tick.get("depth", {})
                if depth:
                    bids = depth.get("buy",  [])
                    asks = depth.get("sell", [])
                    bq   = sum(b.get("quantity", 0) for b in bids)
                    aq   = sum(a.get("quantity", 0) for a in asks)
                    s["depth"] = {
                        "bids":          bids,
                        "asks":          asks,
                        "bid_qty":       bq,
                        "ask_qty":       aq,
                        "bid_ask_ratio": bq / max(aq, 1),
                    }

                    # [V16] Track depth history for absorption detection (last 60 snapshots)
                    hist = s.get("_depth_history")
                    if hist is None:
                        s["_depth_history"] = []
                        hist = s["_depth_history"]
                    hist.append({"bq": bq, "aq": aq, "ratio": bq / max(aq, 1), "ltp": ltp})
                    if len(hist) > 60:
                        s["_depth_history"] = hist[-60:]

                # [V16] Cumulative Delta: buy_vol - sell_vol (trade-by-trade)
                # Kite FULL mode: if trade price >= ask → buyer initiated
                #                  if trade price <= bid → seller initiated
                if ltp > 0 and vol > 0:
                    cd = s.get("_cum_delta", 0.0)
                    best_ask = 0.0
                    best_bid = 0.0
                    if s.get("depth", {}).get("asks"):
                        best_ask = s["depth"]["asks"][0].get("price", 0)
                    if s.get("depth", {}).get("bids"):
                        best_bid = s["depth"]["bids"][0].get("price", 0)

                    if best_ask > 0 and ltp >= best_ask:
                        cd += vol       # Buyer aggressor (lifting the ask)
                    elif best_bid > 0 and ltp <= best_bid:
                        cd -= vol       # Seller aggressor (hitting the bid)
                    s["_cum_delta"] = cd

                    # [V16] Large order detection — flag blocks > 5x avg trade size
                    avg_size = s.get("_avg_trade_size", vol)
                    trade_count = s.get("_trade_count", 0) + 1
                    avg_size = avg_size + (vol - avg_size) / trade_count  # running average
                    s["_avg_trade_size"] = avg_size
                    s["_trade_count"] = trade_count
                    s["_last_large_order"] = vol > (avg_size * 5) if avg_size > 0 else False

                # [V16] VWAP update — incremental on every tick
                if ltp > 0 and vol > 0:
                    v = self._vwap[token]
                    v["cum_pv"]  += ltp * vol
                    v["cum_vol"] += vol
                    if v["cum_vol"] > 0:
                        v["vwap"] = v["cum_pv"] / v["cum_vol"]
                    v["tick_count"] += 1

                # [V16] ORB tracking — first 15 minutes
                orb = self._orb[token]
                if not orb["orb_locked"] and ltp > 0:
                    tick_time = ts or now_ist()
                    if isinstance(tick_time, datetime.datetime):
                        t = tick_time.time()
                        if t < datetime.time(9, 30):
                            if ltp > orb["orb_high"]:
                                orb["orb_high"] = ltp
                            if ltp < orb["orb_low"]:
                                orb["orb_low"] = ltp
                        else:
                            orb["orb_locked"] = True

            # Queue candle update — processed below, lock not held
            if ts and isinstance(ts, datetime.datetime) and ltp > 0:
                candle_updates.append((token, ltp, vol, ts))

        # Process candle updates — one lock acquisition per token
        for token, ltp, vol, ts in candle_updates:
            self._update_candle(token, ltp, vol, ts)

        with self._lock:
            self._ready        = True
            self._last_tick_at = now_ist()

    def _update_candle(self, token: int, ltp: float,
                       vol: int, ts: datetime.datetime):
        """Update 5-min candle for one token. Acquires lock independently."""
        bucket = _bucket_5min(ts)
        with self._lock:
            s  = self._store[token]
            cc = s["_current_candle"]

            if cc is None or cc["bucket"] != bucket:
                if cc is not None:
                    s["candles_5min"].append({
                        "open":   cc["open"],  "high": cc["high"],
                        "low":    cc["low"],   "close": cc["close"],
                        "volume": cc["volume"],
                    })
                    if len(s["candles_5min"]) > 250:
                        s["candles_5min"] = s["candles_5min"][-250:]
                s["_current_candle"] = {
                    "bucket": bucket, "open": ltp, "high": ltp,
                    "low": ltp, "close": ltp, "volume": vol,
                }
            else:
                cc["high"]    = max(cc["high"], ltp)
                cc["low"]     = min(cc["low"],  ltp)
                cc["close"]   = ltp
                cc["volume"] += vol

    # ── Read interface ────────────────────────────────────────────────

    def get_ltp(self, token: int) -> float:
        with self._lock:
            return self._store[token]["last_price"]

    def get_ltp_if_fresh(self, token: int) -> float:
        """
        Returns LTP only if the last tick for this token arrived within
        STALE_THRESHOLD_SECS seconds. Returns 0.0 if stale or never received.
        ScannerAgent uses this — 0.0 triggers the REST fallback path.
        During market hours, a 10-second-old price means the WebSocket
        has disconnected. Using ghost prices can fire false signals.
        """
        with self._lock:
            s       = self._store[token]
            tick_at = s.get("last_tick_at")
            ltp     = s["last_price"]
        if tick_at is None:
            return 0.0
        age = (now_ist() - tick_at).total_seconds()
        return ltp if age <= self.STALE_THRESHOLD_SECS else 0.0

    def is_fresh(self) -> bool:
        """
        True if any tick arrived within STALE_THRESHOLD_SECS globally.
        Use this instead of is_ready() in scan paths — is_ready() stays
        True permanently even after the WebSocket disconnects.
        """
        if self._last_tick_at is None:
            return False
        age = (now_ist() - self._last_tick_at).total_seconds()
        return age <= self.STALE_THRESHOLD_SECS

    def get_depth(self, token: int) -> dict:
        with self._lock:
            return dict(self._store[token]["depth"])

    def get_volume(self, token: int) -> int:
        with self._lock:
            return self._store[token]["volume"]

    def get_day_open(self, token: int) -> float:
        with self._lock:
            return self._store[token]["day_open"]

    def get_candles_5min(self, token: int) -> list:
        """
        Returns closed 5-min candles plus a synthetic 'current' candle
        built from the in-progress bucket. Always has the latest price.
        """
        with self._lock:
            closed = list(self._store[token]["candles_5min"])
            cc     = self._store[token]["_current_candle"]
        if cc:
            closed.append({
                "open":   cc["open"],
                "high":   cc["high"],
                "low":    cc["low"],
                "close":  cc["close"],
                "volume": cc["volume"],
            })
        return closed

    def get_advance_count(self, tokens: list) -> tuple:
        """Returns (advancing_count, declining_count) for given tokens."""
        adv = dec = 0
        with self._lock:
            for t in tokens:
                chg = self._store[t]["change_pct"]
                if chg > 0:
                    adv += 1
                elif chg < 0:
                    dec += 1
        return adv, dec

    def is_ready(self) -> bool:
        return self._ready

    # ── [V16] VWAP + ORB readers ──────────────────────────────────────

    def get_vwap(self, token: int) -> float:
        """Returns current intraday VWAP for a token."""
        with self._lock:
            return self._vwap[token]["vwap"]

    def get_vwap_distance(self, token: int) -> float:
        """Returns (ltp - vwap) / vwap as a fraction. Positive = above VWAP."""
        with self._lock:
            vwap = self._vwap[token]["vwap"]
            ltp  = self._store[token]["last_price"]
        if vwap <= 0:
            return 0.0
        return (ltp - vwap) / vwap

    def get_orb(self, token: int) -> dict:
        """
        Returns ORB (Opening Range Breakout) data.
        {"orb_high": float, "orb_low": float, "orb_locked": bool, "orb_range_pct": float}
        """
        with self._lock:
            orb = dict(self._orb[token])
        if orb["orb_high"] > 0 and orb["orb_low"] < 999999:
            orb["orb_range_pct"] = (orb["orb_high"] - orb["orb_low"]) / orb["orb_low"]
        else:
            orb["orb_range_pct"] = 0.0
        return orb

    def reset_daily(self):
        """
        [V16] Reset VWAP and ORB data for a new trading day.
        Call at pre_market or after end_of_day.
        """
        with self._lock:
            self._vwap.clear()
            self._orb.clear()
            for s in self._store.values():
                s["day_open"] = 0.0
                s["day_high"] = 0.0
                s["day_low"]  = 0.0
                s["volume"]   = 0
                # Intentionally maintaining historical candles_5min for indicator warmup across days
