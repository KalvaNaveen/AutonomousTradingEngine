# BNF Autonomous Trading Engine (V19) - 1% Club Edition

A 100% autonomous, multi-strategy trading engine for NSE India, leveraging Zerodha Kite. The V19 build transforms the framework into a deeply diverse, multi-timeframe ecosystem designed for rigorous Institutional-style execution. It completely decouples strategy iteration from operational persistence, featuring strict execution logic, correlation checks, volatility-adjusted position sizing, and exact paper-trading parity.

## 🚀 The Top Active Strategies (Multi-Regime Diversified)

The V19 Engine operates heavily decoupled across diversified independent strategies, neutralizing specific regime weaknesses. *(Note: Pairs Trading (S5), Iron Condors (S7), and ML Hybrid (S10) remain in research pending future infra deployment).*

1. **S1: Moving Average Crossover + ADX (Trend)**
   * **Focus:** Captures heavy intraday trends.
   * **Trigger:** Fast EMA (9) crossover against Slow EMA (21) strictly filtered by ADX(14) > 25 for momentum, aligned with the 200 EMA higher-timeframe trend.

2. **S2: BB & RSI Mean Reversion (Chop / Sideways)**
   * **Focus:** Generates alpha in untrending, choppy regimes.
   * **Trigger:** Fades extremes when Price breaches Bollinger Bands (20, 2σ) while RSI(14) signals oversold (<30) or overbought (>70) against Daily VWAP.

3. **S3: Opening Range Breakout (ORB)**
   * **Focus:** Exploits institutional morning liquidity gaps.
   * **Trigger:** Identifies 9:15-9:30 AM High/Low range. Executes exclusively on a 15-minute close outside the threshold alongside heavy RVOL spikes.

4. **S4: Cash-Futures Arbitrage**
   * **Focus:** Ultra-low risk delta-neutral capture.
   * **Trigger:** Scans NIFTY50 / BANKNIFTY indexes vs near-month Futures mispricing > 0.15%.

5. **S6_VWAP: VWAP Band Reversion**
   * **Focus:** Intraday pullback exploitation.
   * **Trigger:** Triggers when price strongly deviates > ±1.5 standard deviations from Intraday VWAP against the macro trend.

6. **S8: Volume Profile + Pivot Point Breakout**
   * **Focus:** Confirms institutional accumulation/distribution.
   * **Trigger:** Trades strictly on volume spikes > 1.5x average piercing major R1/S1 or VAH/VAL zones.

7. **S9: Multi-Timeframe Trend + Momentum Filter**
   * **Focus:** Swing/Intraday confluence precision.
   * **Trigger:** Aligns a 15-minute RSI & MACD signal specifically to the prevailing slope of the Daily 200 EMA context.

---

## 🛡️ Institutional Risk Management & Portfolio Defense

V19 radically overhauls risk layers to guarantee survival and strict drawdown control. Execution will block any signal violating mathematical safeguards.

* **Volatility-Adjusted Position Sizing:** Fixed capital percentage allocation is deprecated. **Risk is dynamically calculated** based exactly on `(Entry - SL)` absolute distance. Capital risk is capped strictly at `0.5%` per trade. Furthermore, position scale shrinks mathematically by 40-60% during `BEAR_PANIC` or elevated `VIX > 22` regimes.
* **Strict 1:1.5 Risk-Reward Minimums:** Embedded natively into the `RiskAgent`. If a parsed strategy yields an `(Entry - Target) / (Entry - SL)` matrix below `1.5` RR, the engine prints a structural block and abandons the query. (e.g. `RR_1.2_BELOW_1.5_STRICT`).
* **Weekly DD Kill-Switch & 3-Day Pause:** Reaching `8%` portfolio Weekly Drawdown instantly freezes the engine. It creates a local `cooldown.txt` time-hash that permanently blocks all REST calls and executions blindly for exactly **72 hours** to counter revenge trading. Daily loss is capped tightly at `1.5%`.
* **Live VIX Extreme Execution Stop:** The scanner continuously queries the `INDIA_VIX_TOKEN`. If VIX shoots violently over `30.0` mid-session, all active entries block at the gateway logic.
* **Portfolio Correlation & Sector Matrix Guard:** The engine blocks clumping! Before committing a trade, `RiskAgent` scans open positions for the specific NSE `Industry` sector to limit dense sector exposure. It explicitly runs a dynamic `Pandas` Pearson correlation matrix over the last 20 daily closes against existing portfolio longs/shorts. Entries correlated strictly above `> 0.85` are aborted with verbose terminal tags (e.g., `HIGH_CORR_0.87_WITH_HDFCBANK`).

---

## ⏱️ Execution & Simulation

* **EOD Time-Based Squelching:** Because MIS executions left open gap deeply overnight, `ExecutionAgent.flatten_all()` is executed faithfully at exactly **15:20 IST** globally against the active orders dict, forcing immediate execution squaring across the portfolio regardless of internal algorithmic state logic.
* **Mathematical Simulator Realistic Fills:** Simulated historical backtests (via `paper_broker.py` `PAPER_MODE=true`) are intentionally penalized. Any virtual limit intercept forcefully injects a real `0.04%` price slippage degradation directly against the simulated fill, and statically debits `₹20.0` flat brokerage per leg directly against realtime PnL availability to strictly ensure backtests reflect net reality.
* **Granular Audit Logs:** Internal Telegram callbacks stream full metadata context over execution failures (e.g., `"Blocked: VIX=27.1 > threshold"`, `"Daily DD Rs.3250"`).
* **Walk-Forward Stress Testing:** `test_strategies.py` allows rapid localized offline testing without Kite rate-limits. Validations rely on sequential arrays and gap-risk simulations across full EOD DB stores.

## ⚙️ How to Run

1. **Environment Config:** Duplicate `.env.example` to `.env`. Add your active `KITE_API_KEY`, secrets, and TOTP base tokens.
2. **Switch Execution Modes:** Define `PAPER_MODE=true` in your `.env` to engage realistic virtualization (reads LIVE websockets, builds purely VIRTUAL P&L). Switch to `false` for active LIVE equity injection.
3. **Execution Execution:**
   ```bash
   pip install -r requirements.txt
   python main.py
   ```
   *(Engine will securely log into Zerodha headless, populate 260-day caches from NSE DBs, fetch today's RBI Blackout Calendar limits, boot the multi-frequency Websocket Ticker, and arm by exactly `09:15 AM IST`).*
