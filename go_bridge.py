"""
go_bridge.py — Python-side TCP client for the Go Trade Executor.

Sends JSON trade signals over a persistent TCP connection to
trade_executor.exe running on 127.0.0.1:9559.

Sub-millisecond local IPC. The Go binary then fires
the order to Zerodha Kite's REST API in compiled machine code.

Usage in execution_agent.py:
    from go_bridge import GoBridge
    bridge = GoBridge()
    result = bridge.send_order({
        "action": "BUY", "symbol": "RELIANCE", "exchange": "NSE",
        "qty": 100, "order_type": "MARKET", "product": "MIS",
        "validity": "DAY", "tag": "S5_VWAP"
    })
"""

import json
import socket
import time
import threading


class GoBridge:
    """Persistent TCP connection to the Go trade executor."""

    HOST = "127.0.0.1"
    PORT = 9559
    TIMEOUT = 5.0  # seconds

    def __init__(self):
        self._lock = threading.Lock()
        self._sock = None
        self._connected = False

    def connect(self) -> bool:
        """Establish TCP connection to Go executor. Autostarts if missing."""
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(self.TIMEOUT)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # Disable Nagle
            self._sock.connect((self.HOST, self.PORT))
            self._connected = True
            print(f"[GoBridge] Connected to Go executor at {self.HOST}:{self.PORT}")
            return True
        except Exception as e:
            print(f"[GoBridge] Initial connection failed: {e}. Attempting to start Go executor...")
            try:
                import subprocess, os
                cwd = os.path.dirname(os.path.abspath(__file__))
                exe_path = os.path.join(cwd, "tick_server.exe")
                
                if os.path.exists(exe_path):
                    subprocess.Popen([exe_path], cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    subprocess.Popen(["go", "run", "cmd/server/main.go"], cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                time.sleep(2.5) # Allow 2.5 seconds for the Go server to bind to port 9559
                
                # Try connecting again
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock.settimeout(self.TIMEOUT)
                self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sock.connect((self.HOST, self.PORT))
                self._connected = True
                print(f"[GoBridge] Successfully started and connected to Go executor at {self.HOST}:{self.PORT}")
                return True
            except Exception as e2:
                print(f"[GoBridge] Connection failed after attempting autostart: {e2}")
                self._connected = False
                return False

    def is_connected(self) -> bool:
        return self._connected

    def send_order(self, signal: dict) -> dict:
        """
        Send a trade signal to Go executor and wait for response.
        Returns dict with: status, order_id, message, latency_us
        Falls back to None if connection is down.
        """
        with self._lock:
            if not self._connected:
                if not self.connect():
                    return {"status": "ERROR", "message": "Go executor not reachable"}

            try:
                t0 = time.perf_counter_ns()

                # Send JSON + newline delimiter
                payload = json.dumps(signal).encode("utf-8") + b"\n"
                self._sock.sendall(payload)

                # Read response (newline-delimited JSON)
                data = b""
                while b"\n" not in data:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("Go executor closed connection")
                    data += chunk

                elapsed_us = (time.perf_counter_ns() - t0) // 1000
                result = json.loads(data.decode("utf-8").strip())
                result["bridge_latency_us"] = elapsed_us
                return result

            except Exception as e:
                self._connected = False
                try:
                    self._sock.close()
                except Exception:
                    pass
                return {"status": "ERROR", "message": f"Bridge error: {e}"}

    def close(self):
        """Gracefully close TCP connection."""
        with self._lock:
            self._connected = False
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
