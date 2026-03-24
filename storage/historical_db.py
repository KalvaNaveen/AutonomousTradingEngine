import sqlite3
import os
import json
import datetime

class HistoricalDB:
    def __init__(self, db_path=None):
        if db_path is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self.db_path = os.path.join(base_dir, "data", "historical.db")
        else:
            self.db_path = db_path
            
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_data (
                    token INTEGER,
                    date TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume INTEGER,
                    PRIMARY KEY (token, date)
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS minute_data (
                    token INTEGER,
                    date_time TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume INTEGER,
                    PRIMARY KEY (token, date_time)
                )
            """)

    def insert_daily_bars(self, token, bars):
        """Batch insert daily bars. Expected format is kite historical_data dictionary."""
        if not bars: return
        data_to_insert = []
        for b in bars:
            # kite date string is usually "2024-01-01T00:00:00+0530" or datetime object
            date_val = str(b.get('date', ''))
            # Format to just YYYY-MM-DD for daily
            if "T" in date_val:
                date_val = date_val.split("T")[0]
            elif " " in date_val:
                date_val = date_val.split(" ")[0]
            
            data_to_insert.append((
                token, date_val, b['open'], b['high'], b['low'], b['close'], b['volume']
            ))
            
        with self.conn:
            self.conn.executemany("""
                INSERT OR REPLACE INTO daily_data (token, date, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, data_to_insert)

    def insert_minute_bars(self, token, bars):
        """Batch insert minute bars. Expected format is kite historical_data dictionary."""
        if not bars: return
        data_to_insert = []
        for b in bars:
            # For minute, keep the full string or standardize
            # Kite gives "2024-01-01T09:15:00+0530"
            date_val = str(b.get('date', ''))
            # Parse to string 'YYYY-MM-DD HH:MM:SS' for cleaner DB reading if needed, or stick to raw string
            # Let's keep it robust and store the raw string from Kite.
            data_to_insert.append((
                token, date_val, b['open'], b['high'], b['low'], b['close'], b['volume']
            ))
            
        with self.conn:
            self.conn.executemany("""
                INSERT OR REPLACE INTO minute_data (token, date_time, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, data_to_insert)

    def get_daily_bars(self, token, start_date_str=None, end_date_str=None):
        """Fetch daily bars for token. Format dates as YYYY-MM-DD."""
        query = "SELECT date, open, high, low, close, volume FROM daily_data WHERE token = ?"
        params = [token]
        
        if start_date_str:
            query += " AND date >= ?"
            params.append(start_date_str)
        if end_date_str:
            query += " AND date <= ?"
            params.append(end_date_str)
            
        query += " ORDER BY date ASC"
        
        cursor = self.conn.execute(query, params)
        rows = cursor.fetchall()
        
        return [{
            "date": row["date"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"]
        } for row in rows]

    def get_minute_bars(self, token, start_datetime_str=None, end_datetime_str=None):
        """Fetch minute bars. Remember Kite dates are '2024-01-01T09:15:00+0530'."""
        query = "SELECT date_time as date, open, high, low, close, volume FROM minute_data WHERE token = ?"
        params = [token]
        
        if start_datetime_str:
            query += " AND date_time >= ?"
            params.append(start_datetime_str)
        if end_datetime_str:
            query += " AND date_time <= ?"
            params.append(end_datetime_str)
            
        query += " ORDER BY date_time ASC"
        
        cursor = self.conn.execute(query, params)
        rows = cursor.fetchall()
        
        return [{
            "date": row["date"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"]
        } for row in rows]

    def get_last_daily_date(self, token):
        query = "SELECT MAX(date) as last_date FROM daily_data WHERE token = ?"
        cursor = self.conn.execute(query, (token,))
        row = cursor.fetchone()
        return row["last_date"] if row and row["last_date"] else None

    def get_last_minute_date(self, token):
        query = "SELECT MAX(date_time) as last_date FROM minute_data WHERE token = ?"
        cursor = self.conn.execute(query, (token,))
        row = cursor.fetchone()
        return row["last_date"] if row and row["last_date"] else None
        
    def prune_old_records(self, days_to_keep=3650):
        """Deletes records older than days_to_keep (10 years) from both tables."""
        import datetime
        cutoff_date = datetime.date.today() - datetime.timedelta(days=days_to_keep)
        cutoff_str = str(cutoff_date)
        
        with self.conn:
            c1 = self.conn.execute("DELETE FROM daily_data WHERE date < ?", (cutoff_str,))
            c2 = self.conn.execute("DELETE FROM minute_data WHERE date_time < ?", (cutoff_str,))
            
        print(f"[HistoricalDB] Pruned records older than {cutoff_str} (10 yrs max). Deleted: {c1.rowcount} daily, {c2.rowcount} minute.")

    def close(self):
        self.conn.close()
