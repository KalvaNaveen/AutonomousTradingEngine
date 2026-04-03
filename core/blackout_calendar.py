"""
Blackout Calendar (V19 update)

Holiday checks are now done dynamically in main.py using Zerodha API quotes
at 9:15 AM each day. This file is retained as an interface for backward
compatibility with other modules (scanner_agent.py etc.), but scraping has
been permanently disabled to avoid false positives.
"""

import datetime
from config import today_ist, now_ist

class BlackoutCalendar:

    def __init__(self):
        self._holidays = set()

    def get_blackout_dates(self) -> set:
        """Stub for legacy code."""
        return self._holidays

    def is_blackout(self, date: datetime.date = None) -> bool:
        """
        Stub that always returns False. 
        True holidays are caught by live API quote timestamps in main.py.
        """
        # We can optionally add manual dates to config if needed:
        # manual_holidays = {"2026-05-01", "2026-08-15"}
        # check = (date or today_ist()).isoformat()
        # return check in manual_holidays
        return False

    def refresh(self, alert_fn=None, force=False):
        """Called daily by scheduler. Does nothing now that scraping is removed."""
        if alert_fn:
            alert_fn("📅 *BLACKOUT CALENDAR (STUB)*\n"
                     "Scraping is disabled. Holiday checks rely on live Kite timestamps at 9:15 AM.")
