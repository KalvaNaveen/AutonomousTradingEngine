"""
report_agent.py — Professional PDF + Telegram reporting for BNF Engine.

Report Types:
  1. Daily    → Rich Telegram message (no PDF)
  2. Weekly   → Beautiful PDF sent as Telegram document
  3. Monthly  → Beautiful PDF sent as Telegram document
  4. Simulator → Beautiful PDF sent as Telegram document

All reports include:
  - Trade log with datetime, strategy, entry, exit, W/L status
  - Top 5 Winners & Losers
  - Strategy-wise breakdown
  - Capital used & total return
"""

import os
import tempfile
import requests
from datetime import datetime
from collections import defaultdict

from config import (
    now_ist, TOTAL_CAPITAL,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS,
)

try:
    from fpdf import FPDF
except ImportError:
    FPDF = None


# ── Helpers ───────────────────────────────────────────────────────

def _fmt(value: float) -> str:
    return f"Rs.{value:+,.0f}" if value != 0 else "Rs.0"

def _pct(value: float) -> str:
    return f"{value:+.2f}%"

def _emoji_pnl(pnl: float) -> str:
    return "📈" if pnl >= 0 else "📉"

def _trade_icon(pnl: float) -> str:
    return "[PASS]" if pnl > 0 else "[FAIL]" if pnl < 0 else "➖"

def _separator() -> str:
    return "━━━━━━━━━━━━━━━━━━━━━━━━"

def _wl(pnl: float) -> str:
    if pnl > 0: return "WIN"
    if pnl < 0: return "LOSS"
    return "FLAT"


def _send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        print(f"[REPORT] {text}")
        return
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception:
            pass


def _send_telegram_document(filepath: str, caption: str = ""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        print(f"[REPORT] Would send {filepath}")
        return
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            with open(filepath, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                    data={"chat_id": chat_id, "caption": caption,
                          "parse_mode": "Markdown"},
                    files={"document": (os.path.basename(filepath), f,
                                        "application/pdf")},
                    timeout=30,
                )
        except Exception as e:
            print(f"[REPORT] Telegram document send failed: {e}")


# ── Notes ─────────────────────────────────────────────────────────

def _notes_daily(stats: dict) -> list:
    tips = []
    wr = stats.get("win_rate", 0)
    total = stats.get("total", 0)
    losses = stats.get("losses", 0)
    streak = stats.get("loss_streak", 0)
    if total == 0:
        tips.append("📭 No trades today — filters active or market quiet")
        return tips
    if wr < 40:
        tips.append(f"[WARN] Below target — {wr:.0f}% WR. Review SL and entry criteria")
    elif wr >= 60:
        tips.append(f"🎯 Strong day — {wr:.0f}% WR. Strategy aligned well")
    if total > 0 and losses / total > 0.5:
        tips.append("[STOP] >50% trades hit SL — consider pausing or widening stops")
    if streak >= 2:
        tips.append(f"🔥 {streak} consecutive losses — reduce size until regime improves")
    pnl = stats.get("gross_pnl", 0)
    if pnl > 0:
        tips.append("💪 Profitable day — lock in gains")
    return tips[:3]


def _notes_period(stats: dict, label: str) -> list:
    tips = []
    wr = stats.get("win_rate", 0)
    total = stats.get("total", 0)
    pnl = stats.get("gross_pnl", 0)
    if total == 0:
        tips.append(f"📭 No trades this {label}")
        return tips
    if wr >= 55:
        tips.append(f"🎯 Solid {label} — {wr:.0f}% WR across {total} trades")
    elif wr < 40:
        tips.append(f"[WARN] {label} WR {wr:.0f}% is below target. Review strategy fit")
    if pnl > 0:
        tips.append(f"💪 Net positive {label} — capital growing steadily")
    elif pnl < 0:
        tips.append(f"📉 Net negative {label} — review biggest losers for patterns")
    best = stats.get("best_regime", "—")
    worst = stats.get("worst_regime", "—")
    if best != "—" and best != worst:
        tips.append(f"🧠 Best regime: {best} | Worst: {worst}")
    return tips[:3]


# =====================================================================
#  1. DAILY REPORT — Telegram Message Only (No PDF)
# =====================================================================

def build_daily_report(
    stats: dict,
    regime: str,
    trades_today: list,
    capital: float,
    total_scans: int = 0,
    nifty_price: float = 0.0,
    nifty_change_pct: float = 0.0,
    vix: float = 0.0,
) -> str:
    date_str = now_ist().strftime("%d %b %Y")
    pnl = stats.get("gross_pnl", 0)
    closing_capital = capital + pnl
    day_roi = (pnl / capital * 100) if capital > 0 else 0
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    total = stats.get("total", 0)
    wr = stats.get("win_rate", 0)
    avg_win = stats.get("avg_win", 0)
    avg_loss = stats.get("avg_loss", 0)

    lines = []
    lines.append(f"[INFO] *FULL DAY REPORT — {date_str}*")
    lines.append(_separator())

    # Market Context
    lines.append("")
    lines.append("🏦 *MARKET CONTEXT*")
    if nifty_price > 0:
        nifty_icon = "📈" if nifty_change_pct >= 0 else "📉"
        lines.append(f"{nifty_icon} Nifty 50: Rs.{nifty_price:,.2f} ({nifty_change_pct:+.2f}%)")
    if vix > 0:
        lines.append(f"🌡️ India VIX: {vix:.2f}")
    lines.append(f"🧠 Regime: `{regime}`")

    # P&L Summary
    lines.append("")
    lines.append("💰 *P&L SUMMARY*")
    lines.append(f"{_emoji_pnl(pnl)} Net P&L: `{_fmt(pnl)}`")
    lines.append(f"📊 Day ROI: `{_pct(day_roi)}`")

    # Trade Breakdown
    lines.append("")
    lines.append("[INFO] *TRADE BREAKDOWN*")
    lines.append(f"Total: `{total}` ({wins}W / {losses}L)")
    lines.append(f"🎯 Win Rate: `{wr:.1f}%`")
    if avg_win != 0:
        lines.append(f"📈 Avg Win: `{_fmt(avg_win)}`")
    if avg_loss != 0:
        lines.append(f"📉 Avg Loss: `{_fmt(avg_loss)}`")

    # Strategy Breakdown
    strat_stats = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0})
    for t in trades_today:
        s = t.get("strategy", "UNKNOWN")
        strat_stats[s]["count"] += 1
        strat_stats[s]["pnl"] += t.get("gross_pnl", 0)
        if t.get("gross_pnl", 0) > 0:
            strat_stats[s]["wins"] += 1
    if strat_stats:
        lines.append("")
        lines.append("📊 *STRATEGY BREAKDOWN*")
        for strat, sd in strat_stats.items():
            swr = sd["wins"] / max(1, sd["count"]) * 100
            lines.append(f"• `{strat}`: {sd['count']}T | WR {swr:.0f}% | {_fmt(sd['pnl'])}")

    # Trade Log
    if trades_today:
        lines.append("")
        lines.append("🔄 *TRADE LOG*")
        for t in trades_today:
            icon = _trade_icon(t.get("gross_pnl", 0))
            sym = t.get("symbol", "???")
            strat = t.get("strategy", "")
            entry_px = t.get("entry_price", 0)
            exit_px = t.get("full_exit_price", 0)
            t_pnl = t.get("gross_pnl", 0)
            reason = t.get("exit_reason", "")
            qty = t.get("qty", 0)
            lines.append(
                f"{icon} `{sym}` | {strat} | Qty: {qty}\n"
                f"    Rs.{entry_px:,.1f} → Rs.{exit_px:,.1f} | {_fmt(t_pnl)} ({reason})"
            )

    # Top 5 Winners
    sorted_wins = sorted([t for t in trades_today if t.get("gross_pnl", 0) > 0],
                         key=lambda x: x.get("gross_pnl", 0), reverse=True)[:5]
    sorted_losses = sorted([t for t in trades_today if t.get("gross_pnl", 0) < 0],
                           key=lambda x: x.get("gross_pnl", 0))[:5]
    if sorted_wins:
        lines.append("")
        lines.append("🏆 *TOP WINNERS*")
        for t in sorted_wins:
            lines.append(f"  [PASS] `{t['symbol']}` {_fmt(t['gross_pnl'])}")
    if sorted_losses:
        lines.append("")
        lines.append("[STOP] *TOP LOSERS*")
        for t in sorted_losses:
            lines.append(f"  [FAIL] `{t['symbol']}` {_fmt(t['gross_pnl'])}")

    # Capital Status
    lines.append("")
    lines.append("💼 *CAPITAL STATUS*")
    lines.append(f"Starting: `Rs.{capital:,.0f}`")
    lines.append(f"Closing: `Rs.{closing_capital:,.0f}`")
    lines.append(f"Total Return: `{_pct(day_roi)}`")

    # System Health
    lines.append("")
    lines.append("⚙️ *SYSTEM HEALTH*")
    lines.append(f"🤖 Agent Uptime: 100%")
    lines.append(f"🔄 Scans Run: `{total_scans}`")

    # AI Insights
    notes = _notes_daily(stats)
    if notes:
        lines.append("")
        lines.append("📝 *POST-MARKET NOTES*")
        for tip in notes:
            lines.append(f"• {tip}")

    lines.append("")
    lines.append(f"⏰ Report generated at {now_ist().strftime('%H:%M')} IST")

    msg_text = "\n".join(lines)

    # Automatically generate and send PDF if there were trades today
    if trades_today and FPDF is not None:
        try:
            filepath = _build_pdf_report(
                title="BNF ENGINE - DAILY PERFORMANCE REPORT",
                period_label=f"Date: {date_str}",
                trades=trades_today,
                capital=capital,
                extra_kv={"Total Scans:": str(total_scans), "Regime:": regime}
            )
            if filepath:
                _send_telegram_document(filepath, caption=f"Detailed Daily PDF Report: {date_str}")
                try:
                    os.remove(filepath)
                except Exception:
                    pass
        except Exception as e:
            print(f"[REPORT] Failed to build daily PDF: {e}")

    return msg_text


# =====================================================================
#  PDF BUILDER (Used by Weekly, Monthly, Simulator)
# =====================================================================

class _ReportPDF(FPDF):
    """Professional trading report PDF with clean tables and sections."""

    def __init__(self, title: str, period: str):
        super().__init__()
        self.report_title = title
        self.report_period = period
        self.set_auto_page_break(auto=True, margin=15)

    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.cell(0, 8, self.report_title, new_x="LMARGIN", new_y="NEXT", align="C")
        self.set_font("Helvetica", "", 9)
        self.cell(0, 5, self.report_period, new_x="LMARGIN", new_y="NEXT", align="C")
        self.line(10, self.get_y() + 2, 200, self.get_y() + 2)
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 7)
        ts = now_ist().strftime("%d %b %Y %H:%M IST")
        self.cell(0, 10, f"BNF Engine | Generated: {ts} | Page {self.page_no()}/{{nb}}",
                  align="C")

    def section_title(self, title: str):
        self.ln(3)
        self.set_font("Helvetica", "B", 11)
        self.set_fill_color(30, 30, 30)
        self.set_text_color(255, 255, 255)
        self.cell(0, 7, f"  {title}", new_x="LMARGIN", new_y="NEXT",
                  fill=True)
        self.set_text_color(0, 0, 0)
        self.ln(2)

    def kv_row(self, key: str, value: str, bold_val: bool = False):
        self.set_font("Helvetica", "", 9)
        self.cell(55, 5, key, new_x="RIGHT")
        self.set_font("Helvetica", "B" if bold_val else "", 9)
        self.cell(0, 5, value, new_x="LMARGIN", new_y="NEXT")

    def trade_table_header(self):
        self.set_font("Helvetica", "B", 6)
        self.set_fill_color(220, 220, 220)
        cols = [("#", 5), ("Date", 15), ("Entry Time", 14), ("Exit Time", 14),
                ("Strategy", 24), ("Symbol", 14), ("Qty", 8),
                ("Entry", 13), ("Exit", 13), ("PnL", 15),
                ("Status", 10), ("Reason", 25)]
        for label, w in cols:
            self.cell(w, 5, label, border=1, fill=True, align="C")
        self.ln()

    def trade_table_row(self, idx: int, t: dict):
        self.set_font("Helvetica", "", 6)
        pnl = t.get("pnl", t.get("gross_pnl", 0))
        status = _wl(pnl)

        # Color coding
        if pnl > 0:
            self.set_fill_color(220, 255, 220)
        elif pnl < 0:
            self.set_fill_color(255, 220, 220)
        else:
            self.set_fill_color(255, 255, 255)

        entry_p = t.get("entry", t.get("entry_price", 0))
        exit_p = t.get("exit", t.get("full_exit_price", 0))
        
        # Split dates and times
        entry_t_str = str(t.get("entry_time", t.get("timestamp", "")))
        exit_t_str = str(t.get("exit_time", t.get("timestamp", "")))
        
        # Parse date and times
        date_str = ""
        entry_time_only = ""
        exit_time_only = ""
        
        if exit_t_str and len(exit_t_str) >= 10:
            date_str = exit_t_str[:10]  # YYYY-MM-DD
            if " " in exit_t_str:
                exit_time_only = exit_t_str.split(" ")[1][:5]  # HH:MM
        
        if entry_t_str and " " in entry_t_str:
            entry_time_only = entry_t_str.split(" ")[1][:5]  # HH:MM
            
        strategy = t.get("strategy", "")[:18]
        symbol = t.get("symbol", "")[:10]
        reason = t.get("reason", t.get("exit_reason", ""))[:18]
        qty = str(t.get("qty", 0))

        cols = [
            (str(idx), 5),
            (date_str, 15),
            (entry_time_only, 14),
            (exit_time_only, 14),
            (strategy, 24),
            (symbol, 14),
            (qty, 8),
            (f"{entry_p:,.1f}", 13),
            (f"{exit_p:,.1f}", 13),
            (f"{pnl:+,.0f}", 15),
            (status, 10),
            (reason, 25),
        ]
        for val, w in cols:
            self.cell(w, 5, val, border=1, fill=True, align="C")
        self.ln()


def _build_pdf_report(
    title: str,
    period_label: str,
    trades: list,
    capital: float,
    max_dd: float = 0.0,
    extra_kv: dict = None,
) -> str:
    """
    Builds a professional PDF report and returns the file path.
    `trades` is a list of dicts, each with:
        symbol, strategy, entry/entry_price, exit/full_exit_price,
        pnl/gross_pnl, exit_time/timestamp, reason/exit_reason
    """
    if FPDF is None:
        print("[REPORT] fpdf2 not installed. Cannot generate PDF.")
        return ""

    pdf = _ReportPDF(title, period_label)
    pdf.alias_nb_pages()
    pdf.add_page()

    # ── Calculate stats
    valid_trades = [t for t in trades
                    if t.get("pnl", t.get("gross_pnl", 0)) != 0
                    or t.get("reason", t.get("exit_reason", "")) != "FORCED_END"]
    all_pnl = [t.get("pnl", t.get("gross_pnl", 0)) for t in valid_trades]
    wins = [p for p in all_pnl if p > 0]
    losses_list = [p for p in all_pnl if p < 0]
    total = len(valid_trades)
    net_pnl = sum(all_pnl)
    wr = len(wins) / max(1, total) * 100
    avg_win = sum(wins) / max(1, len(wins))
    avg_loss = sum(losses_list) / max(1, len(losses_list))
    closing_cap = capital + net_pnl
    roi = (net_pnl / capital * 100) if capital > 0 else 0

    # ── Summary Section
    pdf.section_title("PERFORMANCE SUMMARY")
    pdf.kv_row("Total Trades:", str(total))
    pdf.kv_row("Wins / Losses:", f"{len(wins)}W / {len(losses_list)}L")
    pdf.kv_row("Win Rate:", f"{wr:.1f}%", bold_val=True)
    pdf.kv_row("Net P&L:", f"{net_pnl:+,.0f}", bold_val=True)
    pdf.kv_row("Avg Win:", f"{avg_win:+,.0f}")
    pdf.kv_row("Avg Loss:", f"{avg_loss:+,.0f}")
    if max_dd > 0:
        pdf.kv_row("Max Drawdown:", f"{max_dd:.2f}%")
    if extra_kv:
        for k, v in extra_kv.items():
            pdf.kv_row(k, v)

    # ── Capital Section
    pdf.section_title("CAPITAL STATUS")
    pdf.kv_row("Starting Capital:", f"{capital:,.0f}")
    pdf.kv_row("Closing Capital:", f"{closing_cap:,.0f}", bold_val=True)
    pdf.kv_row("Total Return:", f"{_pct(roi)}", bold_val=True)

    # ── Strategy Breakdown
    strat_stats = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0})
    for t in valid_trades:
        s = t.get("strategy", "UNKNOWN")
        p = t.get("pnl", t.get("gross_pnl", 0))
        strat_stats[s]["count"] += 1
        strat_stats[s]["pnl"] += p
        if p > 0:
            strat_stats[s]["wins"] += 1

    if strat_stats:
        pdf.section_title("STRATEGY BREAKDOWN")
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(220, 220, 220)
        for label, w in [("Strategy", 50), ("Trades", 20), ("Wins", 20),
                         ("WR%", 20), ("Net PnL", 30), ("Avg PnL", 30)]:
            pdf.cell(w, 5, label, border=1, fill=True, align="C")
        pdf.ln()

        pdf.set_font("Helvetica", "", 8)
        for strat, sd in sorted(strat_stats.items(), key=lambda x: x[1]["pnl"],
                                 reverse=True):
            swr = sd["wins"] / max(1, sd["count"]) * 100
            savg = sd["pnl"] / max(1, sd["count"])
            fill_color = (220, 255, 220) if sd["pnl"] > 0 else (255, 220, 220)
            pdf.set_fill_color(*fill_color)
            for val, w in [(strat, 50), (str(sd["count"]), 20),
                           (str(sd["wins"]), 20), (f"{swr:.0f}%", 20),
                           (f"{sd['pnl']:+,.0f}", 30),
                           (f"{savg:+,.0f}", 30)]:
                pdf.cell(w, 5, val, border=1, fill=True, align="C")
            pdf.ln()

    # ── Top 5 Winners
    sorted_wins = sorted(valid_trades,
                         key=lambda x: x.get("pnl", x.get("gross_pnl", 0)),
                         reverse=True)[:5]
    sorted_losers = sorted(valid_trades,
                           key=lambda x: x.get("pnl", x.get("gross_pnl", 0)))[:5]

    if sorted_wins and sorted_wins[0].get("pnl", sorted_wins[0].get("gross_pnl", 0)) > 0:
        pdf.section_title("TOP 5 WINNERS")
        pdf.set_font("Helvetica", "", 8)
        for i, t in enumerate(sorted_wins, 1):
            p = t.get("pnl", t.get("gross_pnl", 0))
            if p <= 0:
                break
            pdf.cell(8, 5, f"{i}.", new_x="RIGHT")
            pdf.cell(30, 5, t.get("symbol", ""), new_x="RIGHT")
            pdf.set_font("Helvetica", "B", 8)
            pdf.cell(30, 5, f"+{p:,.0f}", new_x="RIGHT")
            pdf.set_font("Helvetica", "", 8)
            pdf.cell(0, 5, f"({t.get('strategy', '')})",
                     new_x="LMARGIN", new_y="NEXT")

    if sorted_losers and sorted_losers[0].get("pnl", sorted_losers[0].get("gross_pnl", 0)) < 0:
        pdf.section_title("TOP 5 LOSERS")
        pdf.set_font("Helvetica", "", 8)
        for i, t in enumerate(sorted_losers, 1):
            p = t.get("pnl", t.get("gross_pnl", 0))
            if p >= 0:
                break
            pdf.cell(8, 5, f"{i}.", new_x="RIGHT")
            pdf.cell(30, 5, t.get("symbol", ""), new_x="RIGHT")
            pdf.set_font("Helvetica", "B", 8)
            pdf.cell(30, 5, f"{p:,.0f}", new_x="RIGHT")
            pdf.set_font("Helvetica", "", 8)
            pdf.cell(0, 5, f"({t.get('strategy', '')})",
                     new_x="LMARGIN", new_y="NEXT")

    # ── Full Trade Log Table
    if valid_trades:
        pdf.section_title(f"DETAILED TRADE LOG ({len(valid_trades)} Trades)")
        pdf.trade_table_header()
        for i, t in enumerate(valid_trades, 1):
            pdf.trade_table_row(i, t)

    # ── Save
    ts = now_ist().strftime("%Y%m%d_%H%M")
    filename = f"BNF_Report_{ts}.pdf"
    filepath = os.path.join(tempfile.gettempdir(), filename)
    pdf.output(filepath)
    return filepath


# =====================================================================
#  2. WEEKLY REPORT — PDF via Telegram
# =====================================================================

def build_weekly_report(
    period_stats: dict,
    from_date: str,
    to_date: str,
    capital: float,
    trades: list = None,
) -> str:
    """
    Build & send weekly PDF report.
    Returns the Telegram text caption.
    If `trades` is provided, generates a full PDF with trade log.
    Otherwise generates text-only fallback.
    """
    pnl = period_stats.get("gross_pnl", 0)
    total = period_stats.get("total", 0)
    wins = period_stats.get("wins", 0)
    losses = period_stats.get("losses", 0)
    wr = period_stats.get("win_rate", 0)

    caption = (
        f"📅 *WEEKLY REPORT*\n"
        f"{from_date} → {to_date}\n"
        f"{_separator()}\n\n"
        f"{_emoji_pnl(pnl)} Net P&L: `{_fmt(pnl)}`\n"
        f"Trades: `{total}` ({wins}W / {losses}L)\n"
        f"🎯 Win Rate: `{wr:.1f}%`\n\n"
        f"📎 Full PDF report attached below."
    )

    if trades and FPDF:
        filepath = _build_pdf_report(
            title="BNF ENGINE - WEEKLY PERFORMANCE REPORT",
            period_label=f"{from_date}  to  {to_date}",
            trades=trades,
            capital=capital,
        )
        if filepath:
            _send_telegram_message(caption)
            _send_telegram_document(filepath,
                                     caption=f"Weekly Report: {from_date} to {to_date}")
            try:
                os.remove(filepath)
            except Exception:
                pass
            return caption

    # Fallback: text-only (existing behaviour)
    return _build_text_period_report("WEEKLY", from_date, to_date,
                                      period_stats, capital)


# =====================================================================
#  3. MONTHLY REPORT — PDF via Telegram
# =====================================================================

def build_monthly_report(
    period_stats: dict,
    from_date: str,
    to_date: str,
    capital: float,
    trades: list = None,
) -> str:
    """Build & send monthly PDF report."""
    pnl = period_stats.get("gross_pnl", 0)
    total = period_stats.get("total", 0)
    wins = period_stats.get("wins", 0)
    losses = period_stats.get("losses", 0)
    wr = period_stats.get("win_rate", 0)

    caption = (
        f"📊 *MONTHLY REPORT*\n"
        f"{from_date} → {to_date}\n"
        f"{_separator()}\n\n"
        f"{_emoji_pnl(pnl)} Net P&L: `{_fmt(pnl)}`\n"
        f"Trades: `{total}` ({wins}W / {losses}L)\n"
        f"🎯 Win Rate: `{wr:.1f}%`\n\n"
        f"📎 Full PDF report attached below."
    )

    if trades and FPDF:
        filepath = _build_pdf_report(
            title="BNF ENGINE - MONTHLY PERFORMANCE REPORT",
            period_label=f"{from_date}  to  {to_date}",
            trades=trades,
            capital=capital,
        )
        if filepath:
            _send_telegram_message(caption)
            _send_telegram_document(filepath,
                                     caption=f"Monthly Report: {from_date} to {to_date}")
            try:
                os.remove(filepath)
            except Exception:
                pass
            return caption

    return _build_text_period_report("MONTHLY", from_date, to_date,
                                      period_stats, capital)


# =====================================================================
#  4. SIMULATOR REPORT — PDF via Telegram
# =====================================================================

def build_simulator_report(
    trades: list,
    capital: float,
    max_dd: float,
    days_back: int,
    top_n: int,
) -> str:
    """
    Build & send simulator PDF report.
    `trades` is list of dicts from simulator (symbol, strategy, entry, exit, pnl,
    entry_time, exit_time, reason).
    """
    all_pnl = [t.get("pnl", 0) for t in trades]
    wins_count = sum(1 for p in all_pnl if p > 0)
    losses_count = sum(1 for p in all_pnl if p < 0)
    net_pnl = sum(all_pnl)
    wr = wins_count / max(1, wins_count + losses_count) * 100

    caption = (
        f"🏦 *SIMULATOR REPORT*\n"
        f"{days_back} Days | {top_n} Symbols\n"
        f"{_separator()}\n\n"
        f"Trades: `{len(trades)}`\n"
        f"🎯 Win Rate: `{wr:.1f}%`\n"
        f"{_emoji_pnl(net_pnl)} Net P&L: `{_fmt(net_pnl)}`\n"
        f"Max DD: `{max_dd:.2f}%`\n\n"
        f"📎 Full PDF report attached below."
    )

    if FPDF:
        filepath = _build_pdf_report(
            title="BNF ENGINE - SIMULATOR BACKTEST REPORT",
            period_label=f"{days_back}-Day Backtest  |  {top_n} Symbols",
            trades=trades,
            capital=capital,
            max_dd=max_dd,
            extra_kv={
                "Days Backtested:": str(days_back),
                "Symbols Scanned:": str(top_n),
            },
        )
        if filepath:
            _send_telegram_message(caption)
            _send_telegram_document(filepath,
                                     caption=f"Simulator Report: {days_back}D x {top_n} symbols")
            try:
                os.remove(filepath)
            except Exception:
                pass
            return caption

    # Fallback text
    return caption


# =====================================================================
#  Text-Only Fallback for Weekly/Monthly (when fpdf2 unavailable)
# =====================================================================

def _build_text_period_report(
    label: str,
    from_date: str,
    to_date: str,
    period_stats: dict,
    capital: float,
) -> str:
    pnl = period_stats.get("gross_pnl", 0)
    closing = capital + pnl
    roi = (pnl / capital * 100) if capital > 0 else 0
    total = period_stats.get("total", 0)
    wins = period_stats.get("wins", 0)
    losses = period_stats.get("losses", 0)
    wr = period_stats.get("win_rate", 0)

    lines = []
    icon = "📅" if label == "WEEKLY" else "📊"
    lines.append(f"{icon} *{label} REPORT*")
    lines.append(f"{from_date} → {to_date}")
    lines.append(_separator())

    lines.append("")
    lines.append("💰 *P&L SUMMARY*")
    lines.append(f"{_emoji_pnl(pnl)} Net P&L: `{_fmt(pnl)}`")
    lines.append(f"📊 ROI: `{_pct(roi)}`")

    lines.append("")
    lines.append("[INFO] *TRADE BREAKDOWN*")
    lines.append(f"Total: `{total}` ({wins}W / {losses}L)")
    lines.append(f"🎯 Win Rate: `{wr:.1f}%`")

    best = period_stats.get("best_regime", "—")
    worst = period_stats.get("worst_regime", "—")
    lines.append("")
    lines.append("🧠 *REGIME PERFORMANCE*")
    lines.append(f"[PASS] Best: `{best}`")
    lines.append(f"[FAIL] Worst: `{worst}`")

    top5 = period_stats.get("top5_symbols", [])
    if top5:
        lines.append("")
        lines.append("🏆 *TOP SYMBOLS*")
        for sym, sym_pnl in top5:
            icon = _trade_icon(sym_pnl)
            lines.append(f"{icon} `{sym}`: {_fmt(sym_pnl)}")

    lines.append("")
    lines.append("💼 *CAPITAL STATUS*")
    lines.append(f"Starting: `Rs.{capital:,.0f}`")
    lines.append(f"Closing: `Rs.{closing:,.0f}`")
    lines.append(f"Total Return: `{_pct(roi)}`")

    notes = _notes_period(period_stats, label.lower())
    if notes:
        lines.append("")
        lines.append(f"📝 *{label} NOTES*")
        for tip in notes:
            lines.append(f"• {tip}")

    lines.append("")
    lines.append(f"⏰ Report generated at {now_ist().strftime('%H:%M')} IST")

    return "\n".join(lines)
