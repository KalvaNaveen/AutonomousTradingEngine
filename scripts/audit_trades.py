"""Quick script to pull trade data for audit comparison."""
import sqlite3
import os
import sys

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base_dir)
db_path = os.path.join(base_dir, "data", "journal.db")

if not os.path.exists(db_path):
    print(f"DB not found at {db_path}")
    sys.exit(1)

conn = sqlite3.connect(db_path)

# First let's see what dates exist
all_dates = conn.execute("SELECT DISTINCT date FROM trades ORDER BY date DESC LIMIT 20").fetchall()
print(f"Available dates: {[r[0] for r in all_dates]}")

dates = ["2026-04-02", "2026-04-06", "2026-04-07"]

output_lines = []

for d in dates:
    output_lines.append(f"\n{'='*90}")
    output_lines.append(f"  DATE: {d}")
    output_lines.append(f"{'='*90}")
    
    summary = conn.execute(
        "SELECT total_trades, wins, losses, win_rate, gross_pnl, regime, stop_reason FROM daily_summary WHERE date=?",
        (d,)
    ).fetchone()
    if summary:
        output_lines.append(f"  Summary: {summary[0]} trades | {summary[1]}W/{summary[2]}L | WR: {summary[3]:.1f}% | PnL: Rs.{summary[4]:.2f} | Regime: {summary[5]}")
    else:
        output_lines.append("  No daily summary found.")
    
    trades = conn.execute(
        """SELECT symbol, strategy, entry_price, full_exit_price, gross_pnl, 
                  exit_reason, qty, hold_minutes, entry_time, timestamp
           FROM trades WHERE date=? ORDER BY timestamp""",
        (d,)
    ).fetchall()
    
    if not trades:
        output_lines.append("  No trades found for this date.")
        continue
    
    total_pnl = 0
    total_charges = 0
    
    for i, t in enumerate(trades, 1):
        sym, strat, ep, xp, pnl, reason, qty, hold, et, xt = t
        xp = xp or 0
        pnl = pnl or 0
        hold = hold or 0
        qty = qty or 0
        total_pnl += pnl
        
        # Compute charges
        from core.charges import compute_trade_charges
        is_short = "SHORT" in str(strat).upper()
        if is_short:
            bv, sv = xp * qty, ep * qty
        else:
            bv, sv = ep * qty, xp * qty
        ch = compute_trade_charges(bv, sv, "MIS")["total"]
        total_charges += ch
        
        output_lines.append(f"  #{i} {sym} | {strat} | Entry:{ep:.1f} Exit:{xp:.1f} Qty:{qty} | PnL:Rs.{pnl:+.0f} | Hold:{hold:.0f}m | {reason} | Charges:Rs.{ch:.1f}")
    
    output_lines.append(f"\n  GROSS PnL:  Rs.{total_pnl:+.2f}")
    output_lines.append(f"  CHARGES:   Rs.{total_charges:.2f}")
    output_lines.append(f"  NET PnL:   Rs.{total_pnl - total_charges:+.2f}")

# Also check rejected trades if any log exists
output_lines.append(f"\n{'='*90}")
output_lines.append("  REJECTED TRADES (from agent_logs)")
output_lines.append(f"{'='*90}")
for d in dates:
    rejects = conn.execute(
        "SELECT detail FROM agent_logs WHERE date=? AND (action LIKE '%REJECT%' OR detail LIKE '%REJECT%' OR detail LIKE '%RR_%' OR detail LIKE '%MR_RR%')",
        (d,)
    ).fetchall()
    if rejects:
        output_lines.append(f"\n  {d}: {len(rejects)} rejections")
        for r in rejects[:10]:
            output_lines.append(f"    {r[0][:100]}")

conn.close()

# Write to file
out_path = os.path.join(base_dir, "data", "audit_output.txt")
with open(out_path, "w") as f:
    f.write("\n".join(output_lines))
print(f"Output written to {out_path}")
print("\n".join(output_lines))
