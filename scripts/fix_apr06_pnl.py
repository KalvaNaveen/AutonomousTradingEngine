"""
Fix inverted gross_pnl for short trades on 2026-04-06.
Also recalculate and fix the daily_summary.
"""
import sqlite3
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config import JOURNAL_DB
DB_PATH = JOURNAL_DB

def main():
    conn = sqlite3.connect(DB_PATH)
    
    # 1. Fetch trades for 2026-04-06
    rows = conn.execute(
        "SELECT id, symbol, strategy, entry_price, full_exit_price, gross_pnl, qty "
        "FROM trades WHERE date = '2026-04-06'"
    ).fetchall()
    
    if not rows:
        print("No trades found for 2026-04-06. Nothing to fix.")
        conn.close()
        return
    
    print("BEFORE fix:")
    for r in rows:
        print(f"  ID={r[0]} | {r[1]:12s} | {r[2]:18s} | Entry={r[3]:.1f} | Exit={r[4]:.1f} | PnL={r[5]:.2f} | Qty={r[6]}")
    
    # 2. Negate gross_pnl for all short strategy trades for today
    updated = conn.execute(
        "UPDATE trades SET gross_pnl = -gross_pnl "
        "WHERE date = '2026-04-06' AND strategy LIKE '%SHORT%'"
    ).rowcount
    
    # 3. Recalculate daily_summary
    # We must fetch all trades for the date again to compute new sum
    trades = conn.execute(
        "SELECT gross_pnl FROM trades WHERE date = '2026-04-06'"
    ).fetchall()
    
    total = len(trades)
    wins = sum(1 for t in trades if t[0] > 0)
    losses = sum(1 for t in trades if t[0] <= 0)
    gross_pnl = sum(t[0] for t in trades)
    win_rate = (wins / total * 100) if total > 0 else 0.0
    
    print(f"\nRecalculated summary: Total={total}, Wins={wins}, Losses={losses}, Win Rate={win_rate:.2f}%, Net PnL={gross_pnl:.2f}")

    # 4. Update daily_summary
    conn.execute(
        "UPDATE daily_summary "
        "SET wins = ?, losses = ?, win_rate = ?, gross_pnl = ? "
        "WHERE date = '2026-04-06'",
        (wins, losses, win_rate, gross_pnl)
    )
    
    conn.commit()
    
    # 5. Verify the fix
    rows = conn.execute(
        "SELECT id, symbol, strategy, entry_price, full_exit_price, gross_pnl, qty "
        "FROM trades WHERE date = '2026-04-06'"
    ).fetchall()
    
    print(f"\nAFTER fix ({updated} short trades updated):")
    for r in rows:
        print(f"  ID={r[0]} | {r[1]:12s} | {r[2]:18s} | Entry={r[3]:.1f} | Exit={r[4]:.1f} | PnL={r[5]:.2f} | Qty={r[6]}")
    
    conn.close()
    print("\nDone. journal.db AND daily_summary corrected for 2026-04-06.")

if __name__ == "__main__":
    main()
