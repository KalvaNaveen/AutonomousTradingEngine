# BNF Engine (Autonomous Swing Trading Engine) - Final Architecture & Documentation

## Overview
The BNF Engine is a fully autonomous, production-ready, localized algorithmic trading system specializing in the Indian stock market (NSE). It strictly executes the **Minervini S3/S4 Breakout Strategy** combined with Mark Minervini's Volatility Contraction Pattern (VCP) and Stage Analysis framework.

The engine connects to Zerodha API (Kite Connect) for live market data and order execution, processes technical and fundamental metrics, manages an autonomous daily life-cycle, strictly enforces daily and sequential risk limits, and issues status/trade alerts directly to Telegram.

---

## Core Features
1. **Fully Autonomous Daily Lifecycle**: The system boots up at 08:30 via Windows NSSM/Systemd, logs into Zerodha (avoiding Playwright timeouts with automatic fallback token capture), initializes SQLite databases, runs market execution, and performs end-of-day maintenance before shutting down safely.
2. **Minervini S3/S4 Specific Execution**: Captures specific "Stage 2" upward trends. It validates SEPA candidates, checks VCP (Volatility Contraction Pattern) structures, and executes SL (Stop-Loss/Buy-Stop) limit orders strictly on breakout levels.
3. **Resilient Data Processing**: Fallbacks in place. The fundamental scanner uses Web Scraping with 3x retry networks, seamlessly downgrading to `yfinance` if the target server restricts the IP.
4. **Master Checklist Gate**: Execution verifies hard constraints: The stock must have an EPS > 0% (S4), an ROE > 15% (S3), and a Relative Strength score >= 70 (`S3_MIN_RS_RATING`).
5. **Dynamic Risk Control**: Hard-coded structural gates restrict trading if a maximum daily sector loss is reached, or after 3 consecutive realized trading losses.
6. **Paper Trading Backtester**: A built-in 21-module E2E integration test loop (`paper_agent.py`) simulating exact historical behaviors without risking real capital to validate strategy permutations.
7. **Partial Fill & Crash Recovery**: Continuous heartbeat state management gracefully reloads partially filled orders or alive positions directly from the local filesystem (`engine_state.db`) if the program forcibly crashes or restarts mid-session.
8. **Intraday S4 Relative Index Checking**: Automatically suppresses S4 long signals if the prospect stock is relatively underperforming the `NIFTY50` from the intraday `day_open` print.

---

## File-by-File Breakdown

### 1. `main.py`
**The Primary Entry Point and Controller**
- **Purpose**: Defines the `TradingEngine` singleton handling the main asynchronous `while True` logic loops.
- **Key Features**: Bootstraps all agents, orchestrates the 09:15-15:30 trading session loop, manages WebSocket health checks, processes system-wide shutdown safely, and pushes the final "EOD Checklist" maintenance reminders to Telegram.

### 2. `config.py`
**The System Configuration Registry**
- **Purpose**: Stores all credentials, constant configurations, and strategy parameters.
- **Key Features**: Houses Telegram bot keys, API endpoints, Minervini thresholds (e.g., `S3_MIN_RS_RATING = 70`, `S4_MIN_EPS = 0`, VCP contraction parameters), blackout dates array, and risk capital limits (`MAX_POSITIONS = 5`).

### 3. `execution_agent.py`
**Order Router & Strategy Validator**
- **Purpose**: Validates buy signals against the `master_checklist` and routes them directly to the broker.
- **Key Features**: Places `ORDER_TYPE_SL` buy-stop limits exactly at the pivot prices. Includes hard gates checking absolute Minimum RS, 5-minute cooldown filters to prevent hyper-active fire loops, and sends success/failure payloads to the `journal.py`.

### 4. `scanner_agent.py`
**Market Intelligence Engine**
- **Purpose**: Scans the predefined universe of tickers to locate Minervini S1/S2/S3/S4 prospects.
- **Key Features**: Runs `scan_s4_leadership()`, verifying 52-week highs, average volume surges, and tracking intraday relative performance against the Nifty-50 (`NIFTY50_TOKEN`). Feeds valid breakout structures directly to the `ExecutionAgent`.

### 5. `fundamental_agent.py`
**Core Fundamentals Screen**
- **Purpose**: Pulls critical fundamental data (EPS, ROE) essential for the Minervini S3 SEPA logic.
- **Key Features**: Operates a robust 3x network request loop against `screener.in`. Integrated immediately with an automated `yfinance` fallback pipeline to completely nullify the threat of HTTP request drops or empty payloads. Returns hard fundamentals allowing/disallowing execution.

### 6. `paper_agent.py`
**Local E2E Simulator & Historical Backtester**
- **Purpose**: To guarantee codebase stability before pushing to live money trading.
- **Key Features**: Triggers 21 localized modular pipeline checks verifying database state, order executions, scraper health, and VCP logic. Also includes `backtest_minervini()` utilizing a simulated `CHUNK_DAYS=90` history fetch and `_simulate_s4_on_history()` logic measuring simulated Stop/Time exits.

### 7. `data_agent.py`
**Broker Network Interface**
- **Purpose**: Main bridge directly to the Zerodha API.
- **Key Features**: Retrieves instrument dumps, fetches real-time LTP quotes, checks day margin bounds, tracks historical bar extraction mapping `KiteConnect` directly to `config.py` symbols. 

### 8. `auto_login.py`
**Zerodha Headless Authentication**
- **Purpose**: Logs into Zerodha without user action using Microsoft Playwright.
- **Key Features**: Automates pushing User ID, Password, and TOTP. Patched specifically to read `request_token` from URL parsing directly in case the final UI payload times out dynamically.

### 9. `daily_cache.py`
**System Performance Optimizer**
- **Purpose**: Retains aggregated data locally to skip repetitive remote calls.
- **Key Features**: Calculates Moving Averages (50/150/200), computes 52-week highs, determines VCP contractions, and builds the numerical Relative Strength (RS) scores for the Scanner.

### 10. `tick_store.py`
**Live WebSocket Queue Management**
- **Purpose**: In-memory repository collecting real-time tick sequences directly from Zerodha WebSocket.
- **Key Features**: Constructs sub-minute pseudo-bars for quick reference, tracks Nifty50/Indices natively for instantaneous benchmark retrieval.

### 11. `market_status_agent.py`
**Broad Market Regime Filter**
- **Purpose**: Implements the overarching Minervini Market Direction logic.
- **Key Features**: Determines if the overall Nifty is in "BULL", "CHOP", "BEAR", or "RALLY_ATTEMPT". Acts as the ultimate global veto layer preventing aggressive capital allocation during "BEAR" or "CHOP" indices.

### 12. `vcp_agent.py`
**Volatility Contraction Pattern Math**
- **Purpose**: Validates tight price footprints defining a Mark Minervini trade.
- **Key Features**: Breaks the chart history into peak/trough percentage contractions, ensuring decreasing volume thresholds across the right side of the base.

### 13. `stage_agent.py`
**Stan Weinstein Stage Rules**
- **Purpose**: Determines stage configuration of stock trends.
- **Key Features**: Identifies "Stage 2" Uptrends confirming 150-day average is strictly above the 200-day average, and the 200-day is sloping upward. Automatically rejects flat-base Stage 1 or downtrend Stage 4 stocks.

### 14. `risk_agent.py` & `fill_monitor.py`
**P&L Risk Control**
- **Purpose**: Stops absolute bleeding and manages complex executions.
- **Key Features**: Tracks `3_CONSECUTIVE_LOSS_SHUTDOWN`, restricts daily P&L boundaries. `fill_monitor.py` reads WebSocket updates determining if a multi-leg order executed completely or exactly how to pyramid/reconcile partial allotments.

### 15. `state_manager.py` & `journal.py`
**Database & Telemetry Storage**
- **Purpose**: Records local persistence across system drops.
- **Key Features**: `state_manager.py` writes active positions (`engine_state.db`) for crash resumption. `journal.py` dumps ledger entries to `journal.db` validating exactly why a trade was pushed or stopped out.

### 16. `paper_broker.py` & `blackout_calendar.py`
**Utility Modules**
- **Purpose**: Specific overrides for simulation/testing limitations and market exclusions.
- **Key Features**: `paper_broker.py` overrides live placement endpoints enabling order intercepting during test simulation. `blackout_calendar.py` lists NSE Holiday closures preventing booting the system blindly during public holidays.
