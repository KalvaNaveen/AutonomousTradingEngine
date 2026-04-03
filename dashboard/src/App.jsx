import { useState, useEffect, useRef } from 'react';
import './index.css';

function SimulatorFloor() {
  const [days, setDays] = useState(30);
  const [top, setTop] = useState(50);
  const [running, setRunning] = useState(false);
  const [logs, setLogs] = useState([]);
  const termRef = useRef(null);

  const runSimulator = () => {
    setLogs([]);
    setRunning(true);
    const ws = new WebSocket(`ws://localhost:8000/api/ws/simulator?days=${days}&top=${top}`);
    
    ws.onmessage = (e) => {
      setLogs(prev => [...prev, e.data]);
      if (termRef.current) {
        termRef.current.scrollTop = termRef.current.scrollHeight;
      }
    };
    ws.onclose = () => setRunning(false);
    ws.onerror = () => {
      setLogs(prev => [...prev, "ERROR: Connection to simulator failed."]);
      setRunning(false);
    };
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px', height: '100%' }}>
      <div className="glass-panel" style={{ display: 'flex', gap: '16px', alignItems: 'center', padding: '16px' }}>
        <h3 style={{ margin: 0, width: '150px' }}>Sim Settings</h3>
        <label className="text-sm">Days Back: 
          <input type="number" className="input-field" style={{ marginLeft: '8px' }} value={days} onChange={e => setDays(Number(e.target.value))} />
        </label>
        <label className="text-sm">Top N Symbols:
          <input type="number" className="input-field" style={{ marginLeft: '8px' }} value={top} onChange={e => setTop(Number(e.target.value))} />
        </label>
        <button className="panel-btn" onClick={runSimulator} disabled={running} style={{ marginLeft: 'auto' }}>
          {running ? 'RUNNING...' : '▶ RUN BACKTEST'}
        </button>
      </div>

      <div className="terminal-window" ref={termRef} style={{ flexGrow: 1 }}>
        {logs.length === 0 && <div style={{opacity: 0.5}}>Ready to run simulation over {days} days on {top} symbols...</div>}
        {logs.map((L, i) => <pre key={i}>{L}</pre>)}
      </div>
    </div>
  );
}

function HistoryFloor() {
  const [dates, setDates] = useState([]);
  const [selectedDate, setSelectedDate] = useState('');
  const [summary, setSummary] = useState(null);
  const [trades, setTrades] = useState([]);
  const [logs, setLogs] = useState([]);
  const logRef = useRef(null);

  useEffect(() => {
    fetch('/api/history/dates').then(r => r.json()).then(data => {
      setDates(data);
      if (data.length > 0) setSelectedDate(data[0]);
    });
  }, []);

  useEffect(() => {
    if (!selectedDate) return;
    fetch(`/api/history/summary/${selectedDate}`).then(r => r.json()).then(setSummary);
    fetch(`/api/history/trades/${selectedDate}`).then(r => r.json()).then(setTrades);
    fetch(`/api/history/logs/${selectedDate}`).then(r => r.json()).then(setLogs);
  }, [selectedDate]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px', height: '100%' }}>
      <div className="glass-panel" style={{ display: 'flex', gap: '16px', alignItems: 'center', padding: '16px' }}>
        <h3 style={{ margin: 0 }}>Select Date</h3>
        <select className="input-field" style={{ width: '200px' }} value={selectedDate} onChange={e => setSelectedDate(e.target.value)}>
          {dates.map(d => <option key={d} value={d}>{d}</option>)}
        </select>

        {summary && (
          <div style={{ display: 'flex', gap: '20px', marginLeft: 'auto', alignItems: 'center' }}>
            <span className="text-sm">Regime: <b style={{color: '#3b82f6'}}>{summary.regime}</b></span>
            <span className="text-sm">Trades: <b>{summary.total_trades}</b></span>
            <span className="text-sm">Win Rate: <b>{summary.win_rate}%</b></span>
            <span className="text-sm">Net P&L: <b className={summary.gross_pnl >= 0 ? 'text-green' : 'text-red'}>Rs.{summary.gross_pnl}</b></span>
          </div>
        )}
      </div>

      <div className="bottom-split" style={{ height: '500px' }}>
        <div className="glass-panel scrollable" style={{ flex: 2 }}>
          <h3 style={{ marginBottom: '8px' }}>History Trades Table</h3>
          {trades.length === 0 ? <p className="text-sm" style={{opacity:0.5}}>No trades found for {selectedDate}.</p> : (
            <table>
              <thead>
                <tr>
                  <th>TIME</th><th>SYMBOL</th><th>STRAT</th><th>QTY</th><th>ENTRY</th><th>EXIT</th><th>REASON</th><th>P&L</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t, i) => (
                  <tr key={i}>
                    <td className="text-sm">{t.timestamp ? t.timestamp.substring(11,19) : ''}</td>
                    <td style={{ fontWeight: 600 }}>{t.symbol}</td>
                    <td><span className="badge">{t.strategy}</span></td>
                    <td>{t.qty}</td>
                    <td>{t.entry_price?.toFixed(1)}</td>
                    <td>{t.full_exit_price?.toFixed(1)}</td>
                    <td className="text-sm">{t.exit_reason}</td>
                    <td style={{ fontWeight: 600 }} className={t.gross_pnl >= 0 ? 'text-green' : 'text-red'}>
                      {t.gross_pnl >= 0 ? '+' : ''}{t.gross_pnl?.toFixed(0)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="glass-panel scrollable" style={{ flex: 1 }}>
          <h3 style={{ marginBottom: '8px' }}>Historical Agent Log</h3>
          <div className="activity-log">
            {logs.length === 0 ? <p className="text-sm" style={{opacity:0.5}}>No logs saved for this date.</p> : (
              logs.map((entry, i) => (
                <div key={i} className="log-entry">
                  <span className="log-time">{entry.time}</span>
                  <span className="log-agent">{entry.agent}</span>
                  <span className="log-action">{entry.action}</span>
                  {entry.detail && <span className="log-detail">{entry.detail}</span>}
                </div>
              ))
            )}
            <div ref={logRef} />
          </div>
        </div>
      </div>
    </div>
  );
}

function LiveFloor({ state, pnlClass, pnlSign, logEndRef }) {
  return (
    <>
      <div className="stats-bar" style={{ marginBottom: '20px' }}>
        <div className="stat-card glass-panel">
          <div className="text-xs">Today's P&L</div>
          <div className={`stat-value ${pnlClass}`}>
            {pnlSign}Rs.{Math.abs(state.pnl).toLocaleString('en-IN', { minimumFractionDigits: 0 })}
          </div>
        </div>
        <div className="stat-card glass-panel">
          <div className="text-xs">Active Positions</div>
          <div className="stat-value text-blue">{state.positions.length}</div>
        </div>
        <div className="stat-card glass-panel">
          <div className="text-xs">Scans Today</div>
          <div className="stat-value" style={{ color: '#c084fc' }}>{state.scan_count}</div>
        </div>
        <div className="stat-card glass-panel" title="Maximum trades are now unlimited, constrained only by available margin capital">
          <div className="text-xs">Trades Today</div>
          <div className="stat-value" style={{ color: '#f59e0b' }}>{state.daily_trades_used} <span style={{fontSize: '0.6em', opacity: 0.7}}>/ ∞</span></div>
        </div>
        <div className="stat-card glass-panel">
          <div className="text-xs">Tick Store</div>
          <div className="stat-value" style={{ color: state.ws_connected ? '#10b981' : '#ef4444', fontSize: '1rem' }}>
            {state.ws_connected ? 'LIVE' : 'STALE'}
            <span style={{ fontSize: '0.7rem', color: 'var(--text-secondary)', marginLeft: 6 }}>
              {state.tick_age >= 0 ? `${state.tick_age}s ago` : ''}
            </span>
          </div>
        </div>
      </div>

      <div className="bottom-split">
        <div className="glass-panel scrollable" style={{ flex: 2 }}>
          <h3 style={{ marginBottom: '4px' }}>Live Trading Floor</h3>
          {state.positions.length === 0 ? (
            <div style={{ flexGrow: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', opacity: 0.5 }}>
              <div className="text-sm">No active positions. Scanning {state.scan_count > 0 ? '250' : '...'} symbols</div>
            </div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>SYMBOL</th><th>STRATEGY</th><th>QTY</th><th>ENTRY</th><th>LTP</th><th>TARGET</th><th>STOPLOSS</th><th>P&L</th>
                </tr>
              </thead>
              <tbody>
                {state.positions.map((pos, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 600 }}>{pos.symbol}</td>
                    <td><span className={`badge ${pos.is_short ? 'short' : 'long'}`}>{pos.strategy}</span></td>
                    <td>{pos.qty}</td>
                    <td>Rs.{pos.entry?.toFixed(1)}</td>
                    <td className="text-blue" style={{ fontWeight: 600 }}>Rs.{pos.ltp?.toFixed(1)}</td>
                    <td className="text-green">Rs.{pos.target?.toFixed(1)}</td>
                    <td className="text-red">Rs.{pos.stop?.toFixed(1)}</td>
                    <td style={{ fontWeight: 600 }} className={pos.unrealized_pnl >= 0 ? 'text-green' : 'text-red'}>
                      {pos.unrealized_pnl >= 0 ? '+' : ''}Rs.{pos.unrealized_pnl?.toFixed(0)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="glass-panel scrollable" style={{ flex: 1 }}>
          <h3 style={{ marginBottom: '8px' }}>Agent Activity Log</h3>
          <div className="activity-log">
            {state.activity_log.length === 0 ? (
              <div className="text-sm" style={{ opacity: 0.4, textAlign: 'center', marginTop: '40px' }}>Waiting for engine actions...</div>
            ) : (
              state.activity_log.map((entry, i) => (
                <div key={`${entry.time}-${entry.agent}-${i}`} className="log-entry">
                  <span className="log-time">{entry.time}</span>
                  <span className="log-agent">{entry.agent}</span>
                  <span className="log-action">{entry.action}</span>
                  {entry.detail && <span className="log-detail">{entry.detail}</span>}
                </div>
              ))
            )}
            <div ref={logEndRef} />
          </div>
        </div>
      </div>
    </>
  );
}

function App() {
  const [activeTab, setActiveTab] = useState('live');
  const [state, setState] = useState({
    status: 'connecting', regime: 'UNKNOWN', pnl: 0, uptime: '0h 0m 0s',
    scan_count: 0, daily_trades_used: 0, ws_connected: false,
    tick_age: -1, positions: [], agents: [], activity_log: [], timestamp: ''
  });
  const [wsConnected, setWsConnected] = useState(false);
  const [digitalClock, setDigitalClock] = useState('');
  const logEndRef = useRef(null);

  useEffect(() => {
    const timer = setInterval(() => {
      setDigitalClock(new Date().toLocaleTimeString('en-IN', { hour12: false }));
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    let ws;
    let reconnectTimeout = null;
    const connect = () => {
      ws = new WebSocket('ws://localhost:8000/api/ws');
      ws.onopen = () => setWsConnected(true);
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'state') setState(data);
        } catch (e) { console.error("Parse error:", e); }
      };
      ws.onclose = () => {
        setWsConnected(false);
        reconnectTimeout = setTimeout(connect, 3000);
      };
    };
    connect();
    return () => {
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
      if (ws) ws.close();
    };
  }, []);

  const pnlClass = state.pnl >= 0 ? 'text-green' : 'text-red';
  const pnlSign = state.pnl >= 0 ? '+' : '';
  const regimeColor = {
    'BULL': '#10b981', 'NORMAL': '#3b82f6', 'VOLATILE': '#f59e0b',
    'BEAR_PANIC': '#ef4444', 'EXTREME_PANIC': '#dc2626', 'OFFLINE': '#6b7280'
  }[state.regime] || '#8b5cf6';
  const agentStatusColor = (s) => s === 'active' ? '#10b981' : s === 'stopped' ? '#ef4444' : s === 'stale' ? '#f59e0b' : '#6b7280';

  return (
    <>
      <div className="sidebar glass-panel">
        <div className="flex-col" style={{ marginBottom: '24px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <div style={{ width: 36, height: 36, borderRadius: 10, background: 'linear-gradient(135deg, #3b82f6, #8b5cf6)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: 16 }}>V19</div>
            <div>
              <h2 style={{ letterSpacing: '-0.03em', fontSize: '1.1rem', margin: 0 }}>BNF Engine</h2>
              <span className="text-sm" style={{ fontSize: '0.7rem' }}>God's Eye Dashboard</span>
            </div>
          </div>
        </div>
        <div className="sidebar-section" style={{ borderBottom: 'none', paddingBottom: '0' }}>
          <div className="text-xs text-primary" style={{letterSpacing: '0.1em', fontSize: '0.65rem'}}>LOCAL TIME (IST)</div>
          <div style={{ marginTop: '8px', fontSize: '2rem', fontWeight: 800, fontFamily: 'monospace', color: 'var(--accent-blue)', letterSpacing: '-0.02em', textShadow: '0 0 12px rgba(59, 130, 246, 0.4)' }}>
            {digitalClock || '--:--:--'}
          </div>
        </div>
        <div className="sidebar-section">
          <div className="text-xs">Dashboard Link</div>
          <div style={{ marginTop: '6px', display: 'flex', alignItems: 'center' }}>
            <span className={`status-dot ${wsConnected ? 'live' : 'offline'}`}></span>
            <span style={{ fontWeight: 500, fontSize: '0.85rem' }}>{wsConnected ? 'Connected' : 'Reconnecting...'}</span>
          </div>
        </div>
        <div className="sidebar-section">
          <div className="text-xs">Market Regime</div>
          <div style={{ marginTop: '6px', fontWeight: 700, fontSize: '1.15rem', color: regimeColor }}>{state.regime.replace(/_/g, ' ')}</div>
        </div>
        <div className="sidebar-section">
          <div className="text-xs">Engine Uptime</div>
          <div style={{ marginTop: '6px', fontWeight: 500, fontSize: '0.95rem', fontFamily: 'monospace' }}>{state.uptime}</div>
        </div>
        <div className="sidebar-section" style={{ marginTop: '8px', flexGrow: 1 }}>
          <div className="text-xs" style={{ marginBottom: '10px' }}>System Agents</div>
          <div className="agent-list scrollable">
            {state.agents.map((agent, i) => (
              <div key={i} className="agent-row">
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <div className="agent-dot" style={{ background: agentStatusColor(agent.status) }}></div>
                  <span style={{ fontWeight: 600, fontSize: '0.8rem' }}>{agent.name}</span>
                </div>
                <div className="text-sm" style={{ fontSize: '0.7rem', marginTop: '2px', paddingLeft: '18px' }}>{agent.detail}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="main-content">
        <div className="top-nav">
          <button className={`tab-btn ${activeTab === 'live' ? 'active' : ''}`} onClick={() => setActiveTab('live')}>Live Floor</button>
          <button className={`tab-btn ${activeTab === 'history' ? 'active' : ''}`} onClick={() => setActiveTab('history')}>Trade History</button>
          <button className={`tab-btn ${activeTab === 'simulator' ? 'active' : ''}`} onClick={() => setActiveTab('simulator')}>Simulator</button>
        </div>

        {activeTab === 'live' && <LiveFloor state={state} pnlClass={pnlClass} pnlSign={pnlSign} logEndRef={logEndRef} />}
        {activeTab === 'history' && <HistoryFloor />}
        {activeTab === 'simulator' && <SimulatorFloor />}
      </div>
    </>
  );
}

export default App;
