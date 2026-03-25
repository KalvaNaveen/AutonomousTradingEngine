import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

"""
Paper Trading Agent — 100% Forward Testing Mode.

This script runs the EXACT same BNFEngine as main.py, but forces
PAPER_MODE = True. This means:
1. Live tick data is consumed from Zerodha Kite WebSocket.
2. The scanner and execution layers run identical to live trading.
3. Orders are routed to the local PaperBroker instead of Kite's REST API.
4. Telegram alerts will look exactly the same as live trading.

No unit tests, no fake PnL reports. Just pure, clean forward-testing.
"""

import os
import sys

# 1. Force PAPER_MODE before any other module imports it
os.environ["PAPER_MODE"] = "true"

# 2. Import the main engine
try:
    from main import BNFEngine
except ImportError as e:
    print(f"FATAL: Could not import main engine: {e}")
    sys.exit(1)


if __name__ == "__main__":
    print("============================================================")
    print("  BNF Engine V16 — FORWARD TESTING (PAPER_MODE=ON)")
    print("  Live WebSocket Data | Virtual Orders | Full Strategy Loop")
    print("============================================================\n")
    
    # 3. Start the exact same loop as live
    try:
        engine = BNFEngine()
        engine.run()
    except KeyboardInterrupt:
        print("\n[Paper] Shutting down cleanly...")
        sys.exit(0)
