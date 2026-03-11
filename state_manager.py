"""
Persists all active trade state to SQLite after every order event.
On engine restart after crash — reloads open positions and resumes monitoring.
No position is ever lost or left unmonitored.
"""

import sqlite3
import datetime
import json
from config import STATE_DB


class StateManager:

    def __init__(self):
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(STATE_DB) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS active_positions (
                    entry_oid       TEXT PRIMARY KEY,
                    symbol          TEXT NOT NULL,
                    strategy        TEXT NOT NULL,
                    product         TEXT NOT NULL,
                    regime          TEXT,
                    entry_price     REAL NOT NULL,
                    stop_price      REAL NOT NULL,
                    partial_target  REAL,
                    target_price    REAL NOT NULL,
                    qty             INTEGER NOT NULL,
                    partial_qty     INTEGER DEFAULT 0,
                    remaining_qty   INTEGER NOT NULL,
                    partial_filled  INTEGER DEFAULT 0,
                    sl_oid          TEXT,
                    partial_oid     TEXT,
                    target_oid      TEXT,
                    entry_time      TEXT NOT NULL,
                    entry_date      TEXT NOT NULL,
                    rvol            REAL DEFAULT 0,
                    deviation_pct   REAL DEFAULT 0,
                    status          TEXT DEFAULT 'OPEN',
                    last_updated    TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()

    # ── Write ─────────────────────────────────────────────────────

    def save(self, entry_oid: str, trade: dict):
        """Persist trade immediately after order placement."""
        now = datetime.datetime.now().isoformat()

        def _str(val):
            if isinstance(val, datetime.datetime):
                return val.isoformat()
            if isinstance(val, datetime.date):
                return val.isoformat()
            return str(val) if val is not None else ""

        with sqlite3.connect(STATE_DB) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO active_positions VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                entry_oid,
                trade.get("symbol", ""),
                trade.get("strategy", ""),
                trade.get("product", ""),
                trade.get("regime", ""),
                trade.get("entry_price", 0),
                trade.get("stop_price", 0),
                trade.get("partial_target") or trade.get("partial_target_1"),
                trade.get("target_price", 0),
                trade.get("qty", 0),
                trade.get("partial_qty", 0),
                trade.get("remaining_qty", trade.get("qty", 0)),
                1 if trade.get("partial_filled") else 0,
                trade.get("sl_oid", ""),
                trade.get("partial_oid", ""),
                trade.get("target_oid", ""),
                _str(trade.get("entry_time", now)),
                _str(trade.get("entry_date", datetime.date.today())),
                trade.get("rvol", 0),
                trade.get("deviation_pct", 0),
                "OPEN",
                now
            ))
            conn.commit()

    def mark_partial_filled(self, entry_oid: str, remaining_qty: int):
        with sqlite3.connect(STATE_DB) as conn:
            conn.execute("""
                UPDATE active_positions
                SET partial_filled=1, remaining_qty=?, last_updated=?
                WHERE entry_oid=?
            """, (remaining_qty, datetime.datetime.now().isoformat(), entry_oid))
            conn.commit()

    def close(self, entry_oid: str):
        with sqlite3.connect(STATE_DB) as conn:
            conn.execute("""
                UPDATE active_positions
                SET status='CLOSED', last_updated=?
                WHERE entry_oid=?
            """, (datetime.datetime.now().isoformat(), entry_oid))
            conn.commit()

    def set_kv(self, key: str, value: str):
        with sqlite3.connect(STATE_DB) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv_store VALUES (?,?)", (key, value)
            )
            conn.commit()

    def get_kv(self, key: str, default: str = "") -> str:
        with sqlite3.connect(STATE_DB) as conn:
            row = conn.execute(
                "SELECT value FROM kv_store WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row else default

    # ── Read (crash recovery) ─────────────────────────────────────

    def load_open_positions(self) -> list:
        """
        Called at startup. Returns all OPEN positions from today or later.
        Reconstructs trade dicts for the execution agent to resume monitoring.
        """
        today = datetime.date.today().isoformat()
        with sqlite3.connect(STATE_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM active_positions
                WHERE status='OPEN' AND entry_date >= ?
                ORDER BY entry_time ASC
            """, (today,)).fetchall()

        trades = []
        for r in rows:
            entry_time_raw = r["entry_time"]
            try:
                entry_time = datetime.datetime.fromisoformat(entry_time_raw)
            except Exception:
                entry_time = datetime.datetime.now()

            entry_date_raw = r["entry_date"]
            try:
                entry_date = datetime.date.fromisoformat(entry_date_raw)
            except Exception:
                entry_date = datetime.date.today()

            trades.append({
                "entry_oid":      r["entry_oid"],
                "symbol":         r["symbol"],
                "strategy":       r["strategy"],
                "product":        r["product"],
                "regime":         r["regime"],
                "entry_price":    r["entry_price"],
                "stop_price":     r["stop_price"],
                "partial_target": r["partial_target"],
                "target_price":   r["target_price"],
                "qty":            r["qty"],
                "partial_qty":    r["partial_qty"],
                "remaining_qty":  r["remaining_qty"],
                "partial_filled": bool(r["partial_filled"]),
                "sl_oid":         r["sl_oid"],
                "partial_oid":    r["partial_oid"],
                "target_oid":     r["target_oid"],
                "entry_time":     entry_time,
                "entry_date":     entry_date,
                "rvol":           r["rvol"],
                "deviation_pct":  r["deviation_pct"],
            })
        return trades
