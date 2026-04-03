"""
Fix inverted gross_pnl for S6_TREND_SHORT trades on 2026-04-01.
The P&L was calculated with the LONG formula (exit - entry) * qty,
but for shorts it should be (entry - exit) * qty.
Simply negate all S6_TREND_SHORT P&L values for that date.
"""
import sqlite3
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config import JOURNAL_DB
DB_PATH = JOURNAL_DB

def main():
    conn = sqlite3.connect(DB_PATH)
    
    # Show current state
    rows = conn.execute(
        "SELECT id, symbol, strategy, entry_price, full_exit_price, gross_pnl, qty "
        "FROM trades WHERE date = '2026-04-01'"
    ).fetchall()
    
    if not rows:
        print("No trades found for 2026-04-01. Nothing to fix.")
        conn.close()
        return
    
    print("BEFORE fix:")
    for r in rows:
        print(f"  ID={r[0]} | {r[1]:12s} | {r[2]:18s} | Entry={r[3]:.1f} | Exit={r[4]:.1f} | PnL={r[5]:.2f} | Qty={r[6]}")
    
    # Fix: negate gross_pnl for all short strategy trades
    # S6_TREND_SHORT is always short, so all its P&L values are inverted
    updated = conn.execute(
        "UPDATE trades SET gross_pnl = -gross_pnl "
        "WHERE date = '2026-04-01' AND strategy LIKE '%SHORT%'"
    ).rowcount
    
    # Also fix daily_summary
    conn.execute(
        "UPDATE daily_summary SET gross_pnl = -gross_pnl "
        "WHERE date = '2026-04-01'"
    )
    
    conn.commit()
    
    # Show fixed state
    rows = conn.execute(
        "SELECT id, symbol, strategy, entry_price, full_exit_price, gross_pnl, qty "
        "FROM trades WHERE date = '2026-04-01'"
    ).fetchall()
    
    print(f"\nAFTER fix ({updated} trades updated):")
    for r in rows:
        print(f"  ID={r[0]} | {r[1]:12s} | {r[2]:18s} | Entry={r[3]:.1f} | Exit={r[4]:.1f} | PnL={r[5]:.2f} | Qty={r[6]}")
    
    conn.close()
    print("\nDone. journal.db corrected for 2026-04-01.")

if __name__ == "__main__":
    main()
