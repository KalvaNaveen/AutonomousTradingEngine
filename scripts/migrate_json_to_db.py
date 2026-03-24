import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import os
import glob
import json
import sqlite3
import time
from storage.historical_db import HistoricalDB

def migrate():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    master_dir = os.path.join(base_dir, "data", "master")
    
    if not os.path.exists(master_dir):
        print(f"No master JSON directory found at {master_dir}")
        return

    db = HistoricalDB()
    
    # Process daily files
    daily_files = glob.glob(os.path.join(master_dir, "daily_*.json"))
    print(f"Found {len(daily_files)} daily JSON files. Migrating...")
    
    for df in daily_files:
        filename = os.path.basename(df)
        token_str = filename.replace("daily_", "").replace(".json", "")
        token = int(token_str)
        
        with open(df, "r", encoding="utf-8") as f:
            bars = json.load(f)
            if bars:
                db.insert_daily_bars(token, bars)
                
    print(f"[PASS] Successfully inserted {len(daily_files)} daily files.")
    
    # Process minute files
    minute_files = glob.glob(os.path.join(master_dir, "minute_*.json"))
    print(f"Found {len(minute_files)} minute JSON files. Migrating (this may take a few minutes)...")
    
    start_time = time.time()
    
    for i, mf in enumerate(minute_files):
        filename = os.path.basename(mf)
        token_str = filename.replace("minute_", "").replace(".json", "")
        token = int(token_str)
        
        with open(mf, "r", encoding="utf-8") as f:
            bars = json.load(f)
            if bars:
                db.insert_minute_bars(token, bars)
                
        if (i + 1) % 50 == 0:
            print(f"  Processed {i + 1}/{len(minute_files)} minute files...")
            
    print(f"[PASS] Successfully inserted {len(minute_files)} minute files. Took {time.time() - start_time:.2f} seconds.")
    
    # Check DB size
    size_mb = os.path.getsize(db.db_path) / (1024 * 1024)
    print(f"Migration complete! SQLite Database is now {size_mb:.2f} MB at {db.db_path}")

if __name__ == "__main__":
    migrate()
