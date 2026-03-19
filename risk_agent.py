import datetime
import numpy as np
from config import *


class RiskAgent:

    def __init__(self, capital: float):
        self.capital            = capital
        self.daily_pnl          = 0.0
        self.open_positions     = {}
        self.daily_trades       = []
        self.engine_stopped     = False
        self.stop_reason        = ""
        self.consecutive_losses = 0

    def approve_trade(self, signal: dict) -> tuple:
        if self.engine_stopped:
            return False, f"ENGINE_STOPPED: {self.stop_reason}"
        if self.daily_pnl <= -(self.capital * DAILY_LOSS_LIMIT_PCT):
            self.engine_stopped = True
            self.stop_reason    = f"DAILY_LOSS_LIMIT ₹{abs(self.daily_pnl):.0f}"
            return False, self.stop_reason
        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.engine_stopped = True
            self.stop_reason    = f"{MAX_CONSECUTIVE_LOSSES}_CONSECUTIVE_LOSSES"
            return False, self.stop_reason
        if len(self.open_positions) >= MAX_OPEN_POSITIONS:
            return False, f"MAX_{MAX_OPEN_POSITIONS}_POSITIONS"
        if signal["symbol"] in [p["symbol"] for p in self.open_positions.values()]:
            return False, f"DUPLICATE_{signal['symbol']}"
        if signal.get("stop_price", 0) <= 0:
            return False, "NO_STOP_DEFINED"
        if signal["stop_price"] >= signal["entry_price"]:
            return False, "STOP_ABOVE_ENTRY"
        if signal.get("target_price", 0) <= signal["entry_price"]:
            return False, "TARGET_BELOW_ENTRY"
        reward = signal["target_price"] - signal["entry_price"]
        risk   = signal["entry_price"] - signal["stop_price"]
        rr     = reward / risk if risk > 0 else 0
        if rr < 1.5:
            return False, f"RR_{rr:.2f}_BELOW_1.5"
        return True, "APPROVED"

    def calculate_position_size(self, entry: float, stop: float,
                                regime: str = "NORMAL",
                                strategy: str = "") -> int:
        """
        [v13] Volatility-adjusted position sizing.
        Scales risk down in volatile regimes to protect capital.

        Regime scaling:
          BULL       → 100% of MAX_RISK_PER_TRADE_PCT
          NORMAL     → 100%
          VOLATILE   → 70%  (reduce exposure when VIX elevated)
          BEAR_PANIC → 40%  (minimal exposure, only S2 should be trading)
          EXTREME    → 30%  (survival mode)

        Strategy scaling:
          S5_VWAP_ORB (MIS) → 50% of max_position (intraday = smaller size)
        """
        # Regime-based risk scaling
        regime_scale = {
            "BULL":          1.0,
            "NORMAL":        1.0,
            "VOLATILE":      0.70,
            "BEAR_PANIC":    0.40,
            "EXTREME_PANIC": 0.30,
            "CHOP":          0.80,
        }.get(regime, 1.0)

        # Shave 0.2% from the risk budget to absorb STT (0.1% sell-side on
        # delivery), brokerage (~0.03% per leg), and typical limit-order
        # slippage (~0.05%). On a 0.8% S2 stop this is material. On a 7%
        # S1 stop it is negligible — but correct in both cases.
        risk_rs = self.capital * MAX_RISK_PER_TRADE_PCT * regime_scale * 0.998
        rps     = entry - stop
        if rps <= 0:
            return 0
        shares = int(risk_rs / rps)

        # Position cap — smaller for intraday strategies
        if strategy.startswith("S5"):
            pos_cap = MAX_POSITION_PCT * 0.50   # 50% max for MIS intraday
        else:
            pos_cap = MAX_POSITION_PCT

        cap = int((self.capital * pos_cap) / (entry * 1.001))
        return min(shares, cap)

    def register_open(self, oid: str, pos: dict):
        self.open_positions[oid] = pos

    def close_position(self, oid: str, exit_price: float) -> float:
        if oid not in self.open_positions:
            return 0.0
        pos = self.open_positions.pop(oid)

        # Final leg only on remaining shares.
        # pos["qty"] was already updated to remaining_qty after partial fill
        # (set in monitor_positions when partial_filled detected).
        final_leg_pnl = (exit_price - pos["entry_price"]) * pos["qty"]
        self.daily_pnl += final_leg_pnl

        # Total trade PnL = partial profit (already added to daily_pnl) + final leg.
        # Win/loss streak uses NET result across all legs, not final leg alone.
        # Without this: a trade that books +₹500 partial then gets stopped for
        # -₹200 on remaining is a NET WIN but was counted as a loss (streak +1).
        total_trade_pnl = pos.get("realised_pnl", 0.0) + final_leg_pnl
        self.consecutive_losses = (0 if total_trade_pnl > 0
                                   else self.consecutive_losses + 1)

        self.daily_trades.append({
            **pos,
            "exit_price": exit_price,
            "pnl":        total_trade_pnl,
            "exit_time":  now_ist(),
        })
        return total_trade_pnl

    def get_daily_stats(self) -> dict:
        t = self.daily_trades
        if not t:
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                    "gross_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                    "loss_streak": self.consecutive_losses, "capital": self.capital}
        wins   = [x for x in t if x["pnl"] > 0]
        losses = [x for x in t if x["pnl"] <= 0]
        return {
            "total":       len(t),
            "wins":        len(wins),
            "losses":      len(losses),
            "win_rate":    len(wins) / len(t) * 100,
            "gross_pnl":   sum(x["pnl"] for x in t),
            "avg_win":     float(np.mean([x["pnl"] for x in wins])) if wins else 0,
            "avg_loss":    float(np.mean([x["pnl"] for x in losses])) if losses else 0,
            "loss_streak": self.consecutive_losses,
            "capital":     self.capital,
        }
