"""
DailyCache — loads historical data for all universe tokens via REST once
at 8:45 AM before market opens. All scanner computations (EMA, RSI,
Bollinger, pivot, avg volume, turnover) run against this cache during
the session. Zero historical REST calls during trading hours.

Why: historical_data() REST takes ~300ms per call.
     160 stocks × 4 REST calls = 640 calls × 300ms = ~3 minutes.
     Load it all at 8:45 AM once. Cache it. Done.

What is cached per token:
  closes[]          — last 260 daily close prices (v10: was 70)
  volumes[]         — last 260 daily volumes
  ema25             — 25-day EMA of closes
  rsi14             — latest RSI(14) value
  bb_lower          — Bollinger lower band (20, 2σ)
  avg_daily_vol     — 20-day average daily volume
  avg_turnover_cr   — 20-day average daily turnover in Crores
  pivot_support     — nearest pivot support price
  upper_circuit     — upper circuit limit (from quote REST)
  lower_circuit     — lower circuit limit (from quote REST)

[v10] Minervini additions:
  sma50             — 50-day SMA
  sma150            — 150-day SMA
  sma200            — 200-day SMA
  sma200_up         — True if SMA200 is rising over last 20 trading days
  high_52w          — 52-week (260d) high
  low_52w           — 52-week (260d) low
  rs_score          — Custom 1–99 RS score (computed cross-sectionally after full load)

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

        [v10] Extended to 260 days for SMA200 + 52-week range.
        Returns True on success, False if data fetch largely failed.
        """
        print(f"[DailyCache] Preloading {len(universe)} tokens (260d)...")
        loaded = 0
        failed = 0

        for token, symbol in universe.items():
            try:
                data = self._fetch_daily(token, days=260)
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

                # [v10] Minervini SMA computations
                sma50  = float(np.mean(closes[-50:])) if len(closes) >= 50 else 0.0
                sma150 = float(np.mean(closes[-150:])) if len(closes) >= 150 else 0.0
                sma200 = float(np.mean(closes[-200:])) if len(closes) >= 200 else 0.0

                # SMA200 trending up: compare current vs 20 days ago
                sma200_up = False
                if len(closes) >= 220:
                    sma200_prev = float(np.mean(closes[-220:-20]))
                    sma200_up = sma200 > sma200_prev

                # 52-week high/low
                high_52w = max(closes[-260:]) if len(closes) >= 260 else max(closes)
                low_52w  = min(closes[-260:]) if len(closes) >= 260 else min(closes)

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
                        # [v10] Minervini fields
                        "sma50":          round(sma50, 2),
                        "sma150":         round(sma150, 2),
                        "sma200":         round(sma200, 2),
                        "sma200_up":      sma200_up,
                        "high_52w":       round(high_52w, 2),
                        "low_52w":        round(low_52w, 2),
                        "rs_score":       0,    # computed cross-sectionally below
                        "loaded_at":      now_ist(),
                    }
                loaded += 1

            except Exception as e:
                failed += 1
                print(f"[DailyCache] {symbol} failed: {e}")

            # Rate-limit guard: Zerodha historical_data limit ~3/sec.
            # 160 tokens at 3/sec = ~55s preload. Acceptable at 8:45 AM.
            time.sleep(0.35)

        # Also preload Nifty50 index history for regime + market status detection
        try:
            nd = self._fetch_daily(NIFTY50_TOKEN, days=260)
            if len(nd) >= 30:
                nc = [d["close"] for d in nd]
                nv = [d["volume"] for d in nd]
                nsma50  = float(np.mean(nc[-50:])) if len(nc) >= 50 else 0.0
                nsma150 = float(np.mean(nc[-150:])) if len(nc) >= 150 else 0.0
                nsma200 = float(np.mean(nc[-200:])) if len(nc) >= 200 else 0.0
                nsma200_up = False
                if len(nc) >= 220:
                    nsma200_prev = float(np.mean(nc[-220:-20]))
                    nsma200_up = nsma200 > nsma200_prev
                with self._lock:
                    self._data[NIFTY50_TOKEN] = {
                        "symbol":  "NIFTY50",
                        "closes":  nc,
                        "volumes": nv,
                        "ema25":   self._ema(nc, 25)[-1],
                        "avg_daily_vol": 1.0,
                        "avg_turnover_cr": 0.0,
                        "bb_lower": 0.0,
                        "rsi14":   50.0,
                        "pivot_support": 0.0,
                        "upper_circuit": 0.0,
                        "lower_circuit": 0.0,
                        "sma50":    round(nsma50, 2),
                        "sma150":   round(nsma150, 2),
                        "sma200":   round(nsma200, 2),
                        "sma200_up": nsma200_up,
                        "high_52w": round(max(nc[-260:]) if len(nc) >= 260 else max(nc), 2),
                        "low_52w":  round(min(nc[-260:]) if len(nc) >= 260 else min(nc), 2),
                        "rs_score": 0,
                        "loaded_at": now_ist(),
                    }
        except Exception:
            pass

        # [v10] Compute RS scores cross-sectionally (after all tokens loaded)
        self._compute_rs_scores()

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

    def _compute_rs_scores(self):
        """
        [v10] Custom RS score: 1–99 percentile rank.
        Formula: 12m perf × 40% + 3m perf × 30% + 1m perf × 30%
        Ranks all universe tokens against each other. Top 1% = RS 99.
        """
        perfs = {}
        with self._lock:
            for token, d in self._data.items():
                closes = d.get("closes", [])
                if len(closes) < 260:
                    continue
                c_now = closes[-1]
                # 12-month (~252 trading days), 3-month (~63), 1-month (~21)
                p12 = (c_now - closes[-252]) / closes[-252] * 100 if len(closes) >= 252 else 0
                p3  = (c_now - closes[-63])  / closes[-63]  * 100 if len(closes) >= 63 else 0
                p1  = (c_now - closes[-21])  / closes[-21]  * 100 if len(closes) >= 21 else 0
                composite = p12 * 0.4 + p3 * 0.3 + p1 * 0.3
                perfs[token] = composite

        if not perfs:
            return

        # Rank and assign percentile scores (1–99)
        sorted_tokens = sorted(perfs, key=lambda t: perfs[t])
        n = len(sorted_tokens)
        with self._lock:
            for rank, token in enumerate(sorted_tokens, 1):
                rs = max(1, min(99, int(rank / n * 100)))
                if token in self._data:
                    self._data[token]["rs_score"] = rs

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

    # [v10] Minervini read methods
    def get_sma50(self, token: int) -> float:
        with self._lock:
            return self._data.get(token, {}).get("sma50", 0.0)

    def get_sma150(self, token: int) -> float:
        with self._lock:
            return self._data.get(token, {}).get("sma150", 0.0)

    def get_sma200(self, token: int) -> float:
        with self._lock:
            return self._data.get(token, {}).get("sma200", 0.0)

    def get_sma200_up(self, token: int) -> bool:
        with self._lock:
            return self._data.get(token, {}).get("sma200_up", False)

    def get_high_52w(self, token: int) -> float:
        with self._lock:
            return self._data.get(token, {}).get("high_52w", 0.0)

    def get_low_52w(self, token: int) -> float:
        with self._lock:
            return self._data.get(token, {}).get("low_52w", 0.0)

    def get_rs_score(self, token: int) -> int:
        with self._lock:
            return self._data.get(token, {}).get("rs_score", 0)

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

    def _fetch_daily(self, token: int, days: int = 260) -> list:
        from_dt = today_ist() - datetime.timedelta(days=days)
        
        # [Fix] 503 errors on Kite historical API - implement 3x retry with backoff
        for attempt in range(3):
            try:
                return self.kite.historical_data(
                    token, from_dt, today_ist(), "day"
                )
            except Exception as e:
                # If it's a 503 or any other API fail, wait before retrying
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))  # 1s, then 2s
                else:
                    raise e
        return []

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
