"""
trade_analysis_agent.py — Post-Trade Intelligence for BNF Engine.

Reviews completed trading calls with a focus on LOSS trades.
For each trade, answers:
  1. What went right in this trade?
  2. What went wrong?
  3. What specific fixes or strategy adjustments could improve returns?

Generates a structured Telegram report at EOD after the daily PDF report,
and optionally a detailed PDF appendix for deep-dive analysis.

Architecture:
  - Reads from Journal SQLite (trades table) for completed trades.
  - Cross-references with the DailyCache (OHLCV) and TickStore for
    market context at entry/exit time.
  - Uses rule-based heuristics (not LLM) for deterministic, reproducible analysis.
"""

import os
import datetime
from collections import defaultdict

from config import (
    now_ist, today_ist, TOTAL_CAPITAL,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS,
    EOD_SQUAREOFF_TIME,
    S6_RSI_PERIOD, S6_RSI_ENTRY_LOW, S6_RSI_EXIT,
    S7_RSI_PERIOD, S7_RSI_OVERSOLD, S7_RSI_EXIT,
    S2_BB_PERIOD, S2_BB_SD, S2_RSI_OVERSOLD, S2_RSI_OVERBOUGHT,
    S2_MAX_HOLD_MINS,
    MAX_RISK_PER_TRADE_PCT, DAILY_LOSS_LIMIT_PCT,
    JOURNAL_DB,
)


def _sanitize_pdf(text: str) -> str:
    """Replace Unicode chars unsupported by Helvetica (Latin-1) with ASCII equivalents."""
    return (text
            .replace("\u2014", "--")    # em-dash
            .replace("\u2013", "-")     # en-dash
            .replace("\u2018", "'")     # left single quote
            .replace("\u2019", "'")     # right single quote
            .replace("\u201c", '"')     # left double quote
            .replace("\u201d", '"')     # right double quote
            .replace("\u2026", "...")   # ellipsis
            .replace("\u20b9", "Rs.")   # rupee sign
            .replace("\u2192", "->")    # arrow
            .replace("\u2022", "*")     # bullet
            .replace("\u2265", ">=")    # >=
            .replace("\u2264", "<=")    # <=
            .replace("\u00d7", "x")     # multiplication sign
            )


# ── Analysis Heuristics ──────────────────────────────────────────

class TradeAnalysis:
    """Analysis result for a single trade."""

    def __init__(self, trade: dict):
        self.trade = trade
        self.symbol = trade.get("symbol", "")
        self.strategy = trade.get("strategy", "")
        self.pnl = trade.get("gross_pnl", trade.get("pnl", 0))
        self.is_win = self.pnl > 0
        self.is_loss = self.pnl < 0
        self.entry_price = trade.get("entry_price", 0)
        self.exit_price = trade.get("full_exit_price", 0)
        self.qty = trade.get("qty", 0)
        self.exit_reason = trade.get("exit_reason", "")
        self.hold_minutes = trade.get("hold_minutes", 0)

        # Analysis fields
        self.positives: list[str] = []
        self.negatives: list[str] = []
        self.fixes: list[str] = []
        self.risk_score: str = "NORMAL"  # LOW / NORMAL / HIGH / CRITICAL
        self.grade: str = "C"  # A / B / C / D / F


def analyze_trade(trade: dict, market_context: dict = None) -> TradeAnalysis:
    """
    Run a comprehensive rule-based analysis on a single completed trade.

    Parameters:
        trade: dict from Journal.get_all_trades_for_date()
        market_context: optional dict with:
            - regime: str
            - vix: float
            - nifty_change_pct: float
            - sector_performance: dict
            - intraday_high/low for the symbol
    """
    a = TradeAnalysis(trade)
    ctx = market_context or {}

    entry = a.entry_price
    exit_p = a.exit_price
    strategy = a.strategy
    reason = a.exit_reason
    is_short = "SHORT" in strategy.upper()

    # ── 1. Direction Analysis ──────────────────────────────────
    if is_short:
        move_pct = ((entry - exit_p) / entry * 100) if entry > 0 else 0
    else:
        move_pct = ((exit_p - entry) / entry * 100) if entry > 0 else 0

    # ── 2. Position Sizing Analysis ───────────────────────────
    position_value = entry * a.qty
    capital = TOTAL_CAPITAL
    position_pct = (position_value / capital * 100) if capital > 0 else 0
    risk_pct = (abs(a.pnl) / capital * 100) if capital > 0 else 0

    if position_pct > 15:
        a.negatives.append(f"Position too large: {position_pct:.1f}% of capital (max 15%)")
        a.fixes.append("Reduce position size — enforce MAX_POSITION_PCT=0.15 strictly")
        a.risk_score = "HIGH"
    elif position_pct < 3:
        a.negatives.append(f"Position very small: {position_pct:.1f}% — limited profit potential")
        a.fixes.append("Consider increasing position size for high-conviction setups")

    # ── 3. Exit Reason Analysis ───────────────────────────────
    if reason == "MIS_EOD_SQUAREOFF":
        if a.is_loss:
            a.negatives.append("Trade held until forced EOD squareoff — no protective exit triggered")
            a.fixes.append("Set tighter time-based stops (exit 30 min before EOD if in loss)")
            a.fixes.append("Review if SL was too wide — price never reached stop but drifted against")
        elif a.is_win:
            a.positives.append("Trade was in profit at EOD squareoff — good directional call")
            a.negatives.append("Target was not hit — profit left on the table by waiting for EOD")
            a.fixes.append("Consider scaling out 50% at 1:1 RR to lock partial profit")

    elif reason == "STOP_HIT":
        a.negatives.append("Stop loss was triggered — entry timing or SL placement needs review")
        if abs(move_pct) < 0.5:
            a.negatives.append(f"Price moved only {abs(move_pct):.2f}% against — stop was too tight")
            a.fixes.append("Widen SL by 0.2-0.5 ATR to avoid noise-triggered exits")
        else:
            a.fixes.append("Review if entry was at a poor price level (e.g., mid-range, no support/resistance)")

    elif reason == "TARGET_HIT":
        a.positives.append("Target was hit cleanly — strategy thesis played out correctly")
        a.positives.append(f"Captured {abs(move_pct):.2f}% move — good execution")

    elif "MACRO_FLIP" in reason:
        if a.is_loss:
            a.negatives.append("Regime flipped against position — macro condition changed mid-trade")
            a.fixes.append("Check MacroAgent veto more aggressively before entry")
            a.fixes.append("Reduce position size when regime is ambiguous or volatile")
        else:
            a.positives.append("Exited on regime change while still in profit — good risk management")

    elif "RSI_EXIT" in reason:
        if a.is_win:
            a.positives.append("Dynamic RSI exit triggered in profit — momentum exhaustion detected correctly")
        else:
            a.negatives.append("RSI exit triggered but trade was still in loss — entry timing was off")
            a.fixes.append("Wait for deeper RSI extreme before entry (lower for longs, higher for shorts)")

    elif reason == "TIME_STOP":
        a.negatives.append("Time-based exit triggered — trade didn't develop as expected")
        a.fixes.append("Review if market conditions (low volume, chop) were unsuitable for this strategy")

    # ── 4. Strategy-Specific Analysis ─────────────────────────

    if "S6" in strategy:
        _analyze_s6_short(a, ctx)
    elif "S7" in strategy:
        _analyze_s7_mean_rev(a, ctx)
    elif "S2" in strategy:
        _analyze_s2_bb_mean_rev(a, ctx)
    elif "S8" in strategy or "MACRO" in strategy:
        _analyze_macro_trade(a, ctx)
    elif "S3" in strategy:
        _analyze_s3_orb(a, ctx)

    # ── 5. Universal Checks ───────────────────────────────────

    # No-Trade Zone violation
    entry_time = trade.get("entry_time", "")
    if entry_time and isinstance(entry_time, str) and " " in entry_time:
        try:
            time_part = entry_time.split(" ")[1][:5]
            hour, minute = int(time_part[:2]), int(time_part[3:5])
            if 11 <= hour < 14 and (hour != 11 or minute >= 15):
                a.negatives.append(f"Entered during No-Trade Zone ({time_part}) — historically low-probability window")
                a.fixes.append("Avoid entries between 11:15-13:45 IST (lunch chop zone)")
        except (ValueError, IndexError):
            pass

    # VIX context
    vix = ctx.get("vix", 0)
    if vix > 22 and a.is_loss:
        a.negatives.append(f"VIX was elevated at {vix:.1f} — high volatility increases whipsaw risk")
        a.fixes.append("Reduce position sizes by 30-50% when VIX > 22")
    elif vix < 12 and a.is_loss:
        a.negatives.append(f"VIX was very low ({vix:.1f}) — low volatility means range-bound action")
        a.fixes.append("Breakout strategies underperform in low-vol; prefer mean-reversion")

    # Risk/Reward realized
    if a.is_win:
        a.positives.append(f"Profit: +₹{a.pnl:,.0f} ({abs(move_pct):.2f}% move captured)")
    elif a.is_loss:
        a.negatives.append(f"Loss: -₹{abs(a.pnl):,.0f} ({abs(move_pct):.2f}% adverse move)")

    # ── 6. Grade Assignment ───────────────────────────────────
    a.grade = _compute_grade(a)

    # ── 7. Default positives if none found ────────────────────
    if not a.positives:
        if a.is_win:
            a.positives.append("Trade was profitable — good execution overall")
        else:
            a.positives.append("Strategy rules were followed — systematic execution")

    if not a.negatives and a.is_loss:
        a.negatives.append("Market moved against the directional thesis")

    if not a.fixes and a.is_loss:
        a.fixes.append("Review if entry conditions were marginal — consider stricter filters")

    return a


# ── Strategy-Specific Analyzers ──────────────────────────────────

def _analyze_s6_short(a: TradeAnalysis, ctx: dict):
    """S6 Trend Short specific analysis."""
    regime = ctx.get("regime", "")

    if a.is_loss:
        if regime == "BULL":
            a.negatives.append("Shorted in a BULL regime — trend was against the position")
            a.fixes.append("Disable S6 short entries in BULL regime or require extreme RSI > 85")
        a.negatives.append("Short trade failed — potential bear trap or V-reversal")
        a.fixes.append("Add volume confirmation: only short if selling volume > 2x average on breakdown")
    else:
        a.positives.append("Correctly identified relative weakness for short entry")
        if "S6_RSI_EXIT" in a.exit_reason:
            a.positives.append("Exited when RSI cooled below threshold — good momentum tracking")


def _analyze_s7_mean_rev(a: TradeAnalysis, ctx: dict):
    """S7 Mean Reversion Long specific analysis."""
    regime = ctx.get("regime", "")

    if a.is_loss:
        if regime in ("BEAR_PANIC", "EXTREME_PANIC"):
            a.negatives.append("Mean-reversion buy in panic regime — catching a falling knife")
            a.fixes.append("Disable S7 in BEAR_PANIC/EXTREME_PANIC regimes entirely")
        a.negatives.append("Mean-reversion long failed — stock continued lower")
        a.fixes.append(f"Require RSI < {S7_RSI_OVERSOLD - 5} instead of {S7_RSI_OVERSOLD} for deeper oversold confirmation")
        a.fixes.append("Add VWAP trend filter — only buy if intraday VWAP is rising")
    else:
        a.positives.append("Mean-reversion thesis confirmed — oversold bounce materialized")


def _analyze_s2_bb_mean_rev(a: TradeAnalysis, ctx: dict):
    """S2 Bollinger Band Mean Reversion analysis."""
    if a.is_loss:
        a.negatives.append("BB mean-reversion failed — price broke through the band instead of reverting")
        a.fixes.append(f"Consider waiting for candle close outside BB ({S2_BB_SD} SD) before entry")
        if a.hold_minutes and a.hold_minutes > S2_MAX_HOLD_MINS:
            a.negatives.append(f"Held {a.hold_minutes:.0f} min — exceeded max hold ({S2_MAX_HOLD_MINS} min)")
    else:
        a.positives.append("BB reversion worked — price snapped back to mean from extreme")


def _analyze_macro_trade(a: TradeAnalysis, ctx: dict):
    """S8/Macro news-driven trade analysis."""
    if a.is_loss:
        a.negatives.append("News/Macro-driven trade failed — market may have already priced in the event")
        a.fixes.append("Verify if news was already reflected in pre-market or previous session")
        a.fixes.append("Use tighter stops for news trades — events create volatile whipsaws")
    else:
        a.positives.append("News catalyst correctly interpreted — strong directional move captured")


def _analyze_s3_orb(a: TradeAnalysis, ctx: dict):
    """S3 Opening Range Breakout analysis."""
    if a.is_loss:
        a.negatives.append("ORB breakout was a false breakout — price reversed into the range")
        a.fixes.append("Require volume > 2x average on the breakout candle as confirmation")
        a.fixes.append("Add retest confirmation — wait for pullback to breakout level before entry")
    else:
        a.positives.append("Opening range breakout confirmed with follow-through momentum")


# ── Grading System ───────────────────────────────────────────────

def _compute_grade(a: TradeAnalysis) -> str:
    """
    Grade a trade from A to F based on execution quality.
    A = Excellent (clean win, rules followed)
    B = Good (win or small loss, acceptable execution)
    C = Average (typical loss, normal market risk)
    D = Poor (preventable loss, rule violations)
    F = Failed (large loss, multiple errors)
    """
    score = 50  # Start at C

    # P&L impact
    if a.is_win:
        score += 20
        if a.exit_reason == "TARGET_HIT":
            score += 15  # Clean execution
    elif a.is_loss:
        risk_pct = (abs(a.pnl) / TOTAL_CAPITAL * 100) if TOTAL_CAPITAL > 0 else 0
        if risk_pct > MAX_RISK_PER_TRADE_PCT * 100 * 1.5:
            score -= 25  # Oversized loss
        elif risk_pct > MAX_RISK_PER_TRADE_PCT * 100:
            score -= 15
        else:
            score -= 5  # Controlled loss

    # Deductions for errors
    score -= len(a.negatives) * 5
    score += len(a.positives) * 3

    # Risk score impact
    if a.risk_score == "CRITICAL":
        score -= 20
    elif a.risk_score == "HIGH":
        score -= 10

    # Clamp to grade
    if score >= 80:
        return "A"
    elif score >= 65:
        return "B"
    elif score >= 45:
        return "C"
    elif score >= 30:
        return "D"
    return "F"


# ── Report Builder ───────────────────────────────────────────────

class TradeAnalysisAgent:
    """
    Runs at EOD after the daily report.
    Analyzes all trades from today and generates an actionable intelligence report.
    """

    def __init__(self, journal=None):
        from core.journal import Journal
        self.journal = journal or Journal()

    def run_daily_analysis(
        self,
        date_str: str = None,
        regime: str = "UNKNOWN",
        vix: float = 0,
        alert_fn=None,
    ) -> str:
        """
        Run the full daily trade analysis and send via Telegram.

        Returns: The formatted analysis report string.
        """
        d = date_str or today_ist().isoformat()
        trades = self.journal.get_all_trades_for_date(d)

        if not trades:
            return ""

        # Build market context
        ctx = {"regime": regime, "vix": vix}

        # Analyze each trade
        analyses = [analyze_trade(t, ctx) for t in trades]

        # Build the report
        report = self._build_report(analyses, d, regime)

        # Send via Telegram
        if alert_fn:
            alert_fn(report)
        else:
            _send_analysis_telegram(report)

        # Build and send PDF if fpdf2 is available
        try:
            pdf_path = self._build_pdf_report(analyses, d, regime)
            if pdf_path:
                _send_analysis_document(pdf_path, f"Trade Analysis: {d}")
                try:
                    os.remove(pdf_path)
                except Exception:
                    pass
        except Exception as e:
            print(f"[TradeAnalysis] PDF generation failed: {e}")

        return report

    def _build_report(self, analyses: list, date_str: str, regime: str) -> str:
        """Build the Telegram-formatted analysis report."""
        losses = [a for a in analyses if a.is_loss]
        wins = [a for a in analyses if a.is_win]
        total_pnl = sum(a.pnl for a in analyses)

        lines = [
            f"🔬 *TRADE ANALYSIS REPORT*",
            f"Date: `{date_str}` | Regime: `{regime}`",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        # Grade distribution
        grades = defaultdict(int)
        for a in analyses:
            grades[a.grade] += 1
        grade_str = " | ".join(f"{g}:{c}" for g, c in sorted(grades.items()))
        lines.append(f"📊 Grades: `{grade_str}`")
        lines.append(f"📉 Losses Analyzed: `{len(losses)}/{len(analyses)}`")
        lines.append("")

        # ── LOSS TRADES: Deep Analysis ──
        if losses:
            lines.append("❌ *LOSS TRADE ANALYSIS*")
            lines.append("")

            for i, a in enumerate(sorted(losses, key=lambda x: x.pnl), 1):
                direction = "SHORT" if "SHORT" in a.strategy.upper() else "LONG"
                lines.append(
                    f"*{i}. {a.symbol}* [{a.strategy}] ({direction})"
                )
                lines.append(
                    f"   PnL: `₹{a.pnl:+,.0f}` | Grade: `{a.grade}` | "
                    f"Exit: `{a.exit_reason}`"
                )

                # What went wrong
                if a.negatives:
                    lines.append("   ⚠️ _Issues:_")
                    for neg in a.negatives[:3]:
                        lines.append(f"   • {neg}")

                # Fixes
                if a.fixes:
                    lines.append("   🔧 _Fixes:_")
                    for fix in a.fixes[:2]:
                        lines.append(f"   → {fix}")

                # What went right (even in losses)
                if a.positives:
                    lines.append("   ✅ _Positives:_")
                    for pos in a.positives[:1]:
                        lines.append(f"   • {pos}")

                lines.append("")

        # ── WIN TRADES: Brief Summary ──
        if wins:
            lines.append("✅ *WINNING TRADES*")
            for a in sorted(wins, key=lambda x: x.pnl, reverse=True):
                lines.append(
                    f"  `{a.symbol}` [{a.strategy}] → "
                    f"`₹{a.pnl:+,.0f}` (Grade: {a.grade})"
                )
                if a.positives:
                    lines.append(f"   • {a.positives[0]}")
            lines.append("")

        # ── AGGREGATE INSIGHTS ──
        lines.append("📋 *KEY TAKEAWAYS*")

        # Strategy performance
        strat_pnl = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0})
        for a in analyses:
            s = a.strategy.split("_")[0] if "_" in a.strategy else a.strategy
            strat_pnl[s]["pnl"] += a.pnl
            if a.is_win:
                strat_pnl[s]["wins"] += 1
            elif a.is_loss:
                strat_pnl[s]["losses"] += 1

        worst_strat = min(strat_pnl.items(), key=lambda x: x[1]["pnl"], default=None)
        if worst_strat and worst_strat[1]["pnl"] < 0:
            lines.append(
                f"  📉 Weakest: `{worst_strat[0]}` "
                f"(₹{worst_strat[1]['pnl']:+,.0f}, "
                f"{worst_strat[1]['losses']}L)"
            )

        best_strat = max(strat_pnl.items(), key=lambda x: x[1]["pnl"], default=None)
        if best_strat and best_strat[1]["pnl"] > 0:
            lines.append(
                f"  📈 Strongest: `{best_strat[0]}` "
                f"(₹{best_strat[1]['pnl']:+,.0f}, "
                f"{best_strat[1]['wins']}W)"
            )

        # Common failure patterns
        eod_losses = sum(1 for a in losses if a.exit_reason == "MIS_EOD_SQUAREOFF")
        sl_losses = sum(1 for a in losses if a.exit_reason == "STOP_HIT")
        macro_losses = sum(1 for a in losses if "MACRO" in a.exit_reason)

        if eod_losses > 0:
            lines.append(
                f"  ⏰ `{eod_losses}` loss(es) from EOD squareoff — "
                f"consider earlier time-based exits"
            )
        if sl_losses > 0:
            lines.append(
                f"  🛑 `{sl_losses}` stop loss hit(s) — "
                f"review SL placement relative to ATR"
            )
        if macro_losses > 0:
            lines.append(
                f"  🌍 `{macro_losses}` macro regime flip loss(es) — "
                f"strengthen pre-entry regime filter"
            )

        # Overall recommendation
        lines.append("")
        win_rate = len(wins) / max(len(analyses), 1) * 100
        if win_rate < 40:
            lines.append(
                "⚡ *ALERT*: Win rate below 40% — "
                "consider pausing aggressive strategies tomorrow"
            )
        elif win_rate >= 60:
            lines.append(
                "💪 *STRONG DAY*: Win rate above 60% — "
                "strategy-market alignment was good"
            )

        lines.append(f"\n⏰ Analysis generated at {now_ist().strftime('%H:%M')} IST")

        return "\n".join(lines)

    def _build_pdf_report(self, analyses: list, date_str: str, regime: str) -> str:
        """Build a detailed PDF trade analysis report."""
        try:
            from fpdf import FPDF
        except ImportError:
            return ""

        class AnalysisPDF(FPDF):
            def __init__(self):
                super().__init__()
                self.set_auto_page_break(auto=True, margin=15)

            def header(self):
                self.set_font("Helvetica", "B", 14)
                self.cell(0, 8, "BNF ENGINE - TRADE ANALYSIS REPORT",
                          new_x="LMARGIN", new_y="NEXT", align="C")
                self.set_font("Helvetica", "", 9)
                self.cell(0, 5, f"Date: {date_str} | Regime: {regime}",
                          new_x="LMARGIN", new_y="NEXT", align="C")
                self.line(10, self.get_y() + 2, 200, self.get_y() + 2)
                self.ln(5)

            def footer(self):
                self.set_y(-15)
                self.set_font("Helvetica", "I", 7)
                ts = now_ist().strftime("%d %b %Y %H:%M IST")
                self.cell(0, 10,
                          f"BNF Engine Trade Analysis | Generated: {ts} | "
                          f"Page {self.page_no()}/{{nb}}",
                          align="C")

            def section(self, title):
                self.ln(3)
                self.set_font("Helvetica", "B", 11)
                self.set_fill_color(30, 30, 30)
                self.set_text_color(255, 255, 255)
                self.cell(0, 7, f"  {title}", new_x="LMARGIN", new_y="NEXT",
                          fill=True)
                self.set_text_color(0, 0, 0)
                self.ln(2)

        pdf = AnalysisPDF()
        pdf.alias_nb_pages()
        pdf.add_page()

        # ── Summary Section
        losses = [a for a in analyses if a.is_loss]
        wins = [a for a in analyses if a.is_win]

        pdf.section("ANALYSIS SUMMARY")
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(55, 5, "Total Trades Analyzed:", new_x="RIGHT")
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, str(len(analyses)), new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("Helvetica", "", 9)
        pdf.cell(55, 5, "Loss Trades:", new_x="RIGHT")
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, str(len(losses)), new_x="LMARGIN", new_y="NEXT")

        grades = defaultdict(int)
        for a in analyses:
            grades[a.grade] += 1
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(55, 5, "Grade Distribution:", new_x="RIGHT")
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5,
                 " | ".join(f"{g}:{c}" for g, c in sorted(grades.items())),
                 new_x="LMARGIN", new_y="NEXT")

        # ── Loss Trade Deep Dives
        if losses:
            pdf.section(f"LOSS TRADE ANALYSIS ({len(losses)} Trades)")

            for i, a in enumerate(sorted(losses, key=lambda x: x.pnl), 1):
                direction = "SHORT" if "SHORT" in a.strategy.upper() else "LONG"

                # Trade header
                pdf.set_font("Helvetica", "B", 9)
                if a.pnl < -100:
                    pdf.set_fill_color(255, 200, 200)
                else:
                    pdf.set_fill_color(255, 230, 230)
                pdf.cell(0, 6,
                         _sanitize_pdf(f"  {i}. {a.symbol} | {a.strategy} ({direction}) | "
                         f"PnL: {a.pnl:+,.0f} | Grade: {a.grade}"),
                         new_x="LMARGIN", new_y="NEXT", fill=True)

                # Details
                pdf.set_font("Helvetica", "", 8)
                pdf.cell(0, 5,
                         _sanitize_pdf(f"  Entry: {a.entry_price:,.1f} | "
                         f"Exit: {a.exit_price:,.1f} | "
                         f"Qty: {a.qty} | Exit: {a.exit_reason}"),
                         new_x="LMARGIN", new_y="NEXT")

                # What went wrong
                if a.negatives:
                    pdf.set_font("Helvetica", "B", 8)
                    pdf.cell(0, 5, "  Issues:", new_x="LMARGIN", new_y="NEXT")
                    pdf.set_font("Helvetica", "", 8)
                    for neg in a.negatives:
                        pdf.cell(0, 4, _sanitize_pdf(f"    - {neg[:90]}"),
                                 new_x="LMARGIN", new_y="NEXT")

                # Fixes
                if a.fixes:
                    pdf.set_font("Helvetica", "B", 8)
                    pdf.cell(0, 5, "  Recommended Fixes:",
                             new_x="LMARGIN", new_y="NEXT")
                    pdf.set_font("Helvetica", "", 8)
                    for fix in a.fixes:
                        pdf.cell(0, 4, _sanitize_pdf(f"    > {fix[:90]}"),
                                 new_x="LMARGIN", new_y="NEXT")

                # Positives
                if a.positives:
                    pdf.set_font("Helvetica", "B", 8)
                    pdf.cell(0, 5, "  What Went Right:",
                             new_x="LMARGIN", new_y="NEXT")
                    pdf.set_font("Helvetica", "", 8)
                    for pos in a.positives:
                        pdf.cell(0, 4, _sanitize_pdf(f"    + {pos[:90]}"),
                                 new_x="LMARGIN", new_y="NEXT")

                pdf.ln(3)

        # ── Win Trade Summary
        if wins:
            pdf.section(f"WINNING TRADES ({len(wins)} Trades)")
            for a in sorted(wins, key=lambda x: x.pnl, reverse=True):
                pdf.set_font("Helvetica", "", 8)
                pdf.set_fill_color(220, 255, 220)
                pdf.cell(0, 5,
                         _sanitize_pdf(f"  {a.symbol} | {a.strategy} | "
                         f"PnL: +{a.pnl:,.0f} | Grade: {a.grade}"),
                         new_x="LMARGIN", new_y="NEXT", fill=True)
                if a.positives:
                    pdf.cell(0, 4, _sanitize_pdf(f"    + {a.positives[0][:90]}"),
                             new_x="LMARGIN", new_y="NEXT")

        # Save
        import tempfile
        ts = now_ist().strftime("%Y%m%d_%H%M")
        filename = f"BNF_TradeAnalysis_{ts}.pdf"
        filepath = os.path.join(tempfile.gettempdir(), filename)
        pdf.output(filepath)
        return filepath


# ── Telegram Helpers ─────────────────────────────────────────────

def _send_analysis_telegram(text: str):
    """Send analysis report via Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        print(f"[TradeAnalysis] {text}")
        return
    import requests
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text,
                      "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception:
            pass


def _send_analysis_document(filepath: str, caption: str = ""):
    """Send analysis PDF via Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        return
    import requests
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
            print(f"[TradeAnalysis] PDF send failed: {e}")
