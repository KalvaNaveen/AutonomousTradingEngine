"""
api_server.py — FastAPI backend for the BNF Engine V19 Dashboard.

Streams live engine state, agent actions, trade history, and system
health metrics to the React frontend via WebSocket.

Runs as a daemon thread spawned from main.py.
"""

import uvicorn
import asyncio
import json
import sys
import time
import datetime
from collections import deque
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="BNF Engine V19 Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global state ──────────────────────────────────────────────────
engine_ref = None
_boot_time = None   # set when server starts

# Agent activity log — ring buffer of recent actions
_agent_log = deque(maxlen=200)

# News feed — ring buffer from MacroAgent RSS headlines
_news_feed = deque(maxlen=100)

# Connected WS clients
active_connections: list[WebSocket] = []


def log_news_headline(title: str, source: str, symbol: str = "", sentiment: str = "neutral"):
    """Called by MacroAgent to push a real headline to the dashboard feed."""
    from config import now_ist
    _news_feed.appendleft({
        "time": now_ist().strftime("%H:%M"),
        "title": title[:120],
        "source": source,
        "symbol": symbol,
        "sentiment": sentiment,  # 'bullish', 'bearish', 'neutral'
    })



def log_agent_action(agent: str, action: str, detail: str = ""):
    """Call from anywhere in the engine to record an agent action."""
    from config import now_ist

    # Prevent duplicate sequential logs to avoid UI spam
    if _agent_log:
        last = _agent_log[0]
        if last["agent"] == agent and last["action"] == action and last["detail"] == detail[:120]:
            return

    time_str = now_ist().strftime("%H:%M:%S")
    _agent_log.appendleft({
        "time": time_str,
        "agent": agent,
        "action": action,
        "detail": detail[:120],
    })
    
    # Fire and forget a background thread to persist to SQLite (to avoid blocking the main tick)
    import threading
    from core.journal import Journal
    threading.Thread(target=Journal().log_agent_activity, args=(agent, action, detail[:120], time_str), daemon=True).start()


# ── WebSocket endpoint ────────────────────────────────────────────

@app.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            # Keep the connection alive; we only push, never receive meaningful data
            await asyncio.wait_for(websocket.receive_text(), timeout=30)
    except Exception:
        pass  # Covers WebSocketDisconnect, TimeoutError, and network resets
    finally:
        if websocket in active_connections:
            active_connections.remove(websocket)


# ── Background broadcaster ───────────────────────────────────────

async def broadcast_state():
    """Sends full engine snapshot to all connected dashboards every 1s."""
    while True:
        try:
            if active_connections and engine_ref:
                payload = _build_payload()
                msg = json.dumps(payload, default=str)
                stale = []
                for conn in active_connections:
                    try:
                        await conn.send_text(msg)
                    except Exception:
                        stale.append(conn)
                for c in stale:
                    if c in active_connections:
                        active_connections.remove(c)
        except Exception as e:
            print(f"[API] Broadcast error: {e}")

        await asyncio.sleep(0.1)  # 100ms = 10 updates/sec for near-realtime UI


def _build_payload() -> dict:
    """Assembles the full state snapshot from engine internals."""
    from config import now_ist

    eng = engine_ref
    now = now_ist()

    # ── Uptime ────────────────────────────────────────────────────
    uptime_secs = int((now - _boot_time).total_seconds()) if _boot_time else 0
    hours, remainder = divmod(uptime_secs, 3600)
    minutes, secs = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {secs}s"

    # ── Regime ────────────────────────────────────────────────────
    regime = getattr(eng, "regime", "UNKNOWN") if eng else "UNKNOWN"

    # ── PnL ───────────────────────────────────────────────────────
    daily_pnl = 0.0
    if eng and hasattr(eng, "risk"):
        daily_pnl = getattr(eng.risk, "daily_pnl", 0.0)

    # ── Scan count ────────────────────────────────────────────────
    scan_count = getattr(eng, "scan_count", 0) if eng else 0

    # ── Trade limit ───────────────────────────────────────────────
    daily_trades_used = 0
    if eng and hasattr(eng, "scanner"):
        daily_trades_used = getattr(eng.scanner, "_daily_trade_count", 0)

    # ── WebSocket health ──────────────────────────────────────────
    ws_ok = False
    tick_age = -1
    has_tick_store = eng and hasattr(eng, "tick_store") and eng.tick_store is not None
    if has_tick_store:
        ws_ok = eng.tick_store.is_fresh()
        last_tick = getattr(eng.tick_store, "_last_tick_at", None)
        if last_tick:
            tick_age = round((now - last_tick).total_seconds(), 1)

    # ── Index data for ticker strip ───────────────────────────────
    from config import NIFTY50_TOKEN, INDIA_VIX_TOKEN
    index_data = {"nifty50": None, "banknifty": None, "vix": None}
    if has_tick_store:
        try:
            n50 = eng.tick_store.get_ltp(NIFTY50_TOKEN)
            if n50 and n50 > 0:
                index_data["nifty50"] = round(n50, 2)
            vix = eng.tick_store.get_ltp(INDIA_VIX_TOKEN)
            if vix and vix > 0:
                index_data["vix"] = round(vix, 2)
        except Exception:
            pass

    # ── Active positions & Sector Breakdown (REAL) ────────────────
    positions = []
    sector_pnl = {}
    
    if eng and hasattr(eng, "execution") and eng.execution:
        for oid, trade in eng.execution.active_trades.items():
            token = trade.get("token")
            symbol = trade.get("symbol", "")

            # Fix missing tokens from SQLite recovery
            if not token and symbol and eng.data:
                for t, s in eng.data.UNIVERSE.items():
                    if s == symbol:
                        token = t; trade["token"] = t; break

            ep = trade.get("entry_price", 0.0)
            qty = trade.get("qty", 0)
            is_short = trade.get("is_short", False)
            ltp = eng.tick_store.get_ltp(token) if eng.tick_store and token else 0.0
            
            pnl = (ltp - ep) * qty if not is_short else (ep - ltp) * qty
            
            # Record for Live Floor
            positions.append({
                "oid": str(oid), "symbol": symbol, "strategy": trade.get("strategy", ""),
                "entry": round(ep, 2), "ltp": round(ltp, 2), "qty": qty,
                "is_short": is_short, "unrealized_pnl": round(pnl, 2),
                "target": round(trade.get("target_price", 0), 2),
                "stop": round(trade.get("stop_price", 0), 2),
                "entry_time": str(trade.get("entry_time", "")),
            })

            # Record for Cyber Pulse
            if hasattr(eng, "data"):
                sect = eng.data.SYMBOL_TO_SECTOR.get(symbol, "OTHER")
                sector_pnl[sect] = sector_pnl.get(sect, 0.0) + pnl



    # ── Agent statuses ────────────────────────────────────────────
    agents = []

    # DataAgent
    data_ok = eng and hasattr(eng, "data") and eng.data is not None
    universe_count = len(eng.data.UNIVERSE) if data_ok else 0
    agents.append({
        "name": "DataAgent",
        "status": "active" if data_ok else "offline",
        "detail": f"{universe_count} symbols loaded",
    })

    # ScannerAgent
    scanner_ok = eng and hasattr(eng, "scanner") and eng.scanner is not None
    agents.append({
        "name": "ScannerAgent",
        "status": "active" if scanner_ok else "offline",
        "detail": f"{scan_count} scans | {daily_trades_used} trades today",
    })

    # RiskAgent — only "active" when engine is armed (token_ok), 
    # otherwise "standby" on market closed days
    risk_ok = eng and hasattr(eng, "risk") and eng.risk is not None
    token_ok = getattr(eng, "token_ok", False) if eng else False
    risk_detail = "Standby (market closed)"
    risk_status = "offline"
    if risk_ok:
        if getattr(eng.risk, "engine_stopped", False):
            risk_status = "stopped"
            risk_detail = f"STOPPED: {getattr(eng.risk, 'stop_reason', '?')}"
        elif token_ok:
            risk_status = "active"
            risk_detail = f"PnL: Rs.{daily_pnl:+,.0f}"
    agents.append({
        "name": "RiskAgent",
        "status": risk_status,
        "detail": risk_detail,
    })

    # ExecutionAgent
    exec_ok = eng and hasattr(eng, "execution") and eng.execution is not None
    agents.append({
        "name": "ExecutionAgent",
        "status": "active" if exec_ok else "offline",
        "detail": f"{len(positions)} open positions",
    })

    # TickStore / WebSocket
    if not has_tick_store:
        ts_status = "offline"
        ts_detail = "Not initialized (no market)"
    elif ws_ok:
        ts_status = "active"
        ts_detail = f"Tick age: {tick_age}s"
    else:
        ts_status = "stale"
        ts_detail = f"Tick age: {tick_age}s" if tick_age >= 0 else "No ticks yet"
    agents.append({
        "name": "TickStore (WS)",
        "status": ts_status,
        "detail": ts_detail,
    })

    # AutoLogin
    token_ok = getattr(eng, "token_ok", False) if eng else False
    agents.append({
        "name": "AutoLogin",
        "status": "active" if token_ok else "pending",
        "detail": "Token refreshed" if token_ok else "Awaiting 08:30 refresh",
    })

    # DailyCache
    cache_ok = False
    if eng and hasattr(eng, "daily_cache") and eng.daily_cache:
        cache_ok = eng.daily_cache.is_loaded()
    agents.append({
        "name": "DailyCache",
        "status": "active" if cache_ok else "loading",
        "detail": "260-day OHLCV loaded" if cache_ok else "Not loaded yet",
    })

    # MacroAgent
    macro_ok = eng and hasattr(eng, "macro") and eng.macro is not None and getattr(eng.macro, "_running", False)
    agents.append({
        "name": "MacroAgent",
        "status": "active" if macro_ok else "offline",
        "detail": "RSS Feeds Polling (5s)" if macro_ok else "News scanner not running",
    })


    return {
        "type": "state",
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime": uptime_str,
        "regime": regime,
        "pnl": round(daily_pnl, 2),
        "scan_count": scan_count,
        "daily_trades_used": daily_trades_used,
        "ws_connected": ws_ok,
        "tick_age": tick_age,
        "positions": positions,
        "agents": agents,
        "activity_log": list(_agent_log)[:50],
        "news_feed": list(_news_feed)[:30],
        "sector_pnl": {k: round(v, 2) for k, v in sector_pnl.items()},
        "universe_count": universe_count,
        "index_data": index_data,
    }


# ── REST endpoints ────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(broadcast_state())


@app.get("/api/health")
def health_check():
    if not engine_ref:
        return {"status": "starting"}
    return {
        "status": "running",
        "regime": getattr(engine_ref, "regime", "UNKNOWN"),
    }

@app.get("/api/history/dates")
def get_history_dates():
    from core.journal import Journal
    return Journal().get_available_dates()

@app.get("/api/history/summary/{date_str}")
def get_history_summary(date_str: str):
    from core.journal import Journal
    summary = Journal().get_daily_summary_for_date(date_str)
    return summary or {}

@app.get("/api/history/trades/{date_str}")
def get_history_trades(date_str: str):
    from core.journal import Journal
    return Journal().get_all_trades_for_date(date_str)

@app.get("/api/history/logs/{date_str}")
def get_history_logs(date_str: str):
    from core.journal import Journal
    return Journal().get_logs_for_date(date_str)

@app.websocket("/api/ws/simulator")
async def simulator_stream(websocket: WebSocket, days: int = 30, top: int = 50):
    await websocket.accept()
    try:
        import os
        sim_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "simulator.py")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", sim_path, "--days", str(days), "--top", str(top),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            await websocket.send_text(line.decode('utf-8', errors='replace').rstrip('\r\n'))
        
        await process.wait()
        await websocket.send_text(f"\n[Simulator] Process exited with code {process.returncode}")
        await websocket.close()
    except WebSocketDisconnect:
        if 'process' in locals() and process.returncode is None:
            process.terminate()
            print("[API] Simulator aborted by client disconnect")
    except Exception as e:
        print(f"[API] Simulator streaming error: {e}")
        try:
            await websocket.send_text(f"ERROR: {e}")
            await websocket.close()
        except: pass


# ── STATIC RE-ROUTING (Dashboard UI) ─────────────────────────
import os
from fastapi.staticfiles import StaticFiles

dist_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dashboard", "dist")
if os.path.exists(dist_path):
    app.mount("/", StaticFiles(directory=dist_path, html=True), name="dashboard")
else:
    print(f"[API] WARNING: Dashboard dist folder not found at {dist_path}.")


# ── Entry point (called from main.py thread) ─────────────────────
_uvicorn_server = None

def start_api_server(engine_instance):
    global engine_ref, _boot_time, _uvicorn_server
    from config import now_ist
    engine_ref = engine_instance
    _boot_time = now_ist()
    print("[API] Starting Dashboard FastAPI Server on port 8000...")
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    _uvicorn_server = uvicorn.Server(config)
    _uvicorn_server.run()

def stop_api_server():
    global _uvicorn_server
    if _uvicorn_server:
        print("[API] Initiating Dashboard Server shutdown...")
        _uvicorn_server.should_exit = True
        _uvicorn_server = None
