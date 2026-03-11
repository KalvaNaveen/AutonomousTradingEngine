# Autonomous Trading Engine (BNF Engine v4)

A 100% autonomous trading engine for NSE India, integrating with Zerodha Kite. This engine executes trades based on specific market conditions and predefined strategies, featuring strict built-in risk management rules, state persistence, and automatic blackout calendar management.

## Core Strategies

The engine currently executes the following core setups:

### 1. Strategy 1: EMA Divergence (Swing Trading - CNC)
*   **Focus**: Identifies strong divergence from the 25-period Exponential Moving Average (EMA).
*   **Regime Dependent**: Minimum deviation thresholds dynamically scale based on the broader market regime (e.g., Bull, Normal, Bear Panic). Inactive during 'Chop'.
*   **Confirmation**: Requires confluence with extreme RSI, Bollinger Band touches, and a substantial increase in Relative Volume (RVOL) before triggering a delivery (CNC) buy.

### 2. Strategy 2: Overreaction Bounce (Intraday - MIS)
*   **Focus**: Targets heavy intraday sell-offs for a quick mean-reversion bounce during specific morning/afternoon timing windows.
*   **Confirmation**: Scans for sudden daily drops accompanied by massive RVOL and specific reversal candlestick setups (such as Hammers or Bullish Engulfing patterns) near pivot supports.
*   **Management**: Strictly intraday (MIS) with tight trailing rules, partial exits at distinct targets, and a rigid time-based stop (e.g., auto-exit after 45 minutes of holding).

## Features
*   **Zero-Touch Operation**: Headless Zerodha authentication and token refresh using automated TOTP.
*   **Resilience**: SQLite state management for instant crash-recovery and seamless resumption of active trade monitoring.
*   **Risk Isolation**: Hard-coded constraints on maximum daily loss, per-trade capital risk, and consecutive loss streaks. 
*   **Notifications**: Real-time Telegram alerts for order statuses, force-exits, regime shifts, and end-of-day summaries.
