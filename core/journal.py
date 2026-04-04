import sqlite3
import datetime
from config import JOURNAL_DB, now_ist, today_ist


class Journal:

    def __init__(self):
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(JOURNAL_DB) as conn:
            # WAL mode for the same reason as state_manager — concurrent writes
            # from the main thread and FillMonitor background threads.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=3000")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp         TEXT, date TEXT,
                    entry_time        TEXT,
                    symbol            TEXT, strategy TEXT, regime TEXT,
                    rvol              REAL, deviation_pct REAL,
                    entry_price       REAL, partial_exit_price REAL,
                    partial_exit_qty  INTEGER, full_exit_price REAL,
                    qty               INTEGER, gross_pnl REAL,
                    stop_hit          INTEGER DEFAULT 0,
                    time_stop_hit     INTEGER DEFAULT 0,
                    exit_reason       TEXT, hold_minutes REAL,
                    daily_pnl_after   REAL
                )
            """)
            # Migration: Add entry_time column to existing trades table
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN entry_time TEXT")
            except Exception:
                pass # Already exists

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_date
                ON trades(date)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_strategy
                ON trades(strategy, date)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_summary (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    date            TEXT UNIQUE, regime TEXT,
                    total_trades    INTEGER, wins INTEGER, losses INTEGER,
                    win_rate        REAL, gross_pnl REAL,
                    max_loss_streak INTEGER,
                    engine_stopped  INTEGER DEFAULT 0, stop_reason TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_logs (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    date    TEXT, time TEXT,
                    agent   TEXT, action TEXT, detail TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent_logs_date
                ON agent_logs(date)
            """)
            conn.commit()

    def log_agent_activity(self, agent: str, action: str, detail: str, timestamp_str: str):
        date_str = timestamp_str[:10]  # if timestamp is ISO like 2026-04-01T... or we can use today's date
        # Wait, the time string passed from api_server is just "HH:MM:SS".
        from config import today_ist
        date_str = today_ist().strftime("%Y-%m-%d")
        with sqlite3.connect(JOURNAL_DB) as conn:
            conn.execute("""
                INSERT INTO agent_logs (date, time, agent, action, detail)
                VALUES (?, ?, ?, ?, ?)
            """, (date_str, timestamp_str, agent, action, detail))
            conn.commit()

    def log_trade(self, trade: dict):
        now  = now_ist()
        et   = trade.get("entry_time") or now
        xt   = trade.get("exit_time") or now
        hold = (xt - et).total_seconds() / 60 if isinstance(et, datetime.datetime) else 0
        with sqlite3.connect(JOURNAL_DB) as conn:
            conn.execute("""
                INSERT INTO trades
                (timestamp, date, entry_time, symbol, strategy, regime, rvol, deviation_pct,
                 entry_price, partial_exit_price, partial_exit_qty,
                 full_exit_price, qty, gross_pnl, stop_hit, time_stop_hit,
                 exit_reason, hold_minutes, daily_pnl_after)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                now.isoformat(), now.strftime("%Y-%m-%d"),
                et.isoformat() if isinstance(et, datetime.datetime) else str(et),

                trade.get("symbol", ""), trade.get("strategy", ""),
                trade.get("regime", ""), trade.get("rvol", 0),
                trade.get("deviation_pct", 0), trade.get("entry_price", 0),
                trade.get("partial_exit_price"),
                trade.get("partial_exit_qty"),
                trade.get("full_exit_price", 0), trade.get("qty", 0),
                trade.get("pnl", 0),
                1 if trade.get("exit_reason") == "STOP_HIT" else 0,
                1 if trade.get("exit_reason") == "TIME_STOP" else 0,
                trade.get("exit_reason", ""), hold,
                trade.get("daily_pnl_after", 0)
            ))
            conn.commit()

    def log_exit(self, oid: str, trade: dict, exit_price: float, reason: str):
        """
        Called by ExecutionAgent._close_minervini() for S3/S4 position exits.
        Wraps log_trade() with the exit price and computed PnL.
        """
        qty = trade.get("remaining_qty", trade.get("qty", 0))
        entry_price = trade.get("entry_price", 0)
        # Direction-aware P&L: shorts profit when price falls
        if trade.get("is_short", False):
            pnl = (entry_price - exit_price) * qty
        else:
            pnl = (exit_price - entry_price) * qty
        self.log_trade({
            **trade,
            "full_exit_price": exit_price,
            "pnl": pnl,
            "exit_reason": reason,
            "exit_time": now_ist(),
            "daily_pnl_after": 0,
        })

    def log_daily_summary(self, stats: dict, regime: str,
                           stopped: bool, reason: str):
        with sqlite3.connect(JOURNAL_DB) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO daily_summary
                (date, regime, total_trades, wins, losses, win_rate,
                 gross_pnl, max_loss_streak, engine_stopped, stop_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                datetime.date.today().isoformat(), regime,
                stats.get("total", 0), stats.get("wins", 0),
                stats.get("losses", 0), stats.get("win_rate", 0),
                stats.get("gross_pnl", 0), stats.get("loss_streak", 0),
                1 if stopped else 0, reason
            ))
            conn.commit()

    def win_rate_by_regime(self) -> list:
        with sqlite3.connect(JOURNAL_DB) as conn:
            return conn.execute("""
                SELECT regime, COUNT(*) total,
                       ROUND(100.0*SUM(CASE WHEN gross_pnl>0 THEN 1 ELSE 0 END)
                             /COUNT(*),1) win_rate,
                       ROUND(AVG(gross_pnl),2) avg_pnl
                FROM trades GROUP BY regime ORDER BY win_rate DESC
            """).fetchall()

    def get_period_summary(self, from_date: str, to_date: str) -> dict:
        """
        Returns aggregated stats for a date range (weekly / monthly summaries).
        from_date / to_date: ISO strings "YYYY-MM-DD" (inclusive).
        """
        with sqlite3.connect(JOURNAL_DB) as conn:
            rows = conn.execute("""
                SELECT gross_pnl, strategy, regime, symbol
                FROM trades
                WHERE date >= ? AND date <= ?
            """, (from_date, to_date)).fetchall()

        if not rows:
            return {
                "total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "gross_pnl": 0.0, "best_regime": "—", "worst_regime": "—",
                "top5_symbols": [],
            }

        total      = len(rows)
        wins       = sum(1 for r in rows if r[0] > 0)
        gross_pnl  = sum(r[0] for r in rows)
        win_rate   = wins / total * 100 if total > 0 else 0.0

        # Regime PnL breakdown
        regime_pnl: dict = {}
        for pnl, _strat, regime, _sym in rows:
            regime_pnl[regime] = regime_pnl.get(regime, 0.0) + pnl
        best_regime  = max(regime_pnl, key=regime_pnl.get) if regime_pnl else "—"
        worst_regime = min(regime_pnl, key=regime_pnl.get) if regime_pnl else "—"

        # Top 5 symbols by total PnL
        sym_pnl: dict = {}
        for pnl, _strat, _regime, sym in rows:
            sym_pnl[sym] = sym_pnl.get(sym, 0.0) + pnl
        top5 = sorted(sym_pnl.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "total":        total,
            "wins":         wins,
            "losses":       total - wins,
            "win_rate":     round(win_rate, 1),
            "gross_pnl":    round(gross_pnl, 2),
            "best_regime":  best_regime,
            "worst_regime": worst_regime,
            "top5_symbols": top5,
        }

    def get_today_top_actions(self, date_str: str = None, n: int = 3) -> list:
        """
        Returns the top-n trades for a given date, sorted by abs(gross_pnl).
        Used in the daily EOD summary Telegram message.
        Returns list of dicts: [{symbol, strategy, gross_pnl, exit_reason}, ...]
        """
        d = date_str or today_ist().isoformat()
        with sqlite3.connect(JOURNAL_DB) as conn:
            rows = conn.execute("""
                SELECT symbol, strategy, gross_pnl, exit_reason
                FROM trades
                WHERE date = ?
                ORDER BY ABS(gross_pnl) DESC
                LIMIT ?
            """, (d, n)).fetchall()
        return [
            {"symbol": r[0], "strategy": r[1],
             "gross_pnl": r[2], "exit_reason": r[3]}
            for r in rows
        ]

    def get_all_trades_for_date(self, date_str: str = None) -> list:
        """
        Returns every trade for a given date, ordered by timestamp.
        Used by report_agent to build the per-trade log in the daily report.
        Returns list of dicts: [{symbol, entry_price, full_exit_price,
                                  gross_pnl, exit_reason}, ...]
        """
        d = date_str or today_ist().isoformat()
        with sqlite3.connect(JOURNAL_DB) as conn:
            rows = conn.execute("""
                SELECT symbol, strategy, entry_price, full_exit_price,
                       gross_pnl, exit_reason, qty, entry_time, timestamp
                FROM trades
                WHERE date = ?
                ORDER BY timestamp ASC
            """, (d,)).fetchall()
        return [
            {"symbol": r[0], "strategy": r[1], "entry_price": r[2],
             "full_exit_price": r[3], "gross_pnl": r[4], "exit_reason": r[5],
             "qty": r[6], "entry_time": r[7], "exit_time": r[8]}
            for r in rows
        ]

    def get_period_trades(self, from_date: str, to_date: str) -> list:
        """
        Returns every trade in a date range with full detail for PDF reports.
        Columns: timestamp, symbol, strategy, entry_price, full_exit_price,
                 gross_pnl, exit_reason
        """
        with sqlite3.connect(JOURNAL_DB) as conn:
            rows = conn.execute("""
                SELECT timestamp, symbol, strategy, entry_price,
                       full_exit_price, gross_pnl, exit_reason, qty
                FROM trades
                WHERE date >= ? AND date <= ?
                ORDER BY timestamp ASC
            """, (from_date, to_date)).fetchall()
        return [
            {"timestamp": r[0], "symbol": r[1], "strategy": r[2],
             "entry_price": r[3], "full_exit_price": r[4],
             "gross_pnl": r[5], "exit_reason": r[6], "qty": r[7]}
            for r in rows
        ]
        
    def get_available_dates(self) -> list:
        """Returns a list of all distinct dates in descending order."""
        with sqlite3.connect(JOURNAL_DB) as conn:
            rows = conn.execute("SELECT DISTINCT date FROM trades ORDER BY date DESC").fetchall()
        
        # If no trades yet, check daily summary
        if not rows:
            with sqlite3.connect(JOURNAL_DB) as conn:
                rows = conn.execute("SELECT DISTINCT date FROM daily_summary ORDER BY date DESC").fetchall()
        
        return [r[0] for r in rows]

    def get_logs_for_date(self, date_str: str) -> list:
        """Returns all agent logs for a given date in chronological order."""
        with sqlite3.connect(JOURNAL_DB) as conn:
            rows = conn.execute("""
                SELECT time, agent, action, detail
                FROM agent_logs
                WHERE date = ?
                ORDER BY id ASC
            """, (date_str,)).fetchall()
        return [
            {"time": r[0], "agent": r[1], "action": r[2], "detail": r[3]}
            for r in rows
        ]

    def get_daily_summary_for_date(self, date_str: str) -> dict:
        """Returns the single row summary for a given date."""
        with sqlite3.connect(JOURNAL_DB) as conn:
            row = conn.execute("""
                SELECT total_trades, wins, losses, win_rate, gross_pnl, stop_reason, engine_stopped, regime
                FROM daily_summary WHERE date = ?
            """, (date_str,)).fetchone()
        if not row:
            return None
        return {
            "total_trades": row[0], "wins": row[1], "losses": row[2],
            "win_rate": row[3], "gross_pnl": row[4], "stop_reason": row[5],
            "engine_stopped": row[6] == 1, "regime": row[7]
        }

