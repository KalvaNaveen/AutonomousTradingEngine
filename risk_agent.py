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

    def calculate_position_size(self, entry: float, stop: float) -> int:
        risk_rs = self.capital * MAX_RISK_PER_TRADE_PCT
        rps     = entry - stop
        if rps <= 0:
            return 0
        shares = int(risk_rs / rps)
        cap    = int((self.capital * MAX_POSITION_PCT) / (entry * 1.001))
        return min(shares, cap)

    def register_open(self, oid: str, pos: dict):
        self.open_positions[oid] = pos

    def close_position(self, oid: str, exit_price: float) -> float:
        if oid not in self.open_positions:
            return 0.0
        pos = self.open_positions.pop(oid)
        pnl = (exit_price - pos["entry_price"]) * pos["qty"]
        self.daily_pnl += pnl
        self.consecutive_losses = 0 if pnl > 0 else self.consecutive_losses + 1
        self.daily_trades.append({**pos, "exit_price": exit_price, "pnl": pnl,
                                   "exit_time": datetime.datetime.now()})
        return pnl

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
