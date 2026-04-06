import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TOTAL_CAPITAL
from core.journal import Journal
from agents.report_agent import build_daily_report, _send_telegram_message

def main():
    journal = Journal()
    date_str = "2026-04-06"
    
    # 1. Fetch Summary Stats
    stats = journal.get_daily_summary_for_date(date_str)
    if not stats:
        print(f"No daily summary found for {date_str}. Creating synthetic summary based on trades...")
        stats = {
            "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "gross_pnl": 0.0, "regime": "NORMAL", "stop_reason": "",
            "engine_stopped": False, "total": 0
        }
    
    # 2. Fetch all trades for the date
    trades_today = journal.get_all_trades_for_date(date_str)
    
    stats["total"] = stats.get("total_trades", len(trades_today))
    if not stats["total"] and trades_today:
        stats["total"] = len(trades_today)
        stats["wins"] = sum(1 for t in trades_today if t.get("gross_pnl", 0) > 0)
        stats["losses"] = sum(1 for t in trades_today if t.get("gross_pnl", 0) <= 0)
        stats["gross_pnl"] = sum(t.get("gross_pnl", 0) for t in trades_today)
        stats["win_rate"] = stats["wins"] / stats["total"] * 100

    print("Generating Daily Report via report_agent.py...")
    
    # 3. Build & Send
    caption = build_daily_report(
        stats=stats,
        regime=stats.get("regime", "NORMAL"),
        trades_today=trades_today,
        capital=TOTAL_CAPITAL,
        total_scans=840,
    )
    
    print(f"Caption generated:\n{caption}")
    _send_telegram_message(caption)
    print("Report officially sent to Telegram.")

if __name__ == "__main__":
    main()
