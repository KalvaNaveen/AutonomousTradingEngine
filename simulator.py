"""
simulator.py — BNF Engine V19 Full 9-Strategy Backtest (MIS, 5 years local data)

Uses 100% SQLite local data (historical.db — 250 symbols, daily + 1-min, 5 years).
No live REST calls during simulation — REST firewall enforced.

Active Strategies in Simulator:
  S1  — MA Crossover (9/21 EMA + ADX + 200 EMA)
  S2  — BB Mean Reversion (BB + RSI + VWAP)
  S3  — Opening Range Breakout (ORB 9:15-9:30)
  S4  — SKIPPED (no NFO futures data in local SQLite — add Nifty FUT 1-min data to enable)
  S6  — Trend Short (VWAP + RSI + relative weakness)
  S6V — VWAP Band Mean Reversion (±1.5 SD)
  S7  — Mean Reversion Long (BB + RSI(4) + VWAP)
  S8  — Volume Profile + Pivot Breakout
  S9  — MTF Momentum (Daily 200 EMA + 15-min RSI + MACD)

USAGE:
  python simulator.py --days 30 --top 50
  python simulator.py --days 250 --top 250   (full 1-year, all 250 symbols)
"""

import os
import sys
import json
import time
import datetime
import argparse
import numpy as np
from collections import defaultdict

# Fix Windows console encoding
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

os.environ["PAPER_MODE"] = "true"

from dotenv import load_dotenv
load_dotenv()

from kiteconnect import KiteConnect
from config import KITE_API_KEY, NIFTY50_TOKEN, INDIA_VIX_TOKEN, TOTAL_CAPITAL, today_ist

# Import LIVE components
from agents.data_agent import DataAgent
from agents.risk_agent import RiskAgent
from agents.scanner_agent import ScannerAgent
from agents.execution_agent import ExecutionAgent
from core.journal import Journal
from core.state_manager import StateManager
from storage.tick_store import TickStore

# ═══════════════════════════════════════════════════════════════
#  SimPosition
# ═══════════════════════════════════════════════════════════════
class SimPosition:
    def __init__(self, symbol, strategy, entry_price, stop_price, target_price, qty, entry_time, is_short=False, product="MIS"):
        self.symbol       = symbol
        self.strategy     = strategy
        self.entry_price  = entry_price
        self.stop_price   = stop_price
        self.target_price = target_price
        self.qty          = qty
        self.entry_time   = entry_time
        self.is_short     = is_short
        self.product      = product

# ═══════════════════════════════════════════════════════════════
#  Simulator Core
# ═══════════════════════════════════════════════════════════════
class MultiTimeframeSimulator:

    def __init__(self, days_back=30, top_n=50):
        self.days_back = days_back
        self.top_n = top_n
        self.capital = TOTAL_CAPITAL
        
        # Real Kite Auth
        self.kite = KiteConnect(api_key=KITE_API_KEY)
        access_token = os.getenv("KITE_ACCESS_TOKEN")
        self.kite.set_access_token(access_token)
        
        try:
            self.kite.profile()
        except Exception:
            print("[Simulator] Token expired. Running AutoLogin...")
            try:
                from core.auto_login import AutoLogin
                access_token = AutoLogin().login()
                self.kite.set_access_token(access_token)
                print("[Simulator] AutoLogin successful.")
            except Exception as e:
                print(f"[Simulator] FATAL: AutoLogin failed: {e}")
                sys.exit(1)
        
        # Real Agents
        self.live_data = DataAgent(self.kite)
        self.universe = dict(list(self.live_data.UNIVERSE.items())[:self.top_n])
        
        # Null blackout for simulation
        class NullBlackout:
            def is_blackout(self, date=None): return False
            def refresh(self): pass
        self.scanner = ScannerAgent(self.live_data, NullBlackout())

        # S4: Futures tokens discovered from Kite, data loaded from DB
        # {token: {"symbol": str, "name": str, "expiry": date}}
        self.futures_map = {}

        # Memory storage
        self.hist_daily = {}
        self.hist_minute = {}   # token -> {"YYYY-MM-DD": {"HH:MM": bar}}
        self.nifty_hist = []
        self.vix_hist = []

        # Trade tracking
        self.open_positions = {}
        self.trades = []
        self.capital_curve = [self.capital]
        self.current_capital = self.capital
        self.peak_capital = self.capital
        self.max_dd = 0.0
        self.daily_pnl = []
        self.consecutive_losses = 0

    # ── 1. Data Fetching Phase (from SQLite DB) ───────────────
    def fetch_all_data(self):
        from storage.historical_db import HistoricalDB
        db = HistoricalDB()

        all_tokens = list(self.universe.keys()) + [NIFTY50_TOKEN, INDIA_VIX_TOKEN]
        end = today_ist()
        required_start_min = end - datetime.timedelta(days=self.days_back + 5)

        print(f"\n[Simulator] Loading {len(self.universe)} symbols x {self.days_back} days from SQLite DB...")

        # 1. Discover S4 futures tokens from Kite (FUTURES ONLY — no options)
        try:
            self.futures_map = self.live_data.load_futures_tokens()
            if self.futures_map:
                fut_tokens = list(self.futures_map.keys())
                print(f"[Simulator] S4 futures discovered: "
                      f"{[v['symbol'] for v in self.futures_map.values()]}")
                self.scanner.set_futures_tokens(self.futures_map)
            else:
                print("[Simulator] S4: No futures tokens — arb scan will be skipped.")
        except Exception as e:
            print(f"[Simulator] S4 futures discovery failed: {e}")
            self.futures_map = {}

        # 2. Load Daily Data (stocks + index + futures)
        fut_daily_tokens = list(self.futures_map.keys()) if self.futures_map else []
        all_daily_tokens = all_tokens + fut_daily_tokens
        count = 0
        for token in all_daily_tokens:
            bars = db.get_daily_bars(token)
            self.hist_daily[token] = bars
            if bars:
                count += 1

        self.nifty_hist = self.hist_daily.get(NIFTY50_TOKEN, [])
        self.vix_hist   = self.hist_daily.get(INDIA_VIX_TOKEN, [])

        # 3. Load Minute Data (stocks)
        min_count = 0
        min_start_str = str(required_start_min)
        for token in self.universe:
            bars = db.get_minute_bars(token, start_datetime_str=min_start_str)
            grouped = defaultdict(list)
            for b in bars:
                date_key = str(b['date'])[:10]
                grouped[date_key].append(b)
            if grouped:
                self.hist_minute[token] = dict(grouped)
                min_count += 1

        # 4. Load Minute Data (futures for S4)
        fut_min_count = 0
        for fut_token in self.futures_map.keys():
            bars = db.get_minute_bars(fut_token, start_datetime_str=min_start_str)
            grouped = defaultdict(list)
            for b in bars:
                date_key = str(b['date'])[:10]
                grouped[date_key].append(b)
            if grouped:
                self.hist_minute[fut_token] = dict(grouped)
                fut_min_count += 1

        db.close()
        print(f"[Simulator] Load complete. Daily: {count}/{len(all_daily_tokens)}, "
              f"Min: {min_count}/{len(self.universe)}, "
              f"Fut-Min: {fut_min_count}/{len(self.futures_map)}\n")

    # ── 2. Mock Injection for Original Logic ──────────────────
    def _mock_cache_for_day(self, sim_date_str, day_idx):
        """Tricks the daily_cache into thinking it's standing at 08:45 AM."""
        class MockDailyCache:
            def __init__(self, hist, idx):
                self.hist = hist
                self.idx = idx
            def is_loaded(self): return True
            def get_ema25(self, token):
                bars = self.hist.get(token, [])
                if self.idx < 30 or not bars: return 0.0
                closes = [b["close"] for b in bars[:self.idx]]
                return DataAgent.compute_ema(closes, 25)[-1]
            def get_rsi14(self, token):
                bars = self.hist.get(token, [])
                if self.idx < 20 or not bars: return 50.0
                return (DataAgent.compute_rsi([b["close"] for b in bars[:self.idx]], 14) or [50])[-1]
            def get_bb_lower(self, token):
                bars = self.hist.get(token, [])
                if self.idx < 20 or not bars: return 0.0
                closes = [b["close"] for b in bars[:self.idx]]
                _, _, bb_lo = DataAgent.compute_bollinger(closes, 20, 2.0)
                return bb_lo
            def get_bb_upper(self, token):
                bars = self.hist.get(token, [])
                if self.idx < 20 or not bars: return 0.0
                closes = [b["close"] for b in bars[:self.idx]]
                bb_hi, _, _ = DataAgent.compute_bollinger(closes, 20, 2.0)
                return bb_hi
            def get_avg_turnover_cr(self, token):
                bars = self.hist.get(token, [])
                if self.idx < 20 or not bars: return 0.0
                recent = bars[max(0, self.idx-20):self.idx]
                if not recent: return 0.0
                avg_vol = np.mean([b["volume"] for b in recent])
                return (avg_vol * recent[-1]["close"]) / 1e7
            def get_closes(self, token):
                return [b["close"] for b in self.hist.get(token, [])[:self.idx]]
            def get(self, token):
                bars = self.hist.get(token, [])
                return {"volumes": [b["volume"] for b in bars[:self.idx]]}
            def is_circuit_breaker(self, token, ltp):
                return False
            def get_pivot_support(self, token):
                bars = self.hist.get(token, [])
                return bars[self.idx-1]["low"] if self.idx > 0 and len(bars)>0 else 0.0
            def get_atr(self, token, period=14):
                bars = self.hist.get(token, [])
                if self.idx < 14 or not bars: return 0.0
                return np.mean([b["high"] - b["low"] for b in bars[max(0, self.idx-14):self.idx]])
            def get_avg_daily_vol(self, token):
                bars = self.hist.get(token, [])
                if self.idx < 20 or not bars: return 0.0
                return np.mean([b["volume"] for b in bars[max(0, self.idx-20):self.idx]])
            def _closes(self, token):
                return [b["close"] for b in self.hist.get(token, [])[:self.idx]]
            def get_sma50(self, token):
                c = self._closes(token)
                return float(np.mean(c[-50:])) if len(c) >= 50 else 0.0
            def get_sma150(self, token):
                c = self._closes(token)
                return float(np.mean(c[-150:])) if len(c) >= 150 else 0.0
            def get_sma200(self, token):
                c = self._closes(token)
                return float(np.mean(c[-200:])) if len(c) >= 200 else 0.0
            def get_sma200_up(self, token):
                c = self._closes(token)
                if len(c) < 220: return False
                return float(np.mean(c[-200:])) > float(np.mean(c[-220:-20]))
            def get_high_52w(self, token):
                c = self._closes(token)
                if not c: return 0.0
                return max(c[-260:]) if len(c) >= 260 else max(c)
            def get_low_52w(self, token):
                c = self._closes(token)
                if not c: return 0.0
                return min(c[-260:]) if len(c) >= 260 else min(c)
            def get_highs(self, token):
                return [b["high"] for b in self.hist.get(token, [])[:self.idx]]
            def get_lows(self, token):
                return [b["low"] for b in self.hist.get(token, [])[:self.idx]]
            def refresh_circuit_limits(self, universe):
                pass

        cache = MockDailyCache(self.hist_daily, day_idx)
        self.live_data.daily_cache = cache
        
        # Inject Nifty index into cache
        from config import NIFTY50_TOKEN as N50
        if N50 not in cache.hist and self.nifty_hist:
            cache.hist[N50] = self.nifty_hist

    def _update_mock_tickstore(self, ts_datetime, time_str, all_minute_bars, token, is_new_day=False):
        """Pushes exact minute-level tick data into tick_store."""
        if not self.live_data.tick_store:
            self.live_data.tick_store = TickStore()
            
        if not hasattr(self, '_sim_cum_vol'):
            self._sim_cum_vol = {}
            
        if is_new_day:
            self._sim_cum_vol = {}
            if self.live_data.tick_store:
                self.live_data.tick_store.reset_daily()
        
        if token in all_minute_bars and time_str in all_minute_bars[token]:
            bar = all_minute_bars[token][time_str]
            
            current_cum = self._sim_cum_vol.get(token, 0) + bar["volume"]
            self._sim_cum_vol[token] = current_cum
            
            mock_dt = datetime.datetime.strptime(ts_datetime, "%Y-%m-%d %H:%M:%S")
            from zoneinfo import ZoneInfo
            mock_dt = mock_dt.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            
            tick = {
                "instrument_token": token,
                "last_price": bar["close"],
                "last_quantity": bar["volume"],
                "last_traded_quantity": bar["volume"],
                "average_traded_price": bar["close"],
                "volume": current_cum,
                "volume_traded": current_cum,
                "exchange_timestamp": mock_dt,
                "last_trade_time": mock_dt,
                "change": 0.0,
                "ohlc": {"open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"]},
                "depth": {"buy": [{"quantity": 100, "price": bar["close"], "orders": 1}],
                          "sell": [{"quantity": 100, "price": bar["close"] * 1.001, "orders": 1}]}
            }
            
            import config
            from agents import scanner_agent
            prev_lambda = config.now_ist
            
            config.now_ist = lambda: mock_dt
            scanner_agent.now_ist = lambda: mock_dt
            
            self.live_data.tick_store.on_ticks(None, [tick])
            
            config.now_ist = prev_lambda
            scanner_agent.now_ist = prev_lambda

    # ── 3. Simulation Core ────────────────────────────────────
    def run(self):
        print("="*70)
        print("   BNF Engine V19 -- 9-STRATEGY SIMULATOR (MIS, Local SQLite 5yr)")
        print("   S4 auto-skipped: no NFO futures data in data/historical.db")
        print("="*70)
        
        # ── 1. Data Fetching Phase ────────────────────────────────
        self.fetch_all_data()

        # ── 1.5 REST API Firewall ──────────────────────────────────
        self.kite.quote = lambda x: {}
        self.kite.historical_data = lambda *args: []
        self.live_data.kite = self.kite

        # Find timeline
        ref_bars = self.nifty_hist
        if not ref_bars or len(ref_bars) < 260:
            print("[Simulator] FATAL: Not enough NIFTY history for SMA200 warmup.")
            return

        sim_start = max(260, len(ref_bars) - self.days_back)
        sim_end = len(ref_bars)
        total_executed = 0
        sig_counts = {"S1": 0, "S2": 0, "S3": 0, "S4": 0,
                      "S6": 0, "S6V": 0, "S7": 0, "S8": 0, "S9": 0}
        err_seen = set()
        daily_trade_count = 0

        # Run day by day
        for day_idx in range(sim_start, sim_end):
            today_bar = ref_bars[day_idx]
            today_date = today_bar['date'].date() if hasattr(today_bar['date'], 'date') else today_bar['date'][:10]
            date_str = str(today_date)
            daily_trade_count = 0  # Reset per day
            
            # 1. Setup morning regime & cache
            self._mock_cache_for_day(date_str, day_idx)
            
            # Inject VIX and Nifty into tick store
            v_bar = self.vix_hist[day_idx] if day_idx < len(self.vix_hist) else {"close": 15.0}
            if not self.live_data.tick_store: self.live_data.tick_store = TickStore()
            self.live_data.tick_store.on_ticks(None, [{"instrument_token": INDIA_VIX_TOKEN, "last_price": v_bar["close"]}])
            self.live_data.tick_store.on_ticks(None, [{
                "instrument_token": NIFTY50_TOKEN, 
                "last_price": today_bar["close"],
                "ohlc": {"open": today_bar["open"], "high": today_bar["high"], "low": today_bar["low"], "close": today_bar["close"]}
            }])
            
            regime = self.scanner.detect_regime()

            if regime == "EXTREME_PANIC":
                self.capital_curve.append(self.current_capital)
                continue

            # Load today's minute data
            todays_minutes = {}
            for t in self.universe:
                if t in self.hist_minute and date_str in self.hist_minute[t]:
                    todays_minutes[t] = {str(b['date'])[11:16]: b for b in self.hist_minute[t][date_str]}

            import zoneinfo
            from agents import scanner_agent
            from agents import data_agent
            from storage import tick_store
            import config
            IST = zoneinfo.ZoneInfo("Asia/Kolkata")
            
            try:
                base_dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue

            # Time loop 09:15 to 15:30 in 5-min steps
            for hour in range(9, 16):
                for minute in range(15 if hour == 9 else 0, 60 if hour < 15 else 31, 5):
                    time_str = f"{hour:02d}:{minute:02d}"
                    ts_datetime = f"{date_str} {time_str}:00"
                    
                    # ── Time Travel ──
                    mock_dt = base_dt.replace(hour=hour, minute=minute, tzinfo=IST)
                    mock_now = lambda dt=mock_dt: dt
                    
                    config.now_ist = mock_now
                    scanner_agent.now_ist = mock_now
                    data_agent.now_ist = mock_now
                    tick_store.now_ist = mock_now
                    
                    # Force TickStore freshness
                    if not self.live_data.tick_store:
                        self.live_data.tick_store = tick_store.TickStore()
                    self.live_data.tick_store._ready = True
                    self.live_data.tick_store.is_ready = lambda: True
                    self.live_data.tick_store.is_fresh = lambda: True
                    self.live_data.tick_store.get_ltp_if_fresh = self.live_data.tick_store.get_ltp
                    
                    # Update Mock Tick Store (stocks)
                    is_new = (hour == 9 and minute == 15)
                    for token in self.universe:
                        self._update_mock_tickstore(ts_datetime, time_str, todays_minutes, token, is_new_day=is_new)

                    # Inject futures ticks for S4 (if data available in DB)
                    for fut_token in self.futures_map.keys():
                        self._update_mock_tickstore(ts_datetime, time_str, todays_minutes, fut_token, is_new_day=is_new)
                        
                    # Check stops/targets
                    self._check_stops_targets(date_str, time_str, todays_minutes)

                    # ── Simulate ORB lock for S3 (lock at 09:30) ──────────
                    if hour == 9 and minute == 30:
                        for tok in self.universe:
                            # Compute ORB from today's 9:15-9:29 bars
                            orb_bars = []
                            for mm in range(15, 30, 5):
                                t_str = f"09:{mm:02d}"
                                if t_str in todays_minutes.get(tok, {}):
                                    orb_bars.append(todays_minutes[tok][t_str])
                            if orb_bars and self.live_data.tick_store:
                                orb_high = max(b["high"] for b in orb_bars)
                                orb_low  = min(b["low"]  for b in orb_bars)
                                # Inject into tick_store ORB structure directly
                                with self.live_data.tick_store._lock:
                                    self.live_data.tick_store._orb[tok]["orb_high"] = orb_high
                                    self.live_data.tick_store._orb[tok]["orb_low"]  = orb_low
                                    self.live_data.tick_store._orb[tok]["orb_locked"] = True

                    # ── Scan all strategies ──────────────────────────────
                    if time_str <= "15:00" and daily_trade_count < 5:
                        signals = []

                        try:
                            s1 = self.scanner.scan_s1_ma_cross(regime)
                            if s1: signals.extend(s1[:1]); sig_counts["S1"] += len(s1)
                        except Exception as e:
                            if "S1" not in err_seen: print(f"  [!] S1 err: {e}"); err_seen.add("S1")

                        try:
                            s2 = self.scanner.scan_s2_bb_mean_rev(regime)
                            if s2: signals.extend(s2[:1]); sig_counts["S2"] += len(s2)
                        except Exception as e:
                            if "S2" not in err_seen: print(f"  [!] S2 err: {e}"); err_seen.add("S2")

                        try:
                            s3 = self.scanner.scan_s3_orb()
                            if s3: signals.extend(s3[:1]); sig_counts["S3"] += len(s3)
                        except Exception as e:
                            if "S3" not in err_seen: print(f"  [!] S3 err: {e}"); err_seen.add("S3")

                        # S4 is skipped in simulator (no NFO futures in local SQLite)
                        # scan_s4_arbitrage() auto-returns [] when _futures_tokens is empty

                        try:
                            s6 = self.scanner.scan_s6_trend_short(regime)
                            if s6: signals.extend(s6[:1]); sig_counts["S6"] += len(s6)
                        except Exception as e:
                            if "S6" not in err_seen: print(f"  [!] S6 err: {e}"); err_seen.add("S6")

                        try:
                            s6v = self.scanner.scan_s6_vwap_band(regime)
                            if s6v: signals.extend(s6v[:1]); sig_counts["S6V"] += len(s6v)
                        except Exception as e:
                            if "S6V" not in err_seen: print(f"  [!] S6V err: {e}"); err_seen.add("S6V")

                        try:
                            s7 = self.scanner.scan_s7_mean_rev_long(regime)
                            if s7: signals.extend(s7[:1]); sig_counts["S7"] += len(s7)
                        except Exception as e:
                            if "S7" not in err_seen: print(f"  [!] S7 err: {e}"); err_seen.add("S7")

                        try:
                            s8 = self.scanner.scan_s8_vol_pivot(regime)
                            if s8: signals.extend(s8[:1]); sig_counts["S8"] += len(s8)
                        except Exception as e:
                            if "S8" not in err_seen: print(f"  [!] S8 err: {e}"); err_seen.add("S8")

                        try:
                            s9 = self.scanner.scan_s9_mtf_momentum(regime)
                            if s9: signals.extend(s9[:1]); sig_counts["S9"] += len(s9)
                        except Exception as e:
                            if "S9" not in err_seen: print(f"  [!] S9 err: {e}"); err_seen.add("S9")

                        # Execute!
                        for sig in signals:
                            if daily_trade_count >= 5:
                                break
                            if any(p.symbol == sig["symbol"] for p in self.open_positions.values()):
                                continue
                            if len(self.open_positions) >= 3:
                                continue
                                
                            # Risk math
                            if sig.get("is_short", False):
                                risk_per_share = max(1, sig["stop_price"] - sig["entry_price"])
                            else:
                                risk_per_share = max(1, sig["entry_price"] - sig["stop_price"])
                                
                            qty_risk = int(self.current_capital * 0.0075 / risk_per_share)
                            if qty_risk <= 0: continue
                            
                            oid = f"SIM_{date_str}_{time_str}_{sig['symbol']}"
                            is_short_val = sig.get("is_short", False)
                            self.open_positions[oid] = SimPosition(
                                sig["symbol"], sig["strategy"], sig["entry_price"],
                                sig["stop_price"], sig["target_price"], qty_risk, ts_datetime,
                                is_short=is_short_val, product="MIS"
                            )
                            total_executed += 1
                            daily_trade_count += 1
                            lbl = "Short" if is_short_val else "Long"
                            print(f"  {date_str} {time_str} | {regime:10s} | {sig['strategy']:18s} | {sig['symbol']:10s} | {lbl} Entry: Rs.{sig['entry_price']:.1f}")
                            
                            # S6 cooldown tracking
                            if "S6" in sig["strategy"]:
                                self.scanner._s6_cooldown[sig["symbol"]] = today_date if isinstance(today_date, datetime.date) else datetime.datetime.strptime(str(today_date), "%Y-%m-%d").date()

            # EOD Capital Tracking
            self.capital_curve.append(self.current_capital)
            self.peak_capital = max(self.peak_capital, self.current_capital)
            dd = (self.peak_capital - self.current_capital) / self.peak_capital * 100
            if dd > self.max_dd: self.max_dd = dd

        self._close_all_end_of_sim()
        
        print(f"\nSignals Generated: " +
              " | ".join(f"{k}={v}" for k, v in sig_counts.items() if v > 0))
        print(f"(S4=0 always: no NFO futures data in local SQLite)")
        if err_seen:
            print(f"Errors (first occurrence only): {', '.join(err_seen)}")
        
        self._report_telegram(total_executed)

    def _check_stops_targets(self, date_str, time_str, todays_minutes):
        closed = []
        for oid, pos in self.open_positions.items():
            token = next((t for t, s in self.universe.items() if s == pos.symbol), None)
            if not token or token not in todays_minutes or time_str not in todays_minutes[token]:
                continue
                
            bar = todays_minutes[token][time_str]
            low = bar["low"]
            high = bar["high"]
            close = bar["close"]

            # MIS EOD square-off at 15:15
            if time_str >= "15:15":
                pnl = (pos.entry_price - close) * pos.qty if pos.is_short else (close - pos.entry_price) * pos.qty
                self._record_trade(pos, close, pnl, f"{date_str} {time_str}", "MIS_EOD")
                closed.append(oid)
                continue

            # Dynamic RSI Exits
            if pos.strategy in ["S6_TREND_SHORT", "S7_MEAN_REV_LONG"]:
                cache_closes = self.live_data.daily_cache.get_closes(token)
                if len(cache_closes) > 0:
                    live_closes = cache_closes.copy()
                    live_closes.append(close)
                    from agents import data_agent
                    import config
                    
                    if "S6" in pos.strategy:
                        rsi_live = (data_agent.DataAgent.compute_rsi(live_closes, config.S6_RSI_PERIOD) or [50])[-1]
                        if rsi_live <= config.S6_RSI_EXIT:
                            pnl = (pos.entry_price - close) * pos.qty
                            self._record_trade(pos, close, pnl, f"{date_str} {time_str}", "S6_RSI_EXIT")
                            closed.append(oid)
                            continue
                            
                    if "S7" in pos.strategy:
                        rsi_live = (data_agent.DataAgent.compute_rsi(live_closes, config.S7_RSI_PERIOD) or [50])[-1]
                        if rsi_live >= config.S7_RSI_EXIT:
                            pnl = (close - pos.entry_price) * pos.qty
                            self._record_trade(pos, close, pnl, f"{date_str} {time_str}", "S7_RSI_EXIT")
                            closed.append(oid)
                            continue

            # Standard Stops & Targets
            if not pos.is_short:
                if low <= pos.stop_price:
                    pnl = (pos.stop_price - pos.entry_price) * pos.qty
                    self._record_trade(pos, pos.stop_price, pnl, f"{date_str} {time_str}", "STOP_LOSS")
                    closed.append(oid)
                elif high >= pos.target_price:
                    pnl = (pos.target_price - pos.entry_price) * pos.qty
                    self._record_trade(pos, pos.target_price, pnl, f"{date_str} {time_str}", "TARGET_HIT")
                    closed.append(oid)
            else:  # Short trade
                if high >= pos.stop_price:
                    pnl = (pos.entry_price - pos.stop_price) * pos.qty
                    self._record_trade(pos, pos.stop_price, pnl, f"{date_str} {time_str}", "STOP_LOSS")
                    closed.append(oid)
                elif low <= pos.target_price:
                    pnl = (pos.entry_price - pos.target_price) * pos.qty
                    self._record_trade(pos, pos.target_price, pnl, f"{date_str} {time_str}", "TARGET_HIT")
                    closed.append(oid)
        
        for oid in closed:
            del self.open_positions[oid]

    def _close_all_end_of_sim(self):
        for pos in self.open_positions.values():
            pnl = 0
            self._record_trade(pos, pos.entry_price, pnl, "SIM_END", "FORCED_END")
        self.open_positions.clear()

    def _record_trade(self, pos, exit_p, pnl, exit_t, reason):
        # ── Realistic Simulation Costs ──
        # Slippage: 0.04% per side
        # Brokerage: ₹40 flat round-trip
        # STT: 0.025% on sell side (approx on exit_p)
        turnover = (pos.entry_price + exit_p) * pos.qty
        total_costs = (turnover * 0.0004) + 40.0 + (exit_p * pos.qty * 0.00025)
        pnl -= total_costs

        self.current_capital += pnl
        t = {
            "symbol": pos.symbol, "strategy": pos.strategy,
            "entry": pos.entry_price, "exit": exit_p,
            "qty": pos.qty, "pnl": round(pnl, 2),
            "entry_time": str(pos.entry_time), "exit_time": str(exit_t),
            "reason": reason
        }
        self.trades.append(t)
        
        res = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"
        print(f"  {str(exit_t):16s} | {res:10s} | {pos.strategy:18s} | {pos.symbol:10s} | Qty: {pos.qty} | Exit: Rs.{exit_p:.1f} | PnL: Rs.{pnl:.0f}")

    def _report_telegram(self, executed):
        wins = [t for t in self.trades if t["pnl"] > 0]
        losses = [t for t in self.trades if t["pnl"] < 0]
        pnl = sum(t["pnl"] for t in self.trades)
        wr = len(wins) / max(1, len(wins)+len(losses)) * 100
        
        if self.trades:
            print("\n" + "=" * 70)
            print("  DETAILED TRADE LOG")
            print("=" * 70)
            for t in self.trades:
                res = "WIN" if t["pnl"] > 0 else "LOSS" if t["pnl"] < 0 else "FLAT"
                print(f"  {t['exit_time'][:16]} | {res:4s} | {t['strategy']:18s} | {t['symbol']:10s} | Qty: {t['qty']} | Entry: Rs.{t['entry']:.1f} -> Exit: Rs.{t['exit']:.1f} | PnL: Rs.{t['pnl']:.0f}")

        top_winners = sorted(wins, key=lambda t: t["pnl"], reverse=True)[:5]
        top_losers = sorted(losses, key=lambda t: t["pnl"])[:5]
        
        strat_stats = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0})
        for t in self.trades:
            s = strat_stats[t["strategy"]]
            s["count"] += 1
            s["pnl"] += t["pnl"]
            if t["pnl"] > 0: s["wins"] += 1

        print("\n" + "="*50)
        print("  FINAL SIMULATOR REPORT")
        print("="*50)
        print(f"Total Trades : {len(self.trades)}")
        print(f"Win Rate     : {wr:.1f}%")
        print(f"Net PnL      : Rs.{pnl:,.2f}")
        print(f"Max DD       : {self.max_dd:.2f}%\n")
        
        print("Strategy Breakdown:")
        for strat, stats in strat_stats.items():
            s_wr = stats["wins"] / max(1, stats["count"]) * 100
            print(f"  {strat:18s} | Trades: {stats['count']} | WR: {s_wr:.0f}% | PnL: Rs.{stats['pnl']:,.0f}")
        
        print("\nTop 5 Winners:")
        for t in top_winners: print(f"  {t['symbol']:12s} Rs.+{t['pnl']:.2f} ({t['strategy']})")
        print("Top 5 Losers:")
        for t in top_losers: print(f"  {t['symbol']:12s} Rs.{t['pnl']:.2f} ({t['strategy']})")

        # Send report via Telegram
        try:
            from agents.report_agent import build_simulator_report
            build_simulator_report(
                trades=self.trades,
                capital=self.capital,
                max_dd=self.max_dd,
                days_back=self.days_back,
                top_n=self.top_n,
            )
            print("\n[Simulator] Detailed PDF report sent to Telegram.")
        except Exception as e:
            print(f"\n[Simulator] PDF report failed: {e}")
            try:
                msg = (
                    "*V18 SIMULATOR COMPLETE*\n"
                    f"Days: `{self.days_back}` | Symbols: `{self.top_n}`\n\n"
                    f"Trades: `{len(self.trades)}`\n"
                    f"Win Rate: `{wr:.1f}%`\n"
                    f"Net PnL: `Rs.{pnl:,.0f}`\n"
                    f"Max DD: `{self.max_dd:.1f}%`\n"
                )
                ex = ExecutionAgent(self.kite, RiskAgent(self.capital), Journal(), StateManager())
                ex.alert(msg)
                print("[Simulator] Fallback text report sent to Telegram.")
            except: pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--top", type=int, default=50)
    args = parser.parse_args()
    MultiTimeframeSimulator(args.days, args.top).run()
