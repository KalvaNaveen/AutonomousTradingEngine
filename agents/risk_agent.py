import datetime
import numpy as np
from config import *


class RiskAgent:

    def __init__(self, capital: float, data_agent=None):
        self.total_capital      = capital
        self.active_capital     = capital * 0.80          # 80% for trading (V5/V6 buffer)
        self.risk_reserve       = capital * 0.20          # 20% safety buffer
        self.data               = data_agent
        self.daily_pnl          = 0.0
        self.open_positions     = {}
        self.daily_trades       = []
        self.engine_stopped     = False
        self.stop_reason        = ""
        self.consecutive_losses = 0
        self.weekly_pnl         = 0.0
        self._corr_series_cache = {}
        self._corr_cache        = {}
        self._corr_cache_date   = None

    def approve_trade(self, signal: dict) -> tuple:
        if self.engine_stopped:
            return False, f"ENGINE_STOPPED: {self.stop_reason}"
            
        import os
        import time
        cooldown_file = os.path.join(BASE_DIR, "data", "cooldown.txt")
        if os.path.exists(cooldown_file):
            with open(cooldown_file, "r") as f:
                try:
                    expiry = float(f.read().strip())
                    if time.time() < expiry:
                        self.engine_stopped = True
                        return False, "ENFORCED_3_DAY_COOLDOWN"
                except: pass
                
        WEEKLY_DRAWDOWN_PCT = 0.08
        if self.weekly_pnl <= -(self.active_capital * WEEKLY_DRAWDOWN_PCT):
            self.engine_stopped = True
            self.stop_reason    = "WEEKLY_DRAWDOWN_8%"
            # Write 3-day cooldown
            os.makedirs(os.path.dirname(cooldown_file), exist_ok=True)
            with open(cooldown_file, "w") as f:
                f.write(str(time.time() + 3*24*3600))
            return False, self.stop_reason

        # Sector check
        if self.data and hasattr(self.data, "SYMBOL_TO_SECTOR"):
            new_sym = signal.get("symbol")
            new_sector = self.data.SYMBOL_TO_SECTOR.get(new_sym)
            if new_sector:
                for pos in self.open_positions.values():
                    open_sec = self.data.SYMBOL_TO_SECTOR.get(pos.get("symbol"))
                    if open_sec == new_sector:
                        return False, f"SECTOR_LIMIT_REACHED_{new_sector}"

        # Robust Portfolio Correlation Logic (O(1) Cached)
        sym_new = signal.get("symbol")
        if sym_new and len(self.open_positions) > 0:
            try:
                corr_matrix = self._get_corr_matrix()
                if sym_new in corr_matrix:
                    for open_sym, corr_val in corr_matrix[sym_new].items():
                        if corr_val > 0.85:
                            return False, f"HIGH_CORR_{corr_val:.2f}_WITH_{open_sym}"
            except Exception as e:
                print(f"[Risk] Correlation Check Error for {sym_new}: {e}")

        if self.data:
            vix = self.data.get_india_vix()
            if vix > VIX_EXTREME_STOP:
                return False, f"VIX_EXTREME_{vix}"

        if self.daily_pnl <= -(self.active_capital * DAILY_LOSS_LIMIT_PCT):
            self.engine_stopped = True
            self.stop_reason    = f"DAILY_LOSS_LIMIT Rs.{abs(self.daily_pnl):.0f}"
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
            
        is_short = signal.get("is_short", False)
        if is_short:
            if signal["stop_price"] <= signal["entry_price"]:
                return False, "STOP_BELOW_ENTRY_SHORT"
            if signal.get("target_price", 0) >= signal["entry_price"]:
                return False, "TARGET_ABOVE_ENTRY_SHORT"
            reward = signal["entry_price"] - signal["target_price"]
            risk   = signal["stop_price"] - signal["entry_price"]
        else:
            if signal["stop_price"] >= signal["entry_price"]:
                return False, "STOP_ABOVE_ENTRY"
            if signal.get("target_price", 0) <= signal["entry_price"]:
                return False, "TARGET_BELOW_ENTRY"
            reward = signal["target_price"] - signal["entry_price"]
            risk   = signal["entry_price"] - signal["stop_price"]
            
        rr = reward / risk if risk > 0 else 0
        if rr < 1.5:
            return False, f"RR_{rr:.2f}_BELOW_1.5_STRICT"
        return True, "APPROVED"

    def _get_corr_matrix(self):
        """Cache correlation once per day or on demand."""
        import datetime, pandas as pd
        today_str = datetime.date.today().isoformat()
        if self._corr_cache_date != today_str:
            self._corr_cache = {}  # symbol -> symbol -> corr
            self._corr_series_cache = {}
            self._corr_cache_date = today_str

        if not self.data or not hasattr(self.data, "daily_cache") or not self.data.daily_cache:
            return self._corr_cache

        # Re-using the built _corr_series_cache to dynamically populate the dict without O(N^2) load
        def get_series(sym):
            if sym in self._corr_series_cache:
                return self._corr_series_cache[sym]
            token = next((t for t, s in self.data.UNIVERSE.items() if s == sym), None)
            if token:
                closes = pd.Series(self.data.daily_cache.get_closes(token)[-20:])
                if len(closes) >= 10:
                    self._corr_series_cache[sym] = closes
                    return closes
            return None

        for pos in self.open_positions.values():
            open_sym = pos["symbol"]
            closes_open = get_series(open_sym)
            if closes_open is not None:
                # We need to map universe against open symbols
                for t, sym1 in self.data.UNIVERSE.items():
                    if sym1 not in self._corr_cache:
                        self._corr_cache[sym1] = {}
                    if open_sym not in self._corr_cache[sym1]:
                        closes_new = get_series(sym1)
                        if closes_new is not None:
                            min_l = min(len(closes_new), len(closes_open))
                            corr = closes_new.iloc[-min_l:].corr(closes_open.iloc[-min_l:])
                            if pd.notna(corr):
                                self._corr_cache[sym1][open_sym] = corr
                                
        return self._corr_cache

    def calculate_position_size(self, entry: float, stop: float,
                                regime: str = "NORMAL",
                                strategy: str = "") -> int:
        """
        [V18] Volatility-adjusted position sizing for MIS intraday.
        Risk per trade: 0.75% of capital = Rs.3,750 on Rs.5L.
        Scales down in volatile regimes.

        Regime scaling:
          BULL       -> 100%
          NORMAL     -> 100%
          CHOP       -> 80%
          BEAR_PANIC -> 40%
          EXTREME    -> 30%
        """
        regime_scale = {
            "BULL":          1.0,
            "NORMAL":        1.0,
            "VOLATILE":      0.70,
            "BEAR_PANIC":    0.40,
            "EXTREME_PANIC": 0.30,
            "CHOP":          0.80,
        }.get(regime, 1.0)

        # Dynamic Volatility scaling
        vix = self.data.get_india_vix() if self.data else 15.0
        if vix > 22.0:
            regime_scale *= 0.6  # Reduce risk implicitly at elevated VIX

        # Explicit STT + Brokerage Buffer
        # STT_BUFFER defaults to 0.997 mapped in config.py
        risk_rs = self.active_capital * MAX_RISK_PER_TRADE_PCT * regime_scale * STT_BUFFER
        rps     = abs(entry - stop)
        if rps <= 0:
            return 0
        shares = int(risk_rs / rps)

        # Position cap — all MIS intraday
        cap = int((self.capital * MAX_POSITION_PCT) / (entry * 1.001))
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
        # Without this: a trade that books +Rs.500 partial then gets stopped for
        # -Rs.200 on remaining is a NET WIN but was counted as a loss (streak +1).
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
