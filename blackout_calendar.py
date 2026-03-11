"""
Automatically fetches:
  1. NSE trading holidays (market closed days)
  2. RBI monetary policy dates (high-impact — engine stays out)

Sources:
  NSE: https://www.nseindia.com/api/holiday-master?type=trading
  RBI: Scraped from RBI website policy calendar

Refreshes weekly. Caches to engine_state.db.
Returns set of date strings: {"2026-02-01", "2026-04-09", ...}
"""

import requests
import json
import datetime
import sqlite3
from config import STATE_DB

NSE_HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
RBI_CALENDAR_URL = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx"

NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                   "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"),
    "Accept":     "application/json",
    "Referer":    "https://www.nseindia.com/",
}

# Known RBI policy months — engine stays out these days
# Pattern: 6 meetings/year, typically Feb, Apr, Jun, Aug, Oct, Dec
# These are hardcoded as fallback if scraping fails
RBI_POLICY_MONTHS_2026 = [2, 4, 6, 8, 10, 12]


class BlackoutCalendar:

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(NSE_HEADERS)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(STATE_DB) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS blackout_cache (
                    cache_date  TEXT PRIMARY KEY,
                    dates_json  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
            """)
            conn.commit()

    def _fetch_nse_holidays(self) -> set:
        """Fetch official NSE trading holidays for current year."""
        try:
            r = self.session.get(NSE_HOLIDAY_URL, timeout=15)
            if r.status_code != 200:
                return set()
            data = r.json()
            # NSE returns {"CM": [...], "FO": [...], ...}
            # CM = Capital Markets (equities)
            cm_holidays = data.get("CM", [])
            dates = set()
            for h in cm_holidays:
                # Format: "01-Jan-2026" → "2026-01-01"
                raw = h.get("tradingDate", "")
                if raw:
                    try:
                        dt = datetime.datetime.strptime(raw, "%d-%b-%Y")
                        dates.add(dt.strftime("%Y-%m-%d"))
                    except ValueError:
                        pass
            return dates
        except Exception as e:
            print(f"[BlackoutCalendar] NSE fetch error: {e}")
            return set()

    def _fetch_rbi_policy_dates(self) -> set:
        """
        Attempts to scrape RBI MPC policy announcement dates.
        Falls back to hardcoded monthly pattern if scraping fails.
        RBI announces dates at the start of each year.
        """
        dates = set()
        try:
            # RBI publishes MPC schedule as press release
            r = self.session.get(
                "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid=57948",
                timeout=15
            )
            if r.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text, "lxml")
                # Look for date patterns in table cells
                for td in soup.find_all("td"):
                    text = td.get_text(strip=True)
                    for fmt in ["%B %d, %Y", "%d %B %Y", "%d-%m-%Y"]:
                        try:
                            dt = datetime.datetime.strptime(text, fmt)
                            if dt.year >= datetime.date.today().year:
                                dates.add(dt.strftime("%Y-%m-%d"))
                        except ValueError:
                            continue
        except Exception:
            pass

        # Fallback: if scraping yields nothing, use first Wednesday
        # of each RBI policy month as conservative blackout
        if not dates:
            year = datetime.date.today().year
            for month in RBI_POLICY_MONTHS_2026:
                try:
                    # First Wednesday of that month
                    d = datetime.date(year, month, 1)
                    while d.weekday() != 2:  # 2 = Wednesday
                        d += datetime.timedelta(days=1)
                    dates.add(d.strftime("%Y-%m-%d"))
                except ValueError:
                    pass
            print(f"[BlackoutCalendar] Using fallback RBI dates: {len(dates)} dates")

        return dates

    def _load_cache(self) -> set:
        """Load cached blackout dates if refreshed within last 7 days."""
        try:
            today = datetime.date.today().isoformat()
            with sqlite3.connect(STATE_DB) as conn:
                row = conn.execute("""
                    SELECT dates_json, updated_at FROM blackout_cache
                    WHERE cache_date = '2026'
                """).fetchone()
            if not row:
                return None
            updated = datetime.date.fromisoformat(row[1][:10])
            age_days = (datetime.date.today() - updated).days
            if age_days > 7:
                return None
            return set(json.loads(row[0]))
        except Exception:
            return None

    def _save_cache(self, dates: set):
        with sqlite3.connect(STATE_DB) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO blackout_cache
                (cache_date, dates_json, updated_at)
                VALUES ('2026', ?, ?)
            """, (json.dumps(list(dates)), datetime.datetime.now().isoformat()))
            conn.commit()

    def get_blackout_dates(self) -> set:
        """
        Returns full set of blackout date strings: {"2026-01-26", ...}
        Uses cache if fresh, fetches fresh if stale.
        """
        cached = self._load_cache()
        if cached:
            return cached

        nse_dates = self._fetch_nse_holidays()
        rbi_dates = self._fetch_rbi_policy_dates()
        all_dates = nse_dates | rbi_dates

        if all_dates:
            self._save_cache(all_dates)
            print(f"[BlackoutCalendar] Fetched {len(nse_dates)} NSE + "
                  f"{len(rbi_dates)} RBI = {len(all_dates)} total blackouts")
        else:
            print("[BlackoutCalendar] WARNING: Could not fetch any dates. "
                  "Proceeding with empty blackout list.")

        return all_dates

    def is_blackout(self, date: datetime.date = None) -> bool:
        """Check if given date (default today) is a blackout day."""
        check = (date or datetime.date.today()).isoformat()
        return check in self.get_blackout_dates()

    def refresh(self, alert_fn=None):
        """Force-refresh cache. Called weekly by scheduler."""
        # Clear cache to force re-fetch
        try:
            with sqlite3.connect(STATE_DB) as conn:
                conn.execute("DELETE FROM blackout_cache")
                conn.commit()
        except Exception:
            pass
        dates = self.get_blackout_dates()
        if alert_fn:
            upcoming = sorted([
                d for d in dates
                if d >= datetime.date.today().isoformat()
            ])[:5]
            lines = "\n".join([f"• `{d}`" for d in upcoming])
            alert_fn(f"📅 *BLACKOUT CALENDAR REFRESHED*\n"
                     f"Total: {len(dates)} dates\n"
                     f"Next 5:\n{lines}")
