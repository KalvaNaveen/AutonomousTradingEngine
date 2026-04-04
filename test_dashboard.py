"""
Minimal test runner for Dashboard + MacroAgent on port 8001.
Used to verify UI changes without restarting the main engine.
"""
import sys
import os
import time
import threading

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

# Monkey-patch the port for this test
import uvicorn

# Create a minimal engine-like object that has what the API needs
class MockEngine:
    def __init__(self):
        from core.state_manager import StateManager
        from agents.risk_agent import RiskAgent
        from config import TOTAL_CAPITAL
        
        self.regime = "OFFLINE"
        self.token_ok = False
        self.scan_count = 0
        self.tick_store = None
        self.daily_cache = None
        self.data = None
        self.scanner = None
        self.execution = None
        self.kite = None
        self.state = StateManager()
        self.risk = RiskAgent(TOTAL_CAPITAL)
        self.macro = None
        self._ws_was_fresh = True
        
        # Start MacroAgent standalone
        try:
            from agents.macro_agent import MacroAgent
            self.macro = MacroAgent(data_agent=None)
            self.macro.start()
            print("[Test] MacroAgent started! RSS polling active.")
        except Exception as e:
            print(f"[Test] MacroAgent start failed: {e}")

def main():
    eng = MockEngine()
    
    from core.api_server import app, broadcast_state
    import core.api_server as api_mod
    from config import now_ist
    
    api_mod.engine_ref = eng
    api_mod._boot_time = now_ist()
    
    print("[Test] Starting Dashboard API on port 8001...")
    print("[Test] Open http://localhost:8001 in your browser")
    
    config = uvicorn.Config(app, host="0.0.0.0", port=8001, log_level="warning")
    server = uvicorn.Server(config)
    server.run()

if __name__ == "__main__":
    main()
