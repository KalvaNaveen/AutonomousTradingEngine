import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import os
import datetime
import time
from kiteconnect import KiteConnect

from config import KITE_API_KEY, NIFTY50_TOKEN, INDIA_VIX_TOKEN, today_ist, now_ist
from agents.data_agent import DataAgent
from storage.historical_db import HistoricalDB

def auto_login():
    try:
        from core.auto_login import AutoLogin
        return AutoLogin().login()
    except Exception as e:
        print(f"AutoLogin failed: {e}")
        return None

def main():
    print("Starting EOD Database Update...")
    db = HistoricalDB()
    kite = KiteConnect(api_key=KITE_API_KEY)
    
    access_token = os.getenv("KITE_ACCESS_TOKEN")
    kite.set_access_token(access_token)
    try:
        kite.profile()
    except Exception:
        print("Token expired or missing. Running AutoLogin...")
        access_token = auto_login()
        if access_token:
            kite.set_access_token(access_token)
            os.environ["KITE_ACCESS_TOKEN"] = access_token
        else:
            print("Failed to authenticate. Exiting.")
            return

    # DataAgent fetches UNIVERSE safely
    agent = DataAgent(kite)
    tokens = list(agent.UNIVERSE.keys()) + [NIFTY50_TOKEN, INDIA_VIX_TOKEN]
    end_date = today_ist()

    # 1. Update Daily bars
    print(f"\nPhase 1: Updating Daily Bars for {len(tokens)} tokens...")
    success_daily = 0
    for token in tokens:
        last_date_str = db.get_last_daily_date(token)
        if last_date_str:
            delta_start = datetime.date.fromisoformat(last_date_str[:10]) + datetime.timedelta(days=1)
        else:
            delta_start = end_date - datetime.timedelta(days=1825) # 5 years

        if delta_start > end_date:
            continue
            
        try:
            bars = kite.historical_data(token, delta_start, end_date, "day")
            if bars:
                db.insert_daily_bars(token, bars)
                success_daily += 1
        except Exception as e:
            print(f"Daily error {token}: {e}")
        time.sleep(0.35)
        
    print(f"Daily Update Complete: {success_daily} tokens fetched.")

    # 2. Update Minute bars
    print(f"\nPhase 2: Updating Minute Bars for {len(agent.UNIVERSE)} tokens...")
    success_minute = 0
    for token in agent.UNIVERSE.keys():
        last_min_str = db.get_last_minute_date(token)
        if last_min_str:
            try:
                last_dt = datetime.date.fromisoformat(last_min_str[:10])
                # We start delta query from exactly the last saved date to catch missing afternoon bars
                delta_start = last_dt
            except:
                delta_start = end_date - datetime.timedelta(days=1825)
        else:
            delta_start = end_date - datetime.timedelta(days=1825) 

        if delta_start > end_date:
            continue
            
        cursor = delta_start
        updated = False
        while cursor <= end_date:
            chunk_end = min(cursor + datetime.timedelta(days=60), end_date)
            try:
                bars = kite.historical_data(token, cursor, chunk_end, "minute")
                if bars:
                    db.insert_minute_bars(token, bars)
                    updated = True
            except Exception:
                pass
            cursor = chunk_end + datetime.timedelta(days=1)
            time.sleep(0.35)
            
        if updated:
            success_minute += 1

    print(f"Minute Update Complete: {success_minute} tokens fetched.")
    
    # 3. Prune extremely old data (retention policy = 10 years)
    try:
        db.prune_old_records(days_to_keep=3650)
    except Exception as e:
        print(f"Error during data pruning: {e}")
        
    print("\nEOD Database Update Fully Concluded.")

if __name__ == "__main__":
    main()
