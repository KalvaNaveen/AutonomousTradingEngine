import datetime
import numpy as np
import pandas as pd
from kiteconnect import KiteConnect
from config import *


class DataAgent:
    UNIVERSE = {}

    NIFTY50_SYMBOLS = [
        "RELIANCE", "TCS", "HDFCBANK", "INFY", "HINDUNILVR",
        "ICICIBANK", "KOTAKBANK", "SBIN", "BHARTIARTL", "ITC",
        "AXISBANK", "LT", "WIPRO", "HCLTECH", "ASIANPAINT",
        "BAJFINANCE", "MARUTI", "SUNPHARMA", "TITAN", "POWERGRID",
        "NTPC", "ULTRACEMCO", "TECHM", "NESTLEIND", "BAJAJFINSV",
        "ONGC", "TATAMOTORS", "TATASTEEL", "ADANIENT", "ADANIPORTS",
        "COALINDIA", "DIVISLAB", "DRREDDY", "EICHERMOT", "GRASIM",
        "HEROMOTOCO", "HINDALCO", "JSWSTEEL", "M&M", "CIPLA",
        "BRITANNIA", "APOLLOHOSP", "BPCL", "SBILIFE", "HDFCLIFE",
        "INDUSINDBK", "BAJAJ-AUTO", "TATACONSUM", "UPL", "SHREECEM"
    ]

    NIFTYNEXT50_SYMBOLS = [
        "ABB", "ADANIGREEN", "ADANITRANS", "AMBUJACEM", "APLAPOLLO",
        "ATGL", "BAJAJHLDNG", "BANKBARODA", "BEL", "BHEL",
        "BOSCHLTD", "CANBK", "CHOLAFIN", "COLPAL", "DABUR",
        "DLF", "DMART", "GAIL", "GODREJCP", "HAVELLS",
        "ICICIPRULI", "INDUSTOWER", "IRCTC", "JINDALSTEL", "LICI",
        "LTIM", "LTTS", "LUPIN", "MARICO", "MCDOWELL-N",
        "MPHASIS", "NAUKRI", "NHPC", "NMDC", "OFSS",
        "PAGEIND", "PEL", "PIIND", "PNB", "RECLTD",
        "SIEMENS", "SRF", "TATACOMM", "TATAPOWER", "TORNTPHARM",
        "TRENT", "VBL", "VEDL", "ZYDUSLIFE", "ZOMATO"
    ]

    def __init__(self, kite: KiteConnect,
                 tick_store=None, daily_cache=None):
        self.kite        = kite
        self.tick_store  = tick_store   # TickStore — live WebSocket feed
        self.daily_cache = daily_cache  # DailyCache — pre-market REST batch
        self.load_universe()

    def load_universe(self):
        """
        Loads NSE EQ instruments and hard-filters to
        Nifty50 + NiftyNext50 = 100 symbols.
        Full NSE universe (~2000 symbols) never scanned.
        One HTTP call at startup. Done.
        """
        try:
            instruments = self.kite.instruments("NSE")
            df          = pd.DataFrame(instruments)
            df          = df[(df['instrument_type'] == 'EQ') &
                             (df['segment'] == 'NSE')]
            target      = set(self.NIFTY50_SYMBOLS + self.NIFTYNEXT50_SYMBOLS)
            df          = df[df['tradingsymbol'].isin(target)]
            self.UNIVERSE = dict(zip(df['instrument_token'],
                                     df['tradingsymbol']))
            print(f"[DataAgent] Universe loaded: "
                  f"{len(self.UNIVERSE)}/100 symbols")
        except Exception as e:
            print(f"[DataAgent] Universe error: {e}")
            self.UNIVERSE = {}

    def get_daily_ohlcv(self, token: int, days: int = 70) -> list:
        """Always REST — historical data not available from WebSocket."""
        try:
            from_date = today_ist() - datetime.timedelta(days=days)
            return self.kite.historical_data(token, from_date,
                                              today_ist(), "day")
        except Exception:
            return []

    def get_intraday_ohlcv(self, token: int, interval: str = "5minute") -> list:
        """
        WebSocket first: return tick_store 5-min candles if available.
        REST fallback: used pre-market or if tick_store not ready.
        """
        if (self.tick_store and self.tick_store.is_ready() and
                interval == "5minute"):
            candles = self.tick_store.get_candles_5min(token)
            if len(candles) >= 2:
                return candles
        # REST fallback
        try:
            today = today_ist()
            return self.kite.historical_data(token, today, today, interval)
        except Exception:
            return []

    def get_order_depth(self, token: int) -> dict:
        """WebSocket first — depth in FULL mode tick. REST fallback."""
        if self.tick_store and self.tick_store.is_ready():
            depth = self.tick_store.get_depth(token)
            if depth.get("bid_ask_ratio", 0) > 0:
                return depth
        # REST fallback
        try:
            symbol = self.UNIVERSE.get(token, "")
            q      = self.kite.quote([f"NSE:{symbol}"])
            key    = f"NSE:{symbol}"
            depth  = q[key]["depth"]
            bids   = depth["buy"]
            asks   = depth["sell"]
            bq     = sum(b["quantity"] for b in bids)
            aq     = sum(a["quantity"] for a in asks)
            return {
                "bids": bids, "asks": asks,
                "bid_qty": bq, "ask_qty": aq,
                "bid_ask_ratio": bq / max(aq, 1),
                "last_price": q[key]["last_price"],
                "volume": q[key]["volume"],
            }
        except Exception:
            return {}

    def get_quote(self, symbol: str) -> dict:
        """
        For current price: tick_store LTP.
        For circuit limits: still REST (not in ticks).
        """
        token = next(
            (t for t, s in self.UNIVERSE.items() if s == symbol), None
        )
        if token and self.tick_store and self.tick_store.is_ready():
            ltp = self.tick_store.get_ltp(token)
            if ltp > 0:
                return {"last_price": ltp, "volume": self.tick_store.get_volume(token)}
        try:
            q = self.kite.quote([f"NSE:{symbol}"])
            return q.get(f"NSE:{symbol}", {})
        except Exception:
            return {}

    def get_india_vix(self) -> float:
        """
        WebSocket first — VIX token subscribed, updates every tick.
        REST fallback uses quote() for live intraday VIX, not historical.
        historical_data() would return yesterday's close — useless intraday.
        """
        if self.tick_store and self.tick_store.is_ready():
            vix = self.tick_store.get_ltp(INDIA_VIX_TOKEN)
            if vix > 0:
                return vix
        # REST fallback — live quote, not daily close
        try:
            q = self.kite.quote([f"NSE:INDIA VIX"])
            vix = q.get("NSE:INDIA VIX", {}).get("last_price", 0.0)
            if vix > 0:
                return vix
        except Exception:
            pass
        # Last resort: yesterday's close is better than a hardcoded default
        try:
            data = self.kite.historical_data(
                INDIA_VIX_TOKEN,
                today_ist() - datetime.timedelta(days=3),
                today_ist(), "day"
            )
            return float(data[-1]["close"]) if data else 16.0
        except Exception:
            return 16.0

    def compute_rvol(self, token: int) -> float:
        """
        Intraday volume from tick_store (live, updates every tick).
        Average volume denominator from daily_cache (precomputed at 8:45 AM).
        Falls back to full REST if neither available.
        """
        if self.tick_store and self.tick_store.is_ready():
            intraday_vol = self.tick_store.get_volume(token)
            if intraday_vol > 0:
                if self.daily_cache and self.daily_cache.is_loaded():
                    avg_vol = self.daily_cache.get_avg_daily_vol(token)
                    return intraday_vol / max(avg_vol, 1)
        # REST fallback
        try:
            hist = self.get_daily_ohlcv(token, days=30)
            if len(hist) < 5:
                return 0.0
            avg_vol = np.mean([d["volume"] for d in hist[-20:]])
            today   = self.get_intraday_ohlcv(token, "60minute")
            if not today:
                return 0.0
            return sum(c["volume"] for c in today) / avg_vol if avg_vol else 0.0
        except Exception:
            return 0.0

    def get_avg_daily_turnover_cr(self, token: int) -> float:
        """daily_cache first — computed at 8:45 AM. REST fallback."""
        if self.daily_cache and self.daily_cache.is_loaded():
            t = self.daily_cache.get_avg_turnover_cr(token)
            if t > 0:
                return t
        hist = self.get_daily_ohlcv(token, days=25)
        if len(hist) < 5:
            return 0.0
        return np.mean([d["volume"] * d["close"] / 1e7 for d in hist[-20:]])

    def check_circuit_breaker(self, symbol: str) -> bool:
        """
        daily_cache has circuit limits refreshed every 15 min.
        REST fallback for pre-market or cache miss.
        """
        token = next(
            (t for t, s in self.UNIVERSE.items() if s == symbol), None
        )
        if token:
            if self.daily_cache and self.daily_cache.is_loaded():
                ltp = (self.tick_store.get_ltp(token)
                       if self.tick_store and self.tick_store.is_ready() else 0.0)
                if ltp > 0:
                    return self.daily_cache.is_circuit_breaker(token, ltp)
        try:
            q     = self.get_quote(symbol)
            upper = q.get("upper_circuit_limit", 0)
            lower = q.get("lower_circuit_limit", 0)
            ltp   = q.get("last_price", 0)
            if ltp <= 0:
                return False
            return (ltp >= upper * 0.999) or (ltp <= lower * 1.001)
        except Exception:
            return False

    def get_advance_decline_ratio(self) -> float:
        """
        tick_store: count tokens where change_pct > 0.
        REST fallback: quote all Nifty50 symbols.
        """
        if self.tick_store and self.tick_store.is_ready():
            tokens  = list(self.UNIVERSE.keys())
            adv, dec = self.tick_store.get_advance_count(tokens)
            total   = adv + dec
            return adv / total if total > 0 else 0.5
        try:
            tokens = [f"NSE:{s}" for s in self.NIFTY50_SYMBOLS]
            quotes = self.kite.quote(tokens)
            adv    = sum(1 for q in quotes.values() if q.get("change", 0) > 0)
            return adv / len(quotes) if quotes else 0.5
        except Exception:
            return 0.5

    def compute_pivot_support(self, token: int) -> float:
        """daily_cache first. REST fallback."""
        if self.daily_cache and self.daily_cache.is_loaded():
            p = self.daily_cache.get_pivot_support(token)
            if p > 0:
                return p
        hist = self.get_daily_ohlcv(token, days=30)
        if len(hist) < 10:
            return 0.0
        lows    = [d["low"] for d in hist]
        current = hist[-1]["close"]
        pivots  = [
            lows[i] for i in range(1, len(lows) - 1)
            if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]
        ]
        supports = [p for p in pivots if p < current]
        return max(supports) if supports else current * 0.93

    @staticmethod
    def compute_ema(prices: list, period: int) -> list:
        if len(prices) < period:
            return prices
        k = 2 / (period + 1)
        ema = [prices[0]]
        for p in prices[1:]:
            ema.append(p * k + ema[-1] * (1 - k))
        return ema

    @staticmethod
    def compute_rsi(prices: list, period: int = 14) -> list:
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
            rs = ag / al if al != 0 else 100
            rsi.append(100 - 100 / (1 + rs))
        return rsi if rsi else [50.0]

    @staticmethod
    def compute_bollinger(prices: list, period: int = 20,
                          sd: float = 2.0) -> tuple:
        if len(prices) < period:
            return prices[-1], prices[-1], prices[-1]
        w = prices[-period:]
        m = np.mean(w)
        s = np.std(w)
        return m + sd * s, m, m - sd * s
