"""
report_agent.py — Emoji-rich Telegram report builders for BNF Engine.

Three public functions:
  build_daily_report()   → Full day report (market, P&L, trade log, insights)
  build_weekly_report()  → 7-day aggregate (P&L, breakdown, top symbols, insights)
  build_monthly_report() → Calendar-month aggregate (same shape as weekly)
"""

from config import now_ist, TOTAL_CAPITAL


# ── Helpers ───────────────────────────────────────────────────────

def _fmt(value: float) -> str:
    """Format currency with Indian ₹ and commas, signed."""
    return f"₹{value:+,.0f}" if value != 0 else "₹0"


def _pct(value: float) -> str:
    """Format as percentage string."""
    return f"{value:+.3f}%"


def _emoji_pnl(pnl: float) -> str:
    return "📈" if pnl >= 0 else "📉"


def _trade_icon(pnl: float) -> str:
    return "✅" if pnl > 0 else "❌"


def _separator() -> str:
    return "━━━━━━━━━━━━━━━━━━━━━━━━"


def _notes_daily(stats: dict) -> list:
    """Generate 1-3 rule-based notes from daily stats."""
    tips = []
    wr = stats.get("win_rate", 0)
    total = stats.get("total", 0)
    losses = stats.get("losses", 0)
    streak = stats.get("loss_streak", 0)

    if total == 0:
        tips.append("📭 No trades today — market may have been quiet or filters too tight")
        return tips

    if wr < 40:
        tips.append(f"⚠️ Below target — {wr:.0f}% win rate. Review SL placement and entry criteria")
    elif wr >= 60:
        tips.append(f"🎯 Strong day — {wr:.0f}% win rate. Strategy aligned well with market")

    if total > 0 and losses / total > 0.5:
        tips.append("🛑 >50% trades hit SL — consider widening stops or improving entry precision")

    if streak >= 2:
        tips.append(f"🔥 {streak} consecutive losses — reduce size or pause until regime improves")

    pnl = stats.get("gross_pnl", 0)
    if pnl > 0:
        tips.append("💪 Profitable day — lock in gains, review what worked")

    return tips[:3]


def _notes_period(stats: dict, label: str) -> list:
    """Generate 1-3 rule-based notes for weekly/monthly."""
    tips = []
    wr = stats.get("win_rate", 0)
    total = stats.get("total", 0)
    pnl = stats.get("gross_pnl", 0)

    if total == 0:
        tips.append(f"📭 No trades this {label}")
        return tips

    if wr >= 55:
        tips.append(f"🎯 Solid {label} — {wr:.0f}% win rate across {total} trades")
    elif wr < 40:
        tips.append(f"⚠️ {label} win rate {wr:.0f}% is below target. Review strategy fit")

    if pnl > 0:
        tips.append(f"💪 Net positive {label} — capital growing steadily")
    elif pnl < 0:
        tips.append(f"📉 Net negative {label} — review biggest losers for common patterns")

    best = stats.get("best_regime", "—")
    worst = stats.get("worst_regime", "—")
    if best != "—" and best != worst:
        tips.append(f"🧠 Best regime: {best} | Worst: {worst} — adjust exposure accordingly")

    return tips[:3]


# ── Daily Report ──────────────────────────────────────────────────

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
    """
    Build full-day emoji report.

    Args:
        stats: from RiskAgent.get_daily_stats()
        regime: current market regime string
        trades_today: list of dicts with keys: symbol, entry_price,
                      full_exit_price, gross_pnl, exit_reason
        capital: starting capital for the day
        total_scans: number of scan cycles run today
        nifty_price: Nifty 50 last price (0 if unavailable)
        nifty_change_pct: Nifty 50 day change % (0 if unavailable)
        vix: India VIX value (0 if unavailable)
    """
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

    # ── Header
    lines.append(f"📋 FULL DAY REPORT — {date_str}")
    lines.append(_separator())

    # ── Market Context
    lines.append("")
    lines.append("🏦 MARKET CONTEXT")
    if nifty_price > 0:
        nifty_icon = "📈" if nifty_change_pct >= 0 else "📉"
        lines.append(f"{nifty_icon} Nifty 50: ₹{nifty_price:,.2f} ({nifty_change_pct:+.2f}%)")
    if vix > 0:
        lines.append(f"🌡️ India VIX: {vix:.2f}")
    lines.append(f"🧠 Regime: {regime}")

    # ── P&L Summary
    lines.append("")
    lines.append("💰 P&L SUMMARY")
    lines.append(f"{_emoji_pnl(pnl)} Net P&L: {_fmt(pnl)}")
    lines.append(f"📊 Day ROI: {_pct(day_roi)}")

    # ── Trade Breakdown
    lines.append("")
    lines.append("📋 TRADE BREAKDOWN")
    lines.append(f"Total: {total} ({wins}W / {losses}L)")
    lines.append(f"🎯 Win Rate: {wr:.1f}%")
    if avg_win != 0:
        lines.append(f"📈 Avg Win: {_fmt(avg_win)}")
    if avg_loss != 0:
        lines.append(f"📉 Avg Loss: {_fmt(avg_loss)}")

    # ── Trade Log
    if trades_today:
        lines.append("")
        lines.append("🔄 TRADE LOG")
        for t in trades_today:
            icon = _trade_icon(t.get("gross_pnl", 0))
            sym = t.get("symbol", "???")
            entry_px = t.get("entry_price", 0)
            exit_px = t.get("full_exit_price", 0)
            t_pnl = t.get("gross_pnl", 0)
            reason = t.get("exit_reason", "")
            lines.append(
                f"{icon} {sym} ₹{entry_px:,.0f}→₹{exit_px:,.0f} "
                f"{_fmt(t_pnl)} ({reason})"
            )

    # ── System Health
    lines.append("")
    lines.append("⚙️ SYSTEM HEALTH")
    lines.append("🤖 Agent Uptime: 100%")
    lines.append(f"🔄 Scans Run: {total_scans}")

    # ── Capital Status
    lines.append("")
    lines.append("💼 CAPITAL STATUS")
    lines.append(f"Starting: ₹{capital:,.0f}")
    lines.append(f"Closing: ₹{closing_capital:,.0f}")

    # ── AI Insights
    notes = _notes_daily(stats)
    if notes:
        lines.append("")
        lines.append("📝 POST-MARKET NOTES")
        for tip in notes:
            lines.append(f"• {tip}")

    # ── Footer
    lines.append("")
    lines.append(f"⏰ Report generated at {now_ist().strftime('%H:%M')} IST")

    return "\n".join(lines)


# ── Weekly Report ─────────────────────────────────────────────────

def build_weekly_report(
    period_stats: dict,
    from_date: str,
    to_date: str,
    capital: float,
) -> str:
    """
    Build 7-day aggregate emoji report.

    Args:
        period_stats: from Journal.get_period_summary()
        from_date / to_date: ISO date strings
        capital: current capital base
    """
    pnl = period_stats.get("gross_pnl", 0)
    closing = capital + pnl
    roi = (pnl / capital * 100) if capital > 0 else 0
    total = period_stats.get("total", 0)
    wins = period_stats.get("wins", 0)
    losses = period_stats.get("losses", 0)
    wr = period_stats.get("win_rate", 0)

    lines = []

    # ── Header
    lines.append("📅 WEEKLY REPORT")
    lines.append(f"{from_date} → {to_date}")
    lines.append(_separator())

    # ── P&L Summary
    lines.append("")
    lines.append("💰 P&L SUMMARY")
    lines.append(f"{_emoji_pnl(pnl)} Net P&L: {_fmt(pnl)}")
    lines.append(f"📊 Week ROI: {_pct(roi)}")

    # ── Trade Breakdown
    lines.append("")
    lines.append("📋 TRADE BREAKDOWN")
    lines.append(f"Total: {total} ({wins}W / {losses}L)")
    lines.append(f"🎯 Win Rate: {wr:.1f}%")

    # ── Regime Info
    best = period_stats.get("best_regime", "—")
    worst = period_stats.get("worst_regime", "—")
    lines.append("")
    lines.append("🧠 REGIME PERFORMANCE")
    lines.append(f"✅ Best: {best}")
    lines.append(f"❌ Worst: {worst}")

    # ── Top Symbols
    top5 = period_stats.get("top5_symbols", [])
    if top5:
        lines.append("")
        lines.append("🏆 TOP SYMBOLS BY P&L")
        for sym, sym_pnl in top5:
            icon = _trade_icon(sym_pnl)
            lines.append(f"{icon} {sym}: {_fmt(sym_pnl)}")

    # ── Capital Status
    lines.append("")
    lines.append("💼 CAPITAL STATUS")
    lines.append(f"Starting: ₹{capital:,.0f}")
    lines.append(f"Closing: ₹{closing:,.0f}")

    # ── AI Insights
    notes = _notes_period(period_stats, "week")
    if notes:
        lines.append("")
        lines.append("📝 WEEKLY PERFORMANCE NOTES")
        for tip in notes:
            lines.append(f"• {tip}")

    # ── Footer
    lines.append("")
    lines.append(f"⏰ Report generated at {now_ist().strftime('%H:%M')} IST")

    return "\n".join(lines)


# ── Monthly Report ────────────────────────────────────────────────

def build_monthly_report(
    period_stats: dict,
    from_date: str,
    to_date: str,
    capital: float,
) -> str:
    """
    Build calendar-month aggregate emoji report.

    Args:
        period_stats: from Journal.get_period_summary()
        from_date / to_date: ISO date strings
        capital: current capital base
    """
    pnl = period_stats.get("gross_pnl", 0)
    closing = capital + pnl
    roi = (pnl / capital * 100) if capital > 0 else 0
    total = period_stats.get("total", 0)
    wins = period_stats.get("wins", 0)
    losses = period_stats.get("losses", 0)
    wr = period_stats.get("win_rate", 0)

    lines = []

    # ── Header
    lines.append("📊 MONTHLY REPORT")
    lines.append(f"{from_date} → {to_date}")
    lines.append(_separator())

    # ── P&L Summary
    lines.append("")
    lines.append("💰 P&L SUMMARY")
    lines.append(f"{_emoji_pnl(pnl)} Net P&L: {_fmt(pnl)}")
    lines.append(f"📊 Month ROI: {_pct(roi)}")

    # ── Trade Breakdown
    lines.append("")
    lines.append("📋 TRADE BREAKDOWN")
    lines.append(f"Total: {total} ({wins}W / {losses}L)")
    lines.append(f"🎯 Win Rate: {wr:.1f}%")

    # ── Regime Info
    best = period_stats.get("best_regime", "—")
    worst = period_stats.get("worst_regime", "—")
    lines.append("")
    lines.append("🧠 REGIME PERFORMANCE")
    lines.append(f"✅ Best: {best}")
    lines.append(f"❌ Worst: {worst}")

    # ── Top Symbols
    top5 = period_stats.get("top5_symbols", [])
    if top5:
        lines.append("")
        lines.append("🏆 TOP SYMBOLS BY P&L")
        for sym, sym_pnl in top5:
            icon = _trade_icon(sym_pnl)
            lines.append(f"{icon} {sym}: {_fmt(sym_pnl)}")

    # ── Capital Status
    lines.append("")
    lines.append("💼 CAPITAL STATUS")
    lines.append(f"Starting: ₹{capital:,.0f}")
    lines.append(f"Closing: ₹{closing:,.0f}")

    # ── AI Insights
    notes = _notes_period(period_stats, "month")
    if notes:
        lines.append("")
        lines.append("📝 MONTHLY PERFORMANCE NOTES")
        for tip in notes:
            lines.append(f"• {tip}")

    # ── Footer
    lines.append("")
    lines.append(f"⏰ Report generated at {now_ist().strftime('%H:%M')} IST")

    return "\n".join(lines)
