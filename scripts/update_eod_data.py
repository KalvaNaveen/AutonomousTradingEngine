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

# ── S4: Futures instruments to sync (STRICTLY FUT only, NO options) ──────────
# Names as they appear in Kite NFO instrument list.
# All-expiry contracts are downloaded so S4 backtest spans multiple expiry cycles.
FUTURES_NAMES = ["NIFTY", "BANKNIFTY"]

def discover_futures_tokens(kite: KiteConnect) -> dict:
    """
    Discovers ALL non-expired Nifty / BankNifty futures contracts from NFO.
    Returns: {instrument_token: {"symbol": str, "name": str, "expiry": date}}
    Only instrument_type == "FUT" — options (CE/PE) are filtered out.
    """
    import pandas as pd
    result = {}
    try:
        nfo = kite.instruments(exchange="NFO")
        df  = pd.DataFrame(nfo)
        # Filter: futures only for target index names
        mask = (
            (df["instrument_type"] == "FUT") &
            (df["name"].isin(FUTURES_NAMES))
        )
        fut_df = df[mask].copy()
        fut_df["expiry"] = pd.to_datetime(fut_df["expiry"]).dt.date
        today = today_ist()
        # Keep all contracts (including near-expired) so we get historical range
        for _, row in fut_df.iterrows():
            tok = int(row["instrument_token"])
            result[tok] = {
                "symbol": str(row["tradingsymbol"]),
                "name":   str(row["name"]),
                "expiry": row["expiry"],
            }
        print(f"[EOD] Futures discovered: {len(result)} contracts "
              f"({', '.join(FUTURES_NAMES)}) — FUT only, no options.")
    except Exception as e:
        print(f"[EOD] Futures discovery failed: {e}")
    return result


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

    # ── Phase 3: Futures Data Sync (S4 Arbitrage Backtest Support) ──────────
    # Discovers ALL Nifty/BankNifty FUT contracts and syncs daily + minute data.
    # STRICTLY instrument_type == FUT only — options are never fetched.
    print(f"\nPhase 3: Futures Data Sync (S4 Arbitrage — NIFTY/BANKNIFTY FUT only)...")
    futures_map = discover_futures_tokens(kite)
    if not futures_map:
        print("  Phase 3 SKIPPED: No futures tokens found (NFO may be unavailable).")
    else:
        success_fut_daily  = 0
        success_fut_minute = 0
        for fut_token, finfo in futures_map.items():
            sym    = finfo["symbol"]
            expiry = finfo["expiry"]

            # ── 3a. Daily bars for this futures contract ──
            last_daily = db.get_last_daily_date(fut_token)
            if last_daily:
                d_start = datetime.date.fromisoformat(last_daily[:10]) + datetime.timedelta(days=1)
            else:
                # Default: go back 5 years (full history)
                d_start = end_date - datetime.timedelta(days=1825)

            if d_start <= end_date:
                try:
                    bars = kite.historical_data(fut_token, d_start, end_date, "day")
                    if bars:
                        db.insert_daily_bars(fut_token, bars)
                        success_fut_daily += 1
                        print(f"  Daily: {sym} ({d_start} -> {end_date}): {len(bars)} bars")
                except Exception as e:
                    ex = str(e)[:80].replace("\n", " ")
                    print(f"  Daily error {sym}: {ex}")
                time.sleep(0.35)

            # ── 3b. Minute bars for this futures contract ──
            # Kite allows only 60 days of minute data per request.
            # Chunk in 59-day windows from last saved date.
            last_min = db.get_last_minute_date(fut_token)
            if last_min:
                try:
                    m_start = datetime.date.fromisoformat(last_min[:10])
                except Exception:
                    m_start = end_date - datetime.timedelta(days=1825)
            else:
                m_start = end_date - datetime.timedelta(days=1825)

            # Do not go before contract listing date (approx 3 months before expiry)
            earliest = expiry - datetime.timedelta(days=90)
            m_start  = max(m_start, earliest)

            if m_start <= end_date:
                cursor  = m_start
                updated = False
                while cursor <= end_date:
                    chunk_end = min(cursor + datetime.timedelta(days=59), end_date)
                    try:
                        bars = kite.historical_data(fut_token, cursor, chunk_end, "minute")
                        if bars:
                            db.insert_minute_bars(fut_token, bars)
                            updated = True
                    except Exception as e:
                        ex = str(e)[:60].replace("\n", " ")
                        print(f"  Minute chunk error {sym} {cursor}: {ex}")
                    cursor = chunk_end + datetime.timedelta(days=1)
                    time.sleep(0.35)
                if updated:
                    success_fut_minute += 1
                    print(f"  Minute: {sym} synced.")

        print(f"Futures Sync Complete: {success_fut_daily} daily | "
              f"{success_fut_minute} minute contracts fetched.")

    # 4. Prune extremely old data (retention policy = 10 years)
    try:
        db.prune_old_records(days_to_keep=3650)
    except Exception as e:
        print(f"Error during data pruning: {e}")

    print("\nEOD Database Update Fully Concluded.")

if __name__ == "__main__":
    main()
