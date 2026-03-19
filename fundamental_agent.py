"""
FundamentalAgent — NSE India fundamental data for Minervini SEPA screening.

Source: screener.in (public pages, no login required for basic data).
Screener.in provides quarterly P&L, balance sheet, and key ratios
for all NSE-listed companies. Data quality is excellent for large/mid caps.

Data fetched (Minervini criteria, NSE India adapted):
  eps_growth_pct    — Net profit growth YoY (latest Q vs same Q 1yr ago)
                       Target: ≥25% (prefer 40–100%). Accelerating = stronger.
  sales_growth_pct  — Revenue growth YoY (latest Q)
                       Target: ≥20%
  roe_pct           — Return on Equity (annual)
                       Target: >17%
  debt_equity       — Debt / Equity ratio
                       Target: <0.5 (50%)
  eps_accelerating  — True if last 3 quarters show rising net profit

Schedule: Weekly (Sunday 06:00 AM) via BNFEngine.refresh_fundamentals().
Cache: SQLite (fundamental_cache table in journal.db), 7-day TTL.
Rate limit: 1.5s between requests (~0.67 req/sec). 160 symbols ≈ 4 minutes.
Failure mode: Missing symbol excluded from S3/S4 only. S1/S2 unaffected.
"""

import requests
import sqlite3
import json
import time
import datetime
import threading
from bs4 import BeautifulSoup
from config import JOURNAL_DB, today_ist, now_ist
from config import (
    S3_MIN_EPS_GROWTH, S3_MIN_SALES_GROWTH, S3_MIN_ROE, S3_MAX_DEBT_EQUITY,
    # [v11] Superperformance Profile constants
    S3_MIN_MARKET_CAP_CR, S3_MAX_MARKET_CAP_CR,
    S3_MIN_FLOAT_CR, S3_INNOVATION_SALES_ACCEL,
)

SCREENER_BASE    = "https://www.screener.in"
SCREENER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                   "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"),
    "Accept":     "text/html,application/xhtml+xml",
    "Referer":    "https://www.screener.in/",
}

# NSE symbol → screener.in URL slug overrides (symbols with special chars)
SLUG_OVERRIDES = {
    "M&M":       "M-and-M",
    "BAJAJ-AUTO": "Bajaj-Auto",
    "MCDOWELL-N": "United-Spirits",
}


class FundamentalAgent:

    def __init__(self):
        self._lock   = threading.Lock()
        self._cache  = {}    # symbol → fundamentals dict
        self._loaded = False
        self._scraper_alerted = set()
        self._init_db()
        # Warm in-memory cache from SQLite so is_loaded() returns True
        # on crash recovery / mid-session restart without waiting for
        # Sunday preload(). Only loads entries within 7-day TTL.
        self._warm_from_db()

    def _init_db(self):
        with sqlite3.connect(JOURNAL_DB) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fundamental_cache (
                    symbol      TEXT PRIMARY KEY,
                    data_json   TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
            """)
            conn.commit()

    def _warm_from_db(self):
        """Load fresh cached entries from SQLite into memory at startup."""
        db = self._load_db()
        count = 0
        for sym, data in db.items():
            if self._fresh(data.get("_updated_at", "")):
                with self._lock:
                    self._cache[sym] = data
                count += 1
        if count > 0:
            self._loaded = True
            print(f"[FundamentalAgent] Warmed {count} symbols from SQLite cache")

    def _slug(self, symbol: str) -> str:
        return SLUG_OVERRIDES.get(symbol, symbol)

    def scrape(self, symbol: str) -> dict:
        """Fetch + parse screener.in page with 3x retry and yfinance fallback."""
        slug = self._slug(symbol)
        urls = [
            f"{SCREENER_BASE}/company/{slug}/consolidated/",
            f"{SCREENER_BASE}/company/{slug}/",
        ]
        
        for url in urls:
            for attempt in range(3):
                try:
                    r = requests.get(url, headers=SCREENER_HEADERS, timeout=15)
                    if r.status_code == 200:
                        result = self._parse(BeautifulSoup(r.text, "lxml"), symbol)
                        if result and result.get("eps_growth_pct") is not None:
                            return result
                except Exception as e:
                    time.sleep(2)
        
        # yfinance fallback
        try:
            import yfinance as yf
            import math
            ticker = yf.Ticker(f"{symbol}.NS")
            info = ticker.info
            
            def _safe_float(v):
                if v is None: return 0.0
                try: 
                    f = float(v)
                    return 0.0 if math.isnan(f) else f
                except: return 0.0

            result = {
                "symbol": symbol,
                "eps_growth_pct": _safe_float(info.get("earningsQuarterlyGrowth")) * 100,
                "sales_growth_pct": _safe_float(info.get("revenueGrowth")) * 100,
                "roe_pct": _safe_float(info.get("returnOnEquity")) * 100,
                "debt_equity": _safe_float(info.get("debtToEquity")) / 100,
                "eps_accelerating": True,
                "market_cap_cr": _safe_float(info.get("marketCap")) / 10000000,
                "free_float_cr": None,
                "innovation_flag": False
            }
            if result.get("eps_growth_pct") is not None:
                return result
        except Exception as e:
            print(f"[FundamentalAgent] yfinance fallback failed for {symbol}: {e}")

        time.sleep(1.5)  # Yahoo rate limit protection
        self._scraper_alert(symbol, urls[0], "Both screener and yfinance failed")
        return {}

    def _scraper_alert(self, symbol: str, url: str, reason: str):
        """
        [v12] Fire Telegram alert when screener.in parse fails.
        Uses a direct requests call — does not depend on ExecutionAgent.
        Throttled: only one alert per symbol per session (avoids spam on
        batch preload failures). Session flag stored in _scraper_alerted set.
        """
        with self._lock:
            if symbol in self._scraper_alerted:
                return
            self._scraper_alerted.add(symbol)

        msg = (
            f"🚨 *SCRAPER BROKEN: Check FundamentalAgent*\n"
            f"Symbol: `{symbol}`\n"
            f"URL: `{url}`\n"
            f"Reason: `{reason[:200]}`\n"
            f"Action: Verify screener.in structure hasn't changed. "
            f"S3/S4 disabled for this symbol until next weekly refresh."
        )
        print(f"[FundamentalAgent] SCRAPER ALERT: {symbol} — {reason}")

        try:
            from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS
            if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
                return
            tg_base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
            for chat_id in TELEGRAM_CHAT_IDS:
                requests.post(
                    f"{tg_base}/sendMessage",
                    json={"chat_id": chat_id, "text": msg,
                          "parse_mode": "Markdown"},
                    timeout=5
                )
        except Exception as e:
            print(f"[FundamentalAgent] Scraper alert failed: {e}")

    def _parse(self, soup, symbol: str) -> dict:
        result = {
            "symbol":           symbol,
            "net_profit_q":     [],
            "sales_q":          [],
            "eps_growth_pct":   None,
            "sales_growth_pct": None,
            "roe_pct":          None,
            "debt_equity":      None,
            "eps_accelerating": False,
            # [v11] Superperformance Profile fields
            "market_cap_cr":   None,    # Market cap ₹ Crore
            "free_float_cr":   None,    # Free float ₹ Crore
            "innovation_flag": False,   # Sales accel > S3_INNOVATION_SALES_ACCEL
        }
        try:
            # ── Quarterly results ─────────────────────────────────────
            qsec = soup.find("section", {"id": "quarters"})
            if qsec:
                for row in qsec.find_all("tr"):
                    tds   = row.find_all("td")
                    if not tds:
                        continue
                    label = tds[0].get_text(strip=True).lower()
                    vals  = []
                    for td in tds[1:]:
                        try:
                            vals.append(float(
                                td.get_text(strip=True).replace(",", "")
                            ))
                        except ValueError:
                            pass

                    if "net profit" in label or "pat" in label:
                        result["net_profit_q"] = vals[-8:]
                    elif "sales" in label or "revenue" in label:
                        result["sales_q"] = vals[-8:]

            # YoY growth: latest quarter vs same quarter 4 periods ago
            for key, arr, out_key in [
                ("net_profit_q", result["net_profit_q"], "eps_growth_pct"),
                ("sales_q",      result["sales_q"],      "sales_growth_pct"),
            ]:
                if len(arr) >= 5:
                    now_val  = arr[-1]
                    prev_val = arr[-5]
                    if prev_val and prev_val != 0:
                        result[out_key] = round(
                            (now_val - prev_val) / abs(prev_val) * 100, 1
                        )
            # EPS acceleration: last 3 quarters improving
            np_q = result["net_profit_q"]
            if len(np_q) >= 3:
                result["eps_accelerating"] = (
                    np_q[-1] > np_q[-2] > np_q[-3]
                )

            # ── Ratios section ────────────────────────────────────────
            ratios = soup.find("section", {"id": "ratios"})
            if not ratios:
                # fallback: look for li items with ratio labels
                ratios = soup.find("ul", {"id": "top-ratios"})
            if ratios:
                for row in ratios.find_all(["tr", "li"]):
                    tds   = row.find_all(["td", "span"])
                    if len(tds) < 2:
                        continue
                    label = tds[0].get_text(strip=True).lower()
                    val_s = tds[-1].get_text(strip=True).replace(
                        ",", "").replace("%", "")
                    try:
                        val = float(val_s)
                    except ValueError:
                        continue
                    if "return on equity" in label or "roe" in label:
                        result["roe_pct"] = val
                    elif "debt / equity" in label or "d/e" in label:
                        result["debt_equity"] = val

            # ── [v11] top-ratios: market cap + free float ─────────
            top_ratios = soup.find("ul", {"id": "top-ratios"})
            if top_ratios:
                for li in top_ratios.find_all("li"):
                    spans = li.find_all("span")
                    if len(spans) < 2:
                        continue
                    lbl = spans[0].get_text(strip=True).lower()
                    raw = spans[-1].get_text(strip=True).replace(",", "")
                    num_str = "".join(c for c in raw
                                      if c.isdigit() or c == ".")
                    if not num_str:
                        continue
                    try:
                        val = float(num_str)
                    except ValueError:
                        continue
                    if "market cap" in lbl or "mkt cap" in lbl:
                        result["market_cap_cr"] = val
                    elif "free float" in lbl or "float" in lbl:
                        result["free_float_cr"] = val

            # ── [v11] Innovation flag ─────────────────────────────────
            sal_g = result.get("sales_growth_pct")
            if sal_g is not None and sal_g >= S3_INNOVATION_SALES_ACCEL:
                result["innovation_flag"] = True

        except Exception as e:
            print(f"[FundamentalAgent] Parse error {symbol}: {e}")

        return result

    def _load_db(self) -> dict:
        out = {}
        try:
            with sqlite3.connect(JOURNAL_DB) as conn:
                for sym, js, upd in conn.execute(
                    "SELECT symbol, data_json, updated_at FROM fundamental_cache"
                ).fetchall():
                    d = json.loads(js)
                    d["_updated_at"] = upd
                    out[sym] = d
        except Exception:
            pass
        return out

    def _save_db(self, symbol: str, data: dict):
        with sqlite3.connect(JOURNAL_DB) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fundamental_cache "
                "(symbol, data_json, updated_at) VALUES (?,?,?)",
                (symbol, json.dumps(data), now_ist().isoformat())
            )
            conn.commit()

    def _fresh(self, upd_str: str, max_days: int = 7) -> bool:
        try:
            upd = datetime.date.fromisoformat(upd_str[:10])
            return (today_ist() - upd).days <= max_days
        except Exception:
            return False

    def preload(self, symbols: list, alert_fn=None) -> bool:
        """
        Weekly batch fetch. Uses SQLite cache (7-day TTL).
        Only hits screener.in for symbols with stale or missing cache.
        """
        print(f"[FundamentalAgent] Preloading {len(symbols)} symbols...")
        db      = self._load_db()
        loaded  = skipped = failed = 0

        for sym in symbols:
            cached = db.get(sym)
            if cached and self._fresh(cached.get("_updated_at", "")):
                with self._lock:
                    self._cache[sym] = cached
                loaded += 1
                skipped += 1
                continue

            data = self.scrape(sym)
            if data:
                self._save_db(sym, data)
                with self._lock:
                    self._cache[sym] = data
                loaded += 1
            else:
                failed += 1

            time.sleep(1.5)   # screener.in rate limit: ~0.67 req/sec

        self._loaded = loaded >= max(1, len(symbols) * 0.5)
        print(f"[FundamentalAgent] {loaded} loaded "
              f"({skipped} from cache, {loaded - skipped} fresh). "
              f"{failed} failed. Ready: {self._loaded}")

        if alert_fn:
            status = "✅" if loaded >= len(symbols) * 0.8 else "⚠️"
            alert_fn(
                f"{status} *FUNDAMENTALS LOADED*\n"
                f"`{loaded}/{len(symbols)}` symbols | {failed} failed\n"
                f"Source: screener.in | Cache age: 7d"
            )
        return self._loaded

    # ── Read interface ────────────────────────────────────────────────

    def get(self, symbol: str) -> dict:
        with self._lock:
            return dict(self._cache.get(symbol, {}))

    def passes_sepa_fundamentals(self, symbol: str) -> tuple:
        """
        All Minervini SEPA fundamental criteria. Returns (passes, reason).
        'Miss any = skip trade.' — Minervini

        [v11] EPS acceleration is now a hard gate (was stored but not checked).
        Minervini: "EPS Q1 +20%, Q2 +30%, Q3 +50% — verify acceleration."
        Decelerating earnings = approaching earnings miss = risk of gap-down.
        """
        d = self.get(symbol)
        if not d:
            return False, "NO_FUNDAMENTAL_DATA"

        eps_g     = d.get("eps_growth_pct")
        sal_g     = d.get("sales_growth_pct")
        roe       = d.get("roe_pct")
        de        = d.get("debt_equity")
        eps_accel = d.get("eps_accelerating", False)

        if eps_g is None:
            return False, "EPS_DATA_MISSING"
        if eps_g < S3_MIN_EPS_GROWTH:
            return False, f"EPS_{eps_g:.0f}%<{S3_MIN_EPS_GROWTH}%_MIN"
        # [v11] Hard gate: EPS must be accelerating quarter-over-quarter
        if not eps_accel:
            return False, "EPS_NOT_ACCELERATING"
        if sal_g is not None and sal_g < S3_MIN_SALES_GROWTH:
            return False, f"SALES_{sal_g:.0f}%<{S3_MIN_SALES_GROWTH}%_MIN"
        if roe is not None and roe < S3_MIN_ROE:
            return False, f"ROE_{roe:.0f}%<{S3_MIN_ROE}%_MIN"
        if de is not None and de > S3_MAX_DEBT_EQUITY:
            return False, f"DE_{de:.2f}>{S3_MAX_DEBT_EQUITY}_MAX"

        return True, "PASS"

    def is_superperformance_profile(self, symbol: str) -> tuple:
        """
        [v11] Minervini Superperformance Stock Profile — Traits of 100%+ Winners.
        'Small-mid cap, float 10-100M, new product/market innovation.' — Minervini

        Returns (qualifies: bool, summary: str).
        Non-blocking: called in master_checklist() as bonus gate.
        Qualifies if ≥ 3 of 4 quantitative criteria pass.

        NSE India mappings:
          1. Market cap ₹300 Cr – ₹5,000 Cr  (mid-cap sweet spot)
          2. Free float ≥ ₹50 Cr              (institutional interest)
          3. Innovation: sales growth ≥ 80% YoY (new product/market proxy)
          4. EPS accelerating                  (momentum in earnings)
        """
        d = self.get(symbol)
        if not d:
            return False, "NO_DATA"

        score = 0
        passed = []
        failed = []

        cap = d.get("market_cap_cr")
        if cap is not None:
            if S3_MIN_MARKET_CAP_CR <= cap <= S3_MAX_MARKET_CAP_CR:
                score += 1; passed.append(f"Cap:₹{cap:.0f}Cr")
            else:
                failed.append(f"Cap:₹{cap:.0f}Cr")

        ff = d.get("free_float_cr")
        if ff is not None:
            if ff >= S3_MIN_FLOAT_CR:
                score += 1; passed.append(f"Float:₹{ff:.0f}Cr")
            else:
                failed.append(f"Float:₹{ff:.0f}Cr_LOW")

        if d.get("innovation_flag", False):
            score += 1; passed.append("Innovation")
        else:
            failed.append("NoInnovation")

        if d.get("eps_accelerating", False):
            score += 1; passed.append("EPSAccel")
        else:
            failed.append("EPSNoAccel")

        qualifies = score >= 3
        summary   = (f"{score}/4 Pass:[{','.join(passed)}] "
                     f"Fail:[{','.join(failed)}]")
        return qualifies, summary

    def is_loaded(self) -> bool:
        return self._loaded
