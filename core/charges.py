"""
core/charges.py — Centralized Zerodha 2026 Trade Charge Calculator

Single source of truth for all charge calculations used by:
  - risk_agent.py (live P&L deduction)
  - report_agent.py (PDF report display)
  - simulator.py (backtest P&L)
  - trade_analysis_agent.py (post-trade analysis)

All values per SEBI/Zerodha schedule effective 2026:
  Brokerage:     Rs.20 per order or 0.03% (whichever is lower)
  STT:           0.025% on sell side (intraday equity)
  Txn Charges:   NSE 0.00297% on turnover
  SEBI Fee:      0.0001% on turnover
  Stamp Duty:    0.003% on buy side (intraday)
  GST:           18% on (brokerage + txn + SEBI)
  DP Charges:    Rs.15.93 per sell delivery (CNC only)
"""


def compute_trade_charges(
    buy_value: float,
    sell_value: float,
    product: str = "MIS",
    slippage_pct: float = 0.0,
) -> dict:
    """
    Compute all statutory charges for a round-trip trade.

    Args:
        buy_value:    entry_price * qty (for longs) or exit_price * qty (for shorts)
        sell_value:   exit_price * qty (for longs) or entry_price * qty (for shorts)
        product:      "MIS" (intraday) or "CNC" (delivery)
        slippage_pct: optional slippage percentage (0.0004 = 0.04%)

    Returns:
        dict with individual components and total
    """
    turnover = buy_value + sell_value
    txn_chg = turnover * 0.0000297
    sebi = turnover * 0.000001

    if product == "CNC":
        brok = 0.0
        stt = turnover * 0.001          # 0.1% both sides for delivery
        stamp = buy_value * 0.00015     # 0.015% on buy side (delivery)
        dp_charge = 15.93               # Rs.13.50 + 18% GST
    else:
        brok = min(buy_value * 0.0003, 20.0) + min(sell_value * 0.0003, 20.0)
        stt = sell_value * 0.00025      # 0.025% sell side only (intraday)
        stamp = buy_value * 0.00003     # 0.003% on buy side (intraday)
        dp_charge = 0.0

    gst = (brok + txn_chg + sebi) * 0.18
    slippage = turnover * slippage_pct

    total = brok + stt + txn_chg + sebi + stamp + gst + dp_charge + slippage

    return {
        "brokerage": round(brok, 2),
        "stt": round(stt, 2),
        "txn_charges": round(txn_chg, 2),
        "sebi_fee": round(sebi, 2),
        "stamp_duty": round(stamp, 2),
        "gst": round(gst, 2),
        "dp_charge": round(dp_charge, 2),
        "slippage": round(slippage, 2),
        "total": round(total, 2),
    }


def compute_charges_from_trade(trade: dict, slippage_pct: float = 0.0) -> float:
    """
    Convenience function: compute total charges from a trade dict.
    Accepts both live engine and simulator trade formats.
    Returns total charge amount (float).
    """
    entry = trade.get("entry_price", trade.get("entry", 0))
    exit_p = trade.get("full_exit_price", trade.get("exit", 0))
    qty = trade.get("qty", 0)
    if not entry or not exit_p or not qty:
        return 0.0

    is_short = trade.get("is_short", False)
    if not isinstance(is_short, bool):
        is_short = "SHORT" in str(trade.get("strategy", "")).upper()

    if is_short:
        buy_val, sell_val = exit_p * qty, entry * qty
    else:
        buy_val, sell_val = entry * qty, exit_p * qty

    product = trade.get("product", "MIS")
    result = compute_trade_charges(buy_val, sell_val, product, slippage_pct)
    return result["total"]
