import yfinance as yf
import sqlite3
import datetime
import threading
import time
from config import JOURNAL_DB, today_ist

class EarningsAgent:
    """
    Weekly scraper that fetches identical earnings calendar dates for 
    the tracked NSE universe using yfinance. Caches results in SQLite 
    to prevent pulling dates on every tick cycle.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}
        self._loaded = False
        self._init_db()
        self._warm_from_db()
        
    def _init_db(self):
        with sqlite3.connect(JOURNAL_DB) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS earnings_cache (
                    symbol      TEXT PRIMARY KEY,
                    next_date   TEXT,
                    updated_at  TEXT NOT NULL
                )
            """)
            conn.commit()

    def _warm_from_db(self):
        with sqlite3.connect(JOURNAL_DB) as conn:
            try:
                cur = conn.execute("SELECT symbol, next_date, updated_at FROM earnings_cache")
                count = 0
                for row in cur:
                    sym, dt_str, updated = row
                    # Cache is fresh for 7 days
                    upd_dt = datetime.datetime.strptime(updated, "%Y-%m-%d").date()
                    if (today_ist() - upd_dt).days < 7:
                        with self._lock:
                            self._cache[sym] = dt_str
                        count += 1
                if count > 0:
                    self._loaded = True
                    print(f"[EarningsAgent] Warmed {count} dates from SQLite")
            except Exception as e:
                print(f"[EarningsAgent] Cache warm error: {e}")

    def fetch_earnings_date(self, symbol: str) -> str:
        """Fetches from yfinance. Returns YYYY-MM-DD or empty string."""
        try:
            t = yf.Ticker(f"{symbol}.NS")
            cal = t.calendar
            if cal and "Earnings Date" in cal:
                dates = cal["Earnings Date"]
                if dates:
                    # Filter for future dates (yfinance returns datetime.date)
                    future_dates = [d for d in dates if d >= today_ist()]
                    if future_dates:
                        return min(future_dates).strftime("%Y-%m-%d")
        except Exception:
            pass
        return ""

    def preload(self, symbols: list):
        """Called weekly/daily to refresh earnings dates for all UNIVERSE symbols."""
        if self._loaded and len(self._cache) >= len(symbols) * 0.9:
            print("[EarningsAgent] Preload skipped (cache already warm)")
            return

        print(f"[EarningsAgent] Preloading earnings dates...")
        
        # Filter symbols that truly need fetching (not fresh in DB)
        to_fetch = []
        for sym in symbols:
            with self._lock:
                if sym not in self._cache:
                    to_fetch.append(sym)

        if not to_fetch:
            self._loaded = True
            print("[EarningsAgent] Preload complete. 0 new dates fetched.")
            return

        print(f"[EarningsAgent] Using ThreadPool to fetch {len(to_fetch)} symbols concurrently...")
        import concurrent.futures
        
        def worker(sym):
            dt_str = self.fetch_earnings_date(sym)
            if dt_str:
                with self._lock:
                    self._cache[sym] = dt_str
                # SQLite per-thread connection
                with sqlite3.connect(JOURNAL_DB, timeout=20) as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO earnings_cache (symbol, next_date, updated_at) VALUES (?, ?, ?)",
                        (sym, dt_str, str(today_ist()))
                    )
                    conn.commit()

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(worker, to_fetch))
        
        self._loaded = True
        print(f"[EarningsAgent] Preload complete. Fetched {len(to_fetch)} new dates.")

    def is_earnings_imminent(self, symbol: str, days: int = 5) -> bool:
        """Returns True if earnings are within next `days` days. Blocks swing entries."""
        with self._lock:
            dt_str = self._cache.get(symbol)
            if not dt_str:
                return False
        
        try:
            edate = datetime.datetime.strptime(dt_str, "%Y-%m-%d").date()
            diff = (edate - today_ist()).days
            return 0 <= diff <= days
        except ValueError:
            return False
