import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

"""
Launch the Go trade executor with environment variables from .env file.
Run this instead of running trade_executor.exe directly.

Usage: python start_executor.py
"""
import os
import subprocess
from dotenv import load_dotenv

# Load .env (same file Python engine uses)
load_dotenv()

exe_path = os.path.join(os.path.dirname(__file__), "go_executor", "trade_executor.exe")

if not os.path.exists(exe_path):
    print(f"[Launcher] ERROR: {exe_path} not found. Run 'go build' first.")
    exit(1)

print("[Launcher] Starting Go Trade Executor with Kite credentials from .env...")

# Pass the full environment (including .env vars) to the Go process
subprocess.run(exe_path, env=os.environ)
