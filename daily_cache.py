"""
DailyCache — loads historical data for all universe tokens via REST once
at 8:45 AM before market opens. All scanner computations (EMA, RSI,
Bollinger, pivot, avg volume, turnover) run against this cache during
the session. Zero historical REST calls during trading hours.

Why: historical_data() REST takes ~300ms per call.
     100 stocks × 4 REST calls = 400 calls × 300ms = 2 minutes.
     Load it all at 8:45 AM once. Cache it. Done.

What is cached per token:
  closes[]          — last 70 daily close prices
  volumes[]         — last 70 daily volumes
  ema25             — 25-day EMA of closes
  rsi14             — latest RSI(14) value
  bb_lower          — Bollinger lower band (20, 2σ)
  avg_daily_vol     — 20-day average daily volume
  avg_turnover_cr   — 20-day average daily turnover in Crores
  pivot_support     — nearest pivot support price
  upper_circuit     — upper circuit limit (from quote REST)
  lower_circuit     — lower circuit limit (from quote REST)

Circuit limits are refreshed every 15 minutes during trading hours
(infrequent REST, not per-tick).
"""

import datetime
import time
import threading
import numpy as np
from kiteconnect import KiteConnect
from config import NIFTY50_TOKEN, today_ist, now_ist


class DailyCache:

    def __init__(self, kite):
        self.kite  = kite
        self._data = {}       # token → cache dict
        self._lock = threading.Lock()
        self._loaded = False
        self._last_circuit_refresh = None

    def preload(self, universe: dict, alert_fn=None) -> bool:
        """
        Call at 8:45 AM. Fetches and computes all historical data.
        universe: {token: symbol} dict from DataAgent.UNIVERSE

        Returns True on success, False if data fetch largely failed.
        """
        print(f"[DailyCache] Preloading {len(universe)} tokens...")
        loaded = 0
        failed = 0

        for token, symbol in universe.items():
            try:
                data = self._fetch_daily(token, days=75)
                if len(data) < 25:
                    failed += 1
                    continue

                closes  = [d["close"]  for d in data]
                volumes = [d["volume"] for d in data]

                ema25    = self._ema(closes, 25)[-1]
                rsi14    = (self._rsi(closes, 14) or [50.0])[-1]
                _, _, bb = self._bollinger(closes, 20, 2.0)
                avg_vol  = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 1.0
                avg_turn = float(np.mean(
                    [v * c / 1e7 for v, c in zip(volumes[-20:], closes[-20:])]
                )) if len(volumes) >= 20 else 0.0
                pivot    = self._pivot_support(data)

                with self._lock:
                    self._data[token] = {
                        "symbol":         symbol,
                        "closes":         closes,
                        "volumes":        volumes,
                        "ema25":          ema25,
                        "rsi14":          rsi14,
                        "bb_lower":       bb,
                        "avg_daily_vol":  avg_vol,
                        "avg_turnover_cr": avg_turn,
                        "pivot_support":  pivot,
                        "upper_circuit":  0.0,
                        "lower_circuit":  0.0,
                        "loaded_at":      now_ist(),
                    }
                loaded += 1

            except Exception as e:
                failed += 1
                print(f"[DailyCache] {symbol} failed: {e}")

            # Rate-limit guard: Zerodha historical_data limit ~3/sec.
            # 100 tokens at 3/sec = ~35s preload. Acceptable at 8:45 AM.
            time.sleep(0.35)

        # Also preload Nifty50 index history for regime detection
        try:
            nd = self._fetch_daily(NIFTY50_TOKEN, days=75)
            if len(nd) >= 30:
                nc = [d["close"] for d in nd]
                with self._lock:
                    self._data[NIFTY50_TOKEN] = {
                        "symbol":  "NIFTY50",
                        "closes":  nc,
                        "ema25":   self._ema(nc, 25)[-1],
                        "volumes": [d["volume"] for d in nd],
                        "avg_daily_vol": 1.0,
                        "avg_turnover_cr": 0.0,
                        "bb_lower": 0.0,
                        "rsi14":   50.0,
                        "pivot_support": 0.0,
                        "upper_circuit": 0.0,
                        "lower_circuit": 0.0,
                        "loaded_at": now_ist(),
                    }
        except Exception:
            pass

        # Load circuit breaker limits via REST quote (once at open)
        self._refresh_circuit_limits(universe)

        self._loaded = loaded >= max(1, int(len(universe) * 0.8))
        print(f"[DailyCache] Loaded {loaded}/{len(universe)} tokens. "
              f"Failed: {failed}. "
              f"Cache ready: {self._loaded}")

        if alert_fn:
            status = "✅" if loaded > len(universe) * 0.9 else "⚠️"
            alert_fn(f"{status} *DAILY CACHE LOADED*\n"
                     f"`{loaded}` tokens ready | `{failed}` failed")
        return self._loaded

    def refresh_circuit_limits(self, universe: dict):
        """Call every 15 min. Circuit limits rarely change but need to be current."""
        self._refresh_circuit_limits(universe)

    def _refresh_circuit_limits(self, universe: dict):
        """Batch quote call for circuit limits — one call per 500 symbols."""
        symbols = [f"NSE:{sym}" for sym in universe.values()]
        # Kite quote accepts up to 500 at once
        for i in range(0, len(symbols), 500):
            batch = symbols[i:i + 500]
            try:
                quotes = self.kite.quote(batch)
                for key, q in quotes.items():
                    sym   = key.replace("NSE:", "")
                    token = next(
                        (t for t, s in universe.items() if s == sym), None
                    )
                    if token and token in self._data:
                        with self._lock:
                            self._data[token]["upper_circuit"] = q.get(
                                "upper_circuit_limit", 0.0
                            )
                            self._data[token]["lower_circuit"] = q.get(
                                "lower_circuit_limit", 0.0
                            )
            except Exception as e:
                print(f"[DailyCache] Circuit limit refresh error: {e}")
        self._last_circuit_refresh = now_ist()

    # ── Read interface ────────────────────────────────────────────────

    def get(self, token: int) -> dict:
        with self._lock:
            return dict(self._data.get(token, {}))

    def get_closes(self, token: int) -> list:
        with self._lock:
            return list(self._data.get(token, {}).get("closes", []))

    def get_ema25(self, token: int) -> float:
        with self._lock:
            return self._data.get(token, {}).get("ema25", 0.0)

    def get_rsi14(self, token: int) -> float:
        with self._lock:
            return self._data.get(token, {}).get("rsi14", 50.0)

    def get_bb_lower(self, token: int) -> float:
        with self._lock:
            return self._data.get(token, {}).get("bb_lower", 0.0)

    def get_avg_daily_vol(self, token: int) -> float:
        with self._lock:
            return self._data.get(token, {}).get("avg_daily_vol", 1.0)

    def get_avg_turnover_cr(self, token: int) -> float:
        with self._lock:
            return self._data.get(token, {}).get("avg_turnover_cr", 0.0)

    def get_pivot_support(self, token: int) -> float:
        with self._lock:
            return self._data.get(token, {}).get("pivot_support", 0.0)

    def is_circuit_breaker(self, token: int, ltp: float) -> bool:
        with self._lock:
            d = self._data.get(token, {})
        upper = d.get("upper_circuit", 0.0)
        lower = d.get("lower_circuit", 0.0)
        if ltp <= 0 or (upper <= 0 and lower <= 0):
            return False
        return (upper > 0 and ltp >= upper * 0.999) or \
               (lower > 0 and ltp <= lower * 1.001)

    def is_loaded(self) -> bool:
        return self._loaded

    # ── Internal helpers ──────────────────────────────────────────────

    def _fetch_daily(self, token: int, days: int = 75) -> list:
        from_dt = today_ist() - datetime.timedelta(days=days)
        return self.kite.historical_data(
            token, from_dt, today_ist(), "day"
        )

    @staticmethod
    def _ema(prices: list, period: int) -> list:
        if len(prices) < period:
            return prices
        k   = 2 / (period + 1)
        ema = [prices[0]]
        for p in prices[1:]:
            ema.append(p * k + ema[-1] * (1 - k))
        return ema

    @staticmethod
    def _rsi(prices: list, period: int = 14) -> list:
        if len(prices) < period + 1:
            return [50.0]
        deltas = np.diff(prices)
        gains  = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        ag, al = np.mean(gains[:period]), np.mean(losses[:period])
        rsi = []
        for i in range(period, len(deltas)):
            ag = (ag * (period - 1) + gains[i]) / period
            al = (al * (period - 1) + losses[i]) / period
            rsi.append(100 - 100 / (1 + ag / al) if al != 0 else 100.0)
        return rsi or [50.0]

    @staticmethod
    def _bollinger(prices: list, period: int = 20,
                   sd: float = 2.0) -> tuple:
        if len(prices) < period:
            return prices[-1], prices[-1], prices[-1]
        w = prices[-period:]
        m = np.mean(w)
        s = np.std(w)
        return m + sd * s, m, m - sd * s

    @staticmethod
    def _pivot_support(data: list) -> float:
        if len(data) < 10:
            return 0.0
        lows    = [d["low"] for d in data]
        current = data[-1]["close"]
        pivots  = [
            lows[i] for i in range(1, len(lows) - 1)
            if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]
        ]
        supports = [p for p in pivots if p < current]
        return max(supports) if supports else current * 0.93
