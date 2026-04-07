import { useState, useEffect, useRef } from 'react';
import './index.css';

function PnlSparkline({ history }) {
  const cleanHistory = (history || []).filter(v => typeof v === 'number' && !isNaN(v));
  if (cleanHistory.length < 2) return null;
  const min = Math.min(...cleanHistory);
  const max = Math.max(...cleanHistory);
  const range = (max - min) || 1;
  const width = 120;
  const height = 40;
  const points = cleanHistory.map((val, i) => {
    const x = (i / (cleanHistory.length - 1)) * width;
    const y = height - ((val - min) / range) * height;
    return `${x},${y}`;
  }).join(' ');

  const color = cleanHistory[cleanHistory.length-1] >= cleanHistory[0] ? 'var(--accent-green)' : 'var(--accent-red)';

  return (
    <svg width={width} height={height} style={{ overflow: 'visible' }}>
      <defs>
        <linearGradient id="glow" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.4" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <polyline
        fill="none"
        stroke={color}
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        points={points}
      />
      <polygon
        fill="url(#glow)"
        points={`${points} ${width},${height} 0,${height}`}
      />
    </svg>
  );
}

function SimulatorFloor() {
  const [days, setDays] = useState(30);
  const [top, setTop] = useState(50);
  const [running, setRunning] = useState(false);
  const [logs, setLogs] = useState([]);
  const termRef = useRef(null);

  const runSimulator = () => {
    setLogs([]);
    setRunning(true);
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/api/ws/simulator?days=${days}&top=${top}`;
    const ws = new WebSocket(wsUrl);
    
    ws.onmessage = (e) => {
      setLogs(prev => [...prev, e.data]);
      if (termRef.current) {
        termRef.current.scrollTop = termRef.current.scrollHeight;
      }
    };
    ws.onclose = () => setRunning(false);
    ws.onerror = (err) => {
      console.error("WS Simulator Error:", err);
      setLogs(prev => [...prev, "ERROR: Connection to simulator failed. Check if backend is running."]);
      setRunning(false);
    };
  };

  return (
    <div className="main-content animate-fade">
      <div className="panel" style={{ flexDirection: 'row', gap: '24px', alignItems: 'center', flexShrink: 0, padding:'14px 20px' }}>
        <h3 style={{ margin: 0, fontSize: '0.9rem', color: 'var(--text-secondary)' }}>SIMULATOR CONFIG</h3>
        <div style={{ display: 'flex', gap: '24px', alignItems: 'center' }}>
          <label className="stat-label" style={{ display: 'flex', alignItems: 'center', gap: '8px', textTransform: 'none' }}>
            Days Back
            <input type="number" className="groww-input" value={days} onChange={e => setDays(Number(e.target.value))} style={{width: '60px', textAlign: 'center'}} />
          </label>
          <label className="stat-label" style={{ display: 'flex', alignItems: 'center', gap: '8px', textTransform: 'none' }}>
            Universe Size
            <input type="number" className="groww-input" value={top} onChange={e => setTop(Number(e.target.value))} style={{width: '60px', textAlign: 'center'}} />
          </label>
        </div>
        <button className="groww-action-btn" onClick={runSimulator} disabled={running} style={{ marginLeft: 'auto', padding: '10px 24px' }}>
          {running ? 'ENGAGED...' : 'INITIALIZE BACKTEST'}
        </button>
      </div>

      <div className="panel" style={{ flex: 1, padding: '20px' }}>
        <div className="terminal-window scrollable" ref={termRef} style={{ flexGrow: 1, height: '100%', minHeight: 0, border: 'none' }}>
          {logs.length === 0 && <div style={{opacity: 0.3, fontFamily: 'Inter'}}>System ready. Awaiting simulation parameters...</div>}
          {logs.map((L, i) => <pre key={i}>{L}</pre>)}
        </div>
      </div>
    </div>
  );
}

function TradeCalendar({ dates, selectedDate, onSelect }) {
  const [viewDate, setViewDate] = useState(() => {
    return selectedDate ? new Date(selectedDate) : new Date();
  });
  const [isOpen, setIsOpen] = useState(false);
  const calendarRef = useRef(null);

  useEffect(() => {
    if (selectedDate) setViewDate(new Date(selectedDate));
  }, [selectedDate]);

  useEffect(() => {
    const handleClick = (e) => {
      if (calendarRef.current && !calendarRef.current.contains(e.target)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  const year = viewDate.getFullYear();
  const month = viewDate.getMonth();
  const firstDay = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  
  const days = [];
  for (let i = 0; i < firstDay; i++) days.push(null);
  for (let i = 1; i <= daysInMonth; i++) days.push(i);

  const prevMonth = () => setViewDate(new Date(year, month - 1, 1));
  const nextMonth = () => setViewDate(new Date(year, month + 1, 1));

  const monthNames = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

  return (
    <div ref={calendarRef} style={{ position: 'relative' }}>
      <button 
        className="groww-input" 
        style={{ width: '200px', textAlign: 'left', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}
        onClick={() => setIsOpen(!isOpen)}
      >
        <span>{selectedDate || 'Select Date'}</span> <span style={{ opacity: 0.8 }}>📅</span>
      </button>

      {isOpen && (
        <div style={{ position: 'absolute', top: '100%', left: 0, marginTop: '8px', background: '#121212', border: '1px solid #2a2a2a', borderRadius: '8px', padding: '16px', zIndex: 100, width: '290px', boxShadow: '0 8px 32px rgba(0,0,0,0.6)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <button onClick={prevMonth} style={{ background: 'transparent', border: 'none', color: '#fff', cursor: 'pointer', fontSize: '1.2rem', padding: '0 8px' }}>&lsaquo;</button>
            <div style={{ fontWeight: 600, fontSize: '0.95rem' }}>{monthNames[month]} {year}</div>
            <button onClick={nextMonth} style={{ background: 'transparent', border: 'none', color: '#fff', cursor: 'pointer', fontSize: '1.2rem', padding: '0 8px' }}>&rsaquo;</button>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: '4px', textAlign: 'center', fontSize: '0.75rem', color: '#7b7b7b', marginBottom: '8px', fontWeight: 600 }}>
            <div>SU</div><div>MO</div><div>TU</div><div>WE</div><div>TH</div><div>FR</div><div>SA</div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: '4px' }}>
            {days.map((d, i) => {
              if (!d) return <div key={i}></div>;
              const dStr = `${year}-${String(month + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
              const hasData = dates.includes(dStr);
              const isSelected = dStr === selectedDate;
              return (
                <button 
                  key={i}
                  disabled={!hasData}
                  onClick={() => { onSelect(dStr); setIsOpen(false); }}
                  style={{
                    padding: '8px 0', border: 'none', borderRadius: '6px',
                    background: isSelected ? '#1876D8' : hasData ? 'rgba(0, 208, 156, 0.15)' : 'transparent',
                    color: isSelected ? '#fff' : hasData ? '#00D09C' : '#555',
                    cursor: hasData ? 'pointer' : 'not-allowed',
                    fontWeight: isSelected || hasData ? 600 : 400,
                    outline: 'none',
                    fontSize: '0.85rem'
                  }}
                >
                  {d}
                </button>
              );
            })}
          </div>
        </div>
      )}
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
    <div className="main-content animate-fade">
      <div className="groww-panel" style={{ flexDirection: 'row', gap: '20px', alignItems: 'center', flexShrink: 0, padding:'14px 20px' }}>
        <div style={{ display:'flex', alignItems:'center', gap:'12px' }}>
          <h3 style={{ margin: 0, fontSize: '0.9rem', color: 'var(--text-secondary)' }}>TRADE LOGS</h3>
          <TradeCalendar dates={dates} selectedDate={selectedDate} onSelect={setSelectedDate} />
        </div>

        {summary && Object.keys(summary).length > 0 && (
          <div style={{ display: 'flex', gap: '32px', marginLeft: 'auto', alignItems: 'center' }}>
            <div className="flex-col" style={{gap: '2px'}}>
              <div className="stat-label" style={{fontSize: '0.65rem'}}>REGIME</div>
              <div className="text-blue" style={{fontWeight: 700}}>{summary.regime || 'UNKNOWN'}</div>
            </div>
            <div className="flex-col" style={{gap: '2px'}}>
              <div className="stat-label" style={{fontSize: '0.65rem'}}>WIN RATE</div>
              <div style={{fontWeight: 700}}>{summary.win_rate || 0}%</div>
            </div>
            <div className="flex-col" style={{gap: '2px', alignItems: 'flex-end'}}>
              <div className="stat-label" style={{fontSize: '0.65rem'}}>NET P&L</div>
              <div className={(summary.gross_pnl || 0) >= 0 ? 'text-green' : 'text-red'} style={{fontWeight: 700, fontSize: '1.1rem'}}>
                {(summary.gross_pnl || 0) >= 0 ? '+' : ''}₹{Number(summary.gross_pnl || 0).toLocaleString('en-IN')}
                {trades.length > 0 && <span style={{fontSize: '0.75rem', opacity: 0.6, marginLeft: '6px', fontWeight: 400}}>
                  ({(((summary.gross_pnl || 0)/trades.reduce((s, t) => s + (t.qty * t.entry_price), 0)) * 100).toFixed(2)}%)
                </span>}
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="bottom-split" style={{ gap: '12px' }}>
        <div className="panel" style={{ flex: 2, padding: 0 }}>
          <div className="table-container scrollable">
            {!selectedDate ? <p style={{padding: 20, opacity: 0.4}}>Initializing logs...</p> : trades.length === 0 ? <p style={{padding: 20, opacity: 0.4}}>No trades found for {selectedDate}</p> : (
              <table>
                <thead>
                  <tr>
                    <th>ENTRY</th><th>EXIT</th><th>SYMBOL</th><th>STRAT</th><th>QTY</th><th>PRICE-IN</th><th>PRICE-OUT</th><th>REASON</th><th style={{textAlign: 'right'}}>ROI / P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t, i) => (
                    <tr key={i}>
                      <td className="text-muted" style={{fontSize: '0.75rem'}}>{t.entry_time ? t.entry_time.substring(11,16) : '--:--'}</td>
                      <td className="text-muted" style={{fontSize: '0.75rem'}}>{t.exit_time ? t.exit_time.substring(11,16) : '--:--'}</td>
                      <td className="symbol-cell">{t.symbol}</td>
                      <td><span className={`badge ${t.strategy?.includes('SHORT') ? 'short' : 'long'}`}>{t.strategy}</span></td>
                      <td>{t.qty}</td>
                      <td>{t.entry_price?.toFixed(1)}</td>
                      <td>{t.full_exit_price?.toFixed(1)}</td>
                      <td className="text-muted">{t.exit_reason}</td>
                      <td style={{ textAlign: 'right', fontWeight: 600 }} className={(t.gross_pnl || 0) >= 0 ? 'text-green' : 'text-red'}>
                        <div style={{fontSize: '0.9rem'}}> {(t.gross_pnl || 0) >= 0 ? '+' : ''}₹{Math.abs(t.gross_pnl || 0).toFixed(0)}</div>
                        <div style={{fontSize: '0.7rem', opacity: 0.6, fontWeight: 400}}>
                          {(((t.gross_pnl || 0)/(t.qty * t.entry_price)) * 100).toFixed(2)}%
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        <div className="panel" style={{ flex: 1, padding: 0 }}>
          <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border-dim)' }}>
            <h3 style={{ margin: 0, fontSize: '0.85rem', color: 'var(--text-secondary)' }}>AGENT FEED</h3>
          </div>
          <div className="activity-log scrollable" style={{ padding: '12px 16px', flex: 1 }}>
            {logs.length === 0 ? <p style={{opacity: 0.3, padding: 20}}>Empty...</p> : (
              logs.map((entry, i) => (
                <div key={i} className="log-entry">
                  <span className="log-time">{entry.time}</span>
                  <span className="log-agent">{entry.agent}</span>
                  <span className="log-detail" style={{color: 'var(--text-primary)'}}>{entry.action} {entry.detail}</span>
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

function LiveFloor({ state, logEndRef }) {
  const pnlClass = state.pnl >= 0 ? 'text-green' : 'text-red';
  const pnlSign = state.pnl >= 0 ? '+' : '-';

  return (
    <div className="main-content animate-fade">
      <div className="stats-bar">
        <div className="panel stat-card" style={{ position: 'relative', overflow: 'visible', background: 'var(--bg-elevated)', border: '1px solid var(--accent-blue)', boxShadow: '0 0 20px rgba(46, 157, 255, 0.1)' }}>
          <div className="stat-label">Daily Net P&L</div>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div className={`stat-value ${pnlClass}`} style={{ fontSize: '1.4rem' }}>{pnlSign}₹{Math.round(Math.abs(state.pnl)).toLocaleString('en-IN')}</div>
            <PnlSparkline history={state.pnl_history || []} />
          </div>
        </div>
        <div className="panel stat-card">
          <div className="stat-label">Positions</div>
          <div className="stat-value text-blue">{state.positions.length}</div>
        </div>
        <div className="panel stat-card">
          <div className="stat-label">Regime</div>
          <div className="stat-value" style={{color: 'var(--accent-purple)'}}>{state.regime.split('_')[0]}</div>
        </div>
        <div className="panel stat-card">
          <div className="stat-label">Efficiency</div>
          <div className="stat-value" style={{color: 'var(--accent-yellow)'}}>{state.daily_trades_used} Trd</div>
        </div>
        <div className="panel stat-card">
          <div className="stat-label">System Health</div>
          <div className="stat-value" style={{ color: state.ws_connected ? 'var(--accent-green)' : 'var(--accent-yellow)', fontSize: '1rem' }}>
            {state.ws_connected ? 'OPERATIONAL' : 'MARKET CLOSED'}
          </div>
        </div>
      </div>

      <div className="bottom-split">
        <div className="panel" style={{ flex: 2, padding: 0 }}>
          <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border-dim)', display: 'flex', justifyContent: 'space-between' }}>
            <h3 style={{ margin: 0, fontSize: '0.85rem', color: 'var(--text-secondary)' }}>ACTIVE FLOOR</h3>
            <span className="text-muted" style={{fontSize: '0.75rem'}}>{state.universe_count || 0} SYMBOLS LOADED</span>
          </div>
          <div className="table-container scrollable">
            {state.positions.length === 0 ? (
              <div style={{ height: '200px', display: 'flex', alignItems: 'center', justifyContent: 'center', opacity: 0.3 }}>
                Waiting for trading signals...
              </div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>ENTRY</th><th>SYMBOL</th><th>STRAT</th><th>QTY</th><th>AVG PRICE</th><th>LTP</th><th>TARGET</th><th>P&L / ROI</th>
                  </tr>
                </thead>
                <tbody>
                  {state.positions.map((pos, i) => (
                    <tr key={i}>
                      <td className="text-muted" style={{fontSize: '0.75rem'}}>{pos.entry_time ? pos.entry_time.substring(11,16) : '--:--'}</td>
                      <td className="symbol-cell">{pos.symbol}</td>
                      <td><span className={`badge ${pos.is_short ? 'short' : 'long'}`}>{pos.strategy}</span></td>
                      <td>{pos.qty}</td>
                      <td>{pos.entry?.toFixed(1)}</td>
                      <td className="text-blue" style={{ fontWeight: 600 }}>{pos.ltp?.toFixed(1)}</td>
                      <td className="text-green">{pos.target?.toFixed(1)}</td>
                      <td style={{ textAlign: 'right', fontWeight: 600 }} className={pos.unrealized_pnl >= 0 ? 'text-green' : 'text-red'}>
                        <div style={{fontSize: '0.9rem'}}> {pos.unrealized_pnl >= 0 ? '+' : ''}₹{Math.abs(pos.unrealized_pnl).toFixed(0)}</div>
                        <div style={{fontSize: '0.7rem', opacity: 0.6, fontWeight: 400}}>
                          {(((pos.unrealized_pnl || 0)/(pos.qty * pos.entry)) * 100).toFixed(2)}%
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        <div className="panel" style={{ flex: 1, padding: 0 }}>
          <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border-dim)' }}>
            <h3 style={{ margin: 0, fontSize: '0.85rem', color: 'var(--text-secondary)' }}>AGENT FEED</h3>
          </div>
          <div className="activity-log scrollable" style={{ padding: '12px 16px', flex: 1 }}>
            {state.activity_log.length === 0 ? (
              <p style={{opacity: 0.3, padding: 20}}>Awaiting activity...</p>
            ) : (
              state.activity_log.map((entry, i) => (
                <div key={i} className="log-entry">
                  <span className="log-time">{entry.time}</span>
                  <span className="log-agent">{entry.agent}</span>
                  <span className="log-detail" style={{color: 'var(--text-primary)'}}>{entry.action} {entry.detail}</span>
                </div>
              ))
            )}
            <div ref={logEndRef} />
          </div>
        </div>
      </div>
    </div>
  );
}

function NewsFeedFloor({ state }) {
  const feed = state.news_feed || [];
  
  const getGradient = (sentiment) => {
    switch(sentiment) {
      case 'bullish': return 'linear-gradient(135deg, rgba(0, 208, 156, 0.2) 0%, rgba(0, 208, 156, 0.05) 100%)';
      case 'bearish': return 'linear-gradient(135deg, rgba(255, 77, 77, 0.2) 0%, rgba(255, 77, 77, 0.05) 100%)';
      default: return 'linear-gradient(135deg, rgba(255, 255, 255, 0.08) 0%, rgba(255, 255, 255, 0.02) 100%)';
    }
  };

  const getBorderColor = (sentiment) => {
    switch(sentiment) {
      case 'bullish': return 'rgba(0, 208, 156, 0.4)';
      case 'bearish': return 'rgba(255, 77, 77, 0.4)';
      default: return 'rgba(255, 255, 255, 0.1)';
    }
  };

  const getTextColor = (sentiment) => {
    switch(sentiment) {
      case 'bullish': return '#00d09c';
      case 'bearish': return '#ff4d4d';
      default: return '#a0aab5';
    }
  };

  return (
    <div className="main-content animate-fade" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div 
        style={{ 
          padding: '24px 32px', 
          flexShrink: 0, 
          display: 'flex', 
          justifyContent: 'space-between', 
          alignItems: 'center',
          background: 'linear-gradient(90deg, rgba(16,20,24,0.8) 0%, rgba(26,32,38,0.8) 100%)',
          backdropFilter: 'blur(12px)',
          borderBottom: '1px solid rgba(255,255,255,0.05)',
          zIndex: 10
        }}
      >
        <div>
          <h3 style={{ margin: 0, fontSize: '1.5rem', fontWeight: '800', letterSpacing: '-0.02em', color: '#fff' }}>
            Live Market Intelligence
          </h3>
          <p style={{ margin: '6px 0 0 0', fontSize: '0.85rem', color: '#a0aab5', fontWeight: '500' }}>
            Algorithmic sentiment scoring across CNBC, Mint, & ET
          </p>
        </div>
        <div style={{ display: 'flex', gap: '24px', alignItems: 'center' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
             <div style={{ width: '8px', height: '8px', borderRadius: '50%', background: '#00d09c', boxShadow: '0 0 10px #00d09c', animation: 'pulse 2s infinite' }}></div>
             <span style={{ fontSize: '0.8rem', fontWeight: '700', color: '#00d09c', letterSpacing: '0.05em' }}>LIVE</span>
          </div>
          <div style={{ textAlign: 'right', background: 'rgba(255,255,255,0.03)', padding: '8px 16px', borderRadius: '12px', border: '1px solid rgba(255,255,255,0.05)' }}>
            <div style={{ fontSize: '0.7rem', textTransform: 'uppercase', color: '#a0aab5', fontWeight: '800', letterSpacing: '0.05em' }}>Headlines Scanned</div>
            <div style={{ fontSize: '1.2rem', fontWeight: '700', color: '#fff', marginTop: '2px' }}>{feed.length}</div>
          </div>
        </div>
      </div>

      <div style={{ flex: 1, padding: '32px', overflowY: 'auto' }}>
        {feed.length === 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', opacity: 0.5 }}>
            <div style={{ fontSize: '3rem', marginBottom: '16px', filter: 'grayscale(1)' }}>📡</div>
            <h4 style={{ fontSize: '1.2rem', margin: 0, color: '#fff' }}>Listening to the market...</h4>
            <div style={{ fontSize: '0.9rem', marginTop: '8px', color: '#a0aab5' }}>Waiting for relevant news catalysts to cross the wire.</div>
          </div>
        ) : (
          <div style={{ 
            display: 'grid', 
            gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', 
            gap: '24px' 
          }}>
            {feed.map((item, i) => (
              <a 
                key={i} 
                href={item.link || '#'} 
                target={item.link ? "_blank" : "_self"}
                rel="noopener noreferrer"
                style={{
                  display: 'flex',
                  flexDirection: 'column',
                  padding: '24px',
                  textDecoration: 'none',
                  background: item.image 
                              ? `linear-gradient(135deg, rgba(16,20,24,0.9) 0%, rgba(16,20,24,0.4) 100%), url(${item.image}) center/cover no-repeat`
                              : getGradient(item.sentiment),
                  backdropFilter: 'blur(16px)',
                  borderRadius: '16px',
                  border: `1px solid ${getBorderColor(item.sentiment)}`,
                  boxShadow: '0 8px 32px rgba(0,0,0,0.1)',
                  transition: 'all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275)',
                  cursor: item.link ? 'pointer' : 'default',
                  position: 'relative',
                  overflow: 'hidden'
                }}
                onMouseOver={(e) => {
                  if (item.link) {
                    e.currentTarget.style.transform = 'translateY(-6px) scale(1.02)';
                    e.currentTarget.style.boxShadow = `0 16px 40px rgba(0,0,0,0.2), 0 0 20px ${getBorderColor(item.sentiment)}`;
                  }
                }}
                onMouseOut={(e) => {
                  if (item.link) {
                    e.currentTarget.style.transform = 'translateY(0) scale(1)';
                    e.currentTarget.style.boxShadow = '0 8px 32px rgba(0,0,0,0.1)';
                  }
                }}
              >
                
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '20px' }}>
                  <div style={{
                    display: 'flex', alignItems: 'center', gap: '6px',
                    padding: '4px 10px', 
                    borderRadius: '20px', 
                    fontSize: '0.7rem',
                    fontWeight: '800', 
                    letterSpacing: '0.05em',
                    color: getTextColor(item.sentiment),
                    backgroundColor: 'rgba(0,0,0,0.2)',
                    border: `1px solid ${getBorderColor(item.sentiment)}`
                  }}>
                    {item.sentiment === 'bullish' ? '🚀 BULLISH' : item.sentiment === 'bearish' ? '⚠️ BEARISH' : '👁️ NEUTRAL'}
                  </div>
                  
                  {item.symbol && (
                    <span style={{ 
                      fontWeight: '800', 
                      color: '#fff', 
                      fontSize: '0.85rem',
                      background: 'rgba(255,255,255,0.1)',
                      padding: '4px 10px',
                      borderRadius: '8px',
                      boxShadow: '0 2px 8px rgba(0,0,0,0.1)'
                    }}>
                      {item.symbol}
                    </span>
                  )}
                </div>
                
                <h4 style={{ 
                  fontSize: '1.1rem', 
                  fontWeight: '600',
                  lineHeight: 1.5, 
                  margin: '0 0 24px 0',
                  color: '#ffffff',
                  flex: 1,
                  textShadow: '0 2px 4px rgba(0,0,0,0.3)'
                }}>
                  {item.title}
                </h4>
                
                <div style={{ 
                  display: 'flex', 
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  fontSize: '0.8rem', 
                  color: '#a0aab5',
                  borderTop: '1px solid rgba(255,255,255,0.1)',
                  paddingTop: '16px'
                }}>
                  <span style={{ fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.05em' }}>{item.source}</span>
                  <span style={{ background: 'rgba(0,0,0,0.3)', padding: '4px 8px', borderRadius: '6px' }}>{item.time}</span>
                </div>
              </a>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function AnalysisFloor() {
  const [dates, setDates] = useState([]);
  const [selectedDate, setSelectedDate] = useState('');
  const [analysis, setAnalysis] = useState(null);
  const [expandedIdx, setExpandedIdx] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetch('/api/history/dates').then(r => r.json()).then(data => {
      setDates(data);
      if (data.length > 0) setSelectedDate(data[0]);
    });
  }, []);

  useEffect(() => {
    if (!selectedDate) return;
    setLoading(true);
    fetch(`/api/analysis/${selectedDate}`)
      .then(r => r.json())
      .then(data => { setAnalysis(data); setLoading(false); setExpandedIdx(null); })
      .catch(() => setLoading(false));
  }, [selectedDate]);

  const gradeColor = (g) => {
    const colors = { A: '#00D09C', B: '#7FD99C', C: '#FFB319', D: '#FF8C42', F: '#FF4D4D' };
    return colors[g] || '#7b7b7b';
  };

  const gradeBg = (g) => {
    const bgs = { A: 'rgba(0,208,156,0.12)', B: 'rgba(127,217,156,0.1)', C: 'rgba(255,179,25,0.1)', D: 'rgba(255,140,66,0.1)', F: 'rgba(255,77,77,0.1)' };
    return bgs[g] || 'transparent';
  };

  const trades = analysis?.trades || [];
  const summary = analysis?.summary || {};
  const lossTrades = trades.filter(t => t.is_loss).sort((a, b) => a.pnl - b.pnl);
  const winTrades = trades.filter(t => t.is_win).sort((a, b) => b.pnl - a.pnl);

  return (
    <div className="main-content animate-fade">
      <div className="groww-panel" style={{ flexDirection: 'row', gap: '20px', alignItems: 'center', flexShrink: 0, padding: '14px 20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <h3 style={{ margin: 0, fontSize: '0.9rem', color: 'var(--text-secondary)' }}>TRADE ANALYSIS</h3>
          <TradeCalendar dates={dates} selectedDate={selectedDate} onSelect={setSelectedDate} />
        </div>
        {summary.total > 0 && (
          <div style={{ display: 'flex', gap: '28px', marginLeft: 'auto', alignItems: 'center' }}>
            <div className="flex-col" style={{ gap: '2px' }}>
              <div className="stat-label" style={{ fontSize: '0.65rem' }}>TRADES</div>
              <div className="text-blue" style={{ fontWeight: 700 }}>{summary.total}</div>
            </div>
            <div className="flex-col" style={{ gap: '2px' }}>
              <div className="stat-label" style={{ fontSize: '0.65rem' }}>WIN RATE</div>
              <div style={{ fontWeight: 700, color: summary.win_rate >= 50 ? 'var(--accent-green)' : 'var(--accent-red)' }}>{summary.win_rate}%</div>
            </div>
            <div className="flex-col" style={{ gap: '2px' }}>
              <div className="stat-label" style={{ fontSize: '0.65rem' }}>GRADES</div>
              <div style={{ display: 'flex', gap: '8px' }}>
                {summary.grades && Object.entries(summary.grades).sort().map(([g, c]) => (
                  <span key={g} style={{ fontWeight: 800, fontSize: '0.8rem', color: gradeColor(g), background: gradeBg(g), padding: '2px 8px', borderRadius: '4px' }}>{g}:{c}</span>
                ))}
              </div>
            </div>
            <div className="flex-col" style={{ gap: '2px', alignItems: 'flex-end' }}>
              <div className="stat-label" style={{ fontSize: '0.65rem' }}>REGIME</div>
              <div style={{ fontWeight: 700, color: 'var(--accent-purple)' }}>{summary.regime || '—'}</div>
            </div>
          </div>
        )}
      </div>

      <div className="bottom-split" style={{ gap: '12px' }}>
        {/* Loss Trades - Deep Analysis */}
        <div className="panel" style={{ flex: 1.2, padding: 0 }}>
          <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border-dim)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h3 style={{ margin: 0, fontSize: '0.85rem', color: 'var(--accent-red)' }}>❌ LOSS ANALYSIS</h3>
            <span className="text-muted" style={{ fontSize: '0.75rem' }}>{lossTrades.length} trades</span>
          </div>
          <div className="scrollable" style={{ padding: '12px 16px', flex: 1 }}>
            {loading && <div style={{ padding: 40, textAlign: 'center', opacity: 0.4 }}>Analyzing trades...</div>}
            {!loading && lossTrades.length === 0 && <div style={{ padding: 40, textAlign: 'center', opacity: 0.3 }}>{selectedDate ? 'No loss trades found' : 'Select a date'}</div>}
            {!loading && lossTrades.map((t, i) => {
              const isExpanded = expandedIdx === `L${i}`;
              const direction = t.strategy?.includes('SHORT') ? 'SHORT' : 'LONG';
              return (
                <div key={`L${i}`} style={{ marginBottom: '10px', borderRadius: '8px', border: '1px solid rgba(255,77,77,0.2)', background: 'rgba(255,77,77,0.04)', overflow: 'hidden' }}>
                  <div
                    onClick={() => setExpandedIdx(isExpanded ? null : `L${i}`)}
                    style={{ padding: '12px 16px', cursor: 'pointer', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                      <span style={{ fontWeight: 800, fontSize: '0.95rem', color: gradeColor(t.grade), background: gradeBg(t.grade), padding: '4px 10px', borderRadius: '6px', minWidth: '32px', textAlign: 'center' }}>{t.grade}</span>
                      <div>
                        <div style={{ fontWeight: 700, fontSize: '0.9rem' }}>{t.symbol}</div>
                        <div style={{ fontSize: '0.7rem', opacity: 0.6 }}>{t.strategy} ({direction})</div>
                      </div>
                    </div>
                    <div style={{ textAlign: 'right' }}>
                      <div className="text-red" style={{ fontWeight: 700 }}>₹{t.pnl?.toFixed(0)}</div>
                      <div style={{ fontSize: '0.7rem', opacity: 0.6 }}>{t.exit_reason}</div>
                    </div>
                  </div>
                  {isExpanded && (
                    <div style={{ padding: '0 16px 16px', borderTop: '1px solid rgba(255,77,77,0.1)' }}>
                      <div style={{ display: 'flex', gap: '20px', padding: '12px 0 8px', fontSize: '0.8rem' }}>
                        <span>Entry: <span className="text-blue" style={{ fontWeight: 600 }}>{t.entry_price?.toFixed(1)}</span></span>
                        <span>Exit: <span style={{ fontWeight: 600 }}>{t.exit_price?.toFixed(1)}</span></span>
                        <span>Qty: <span style={{ fontWeight: 600 }}>{t.qty}</span></span>
                      </div>
                      {t.negatives?.length > 0 && (
                        <div style={{ margin: '8px 0' }}>
                          <div style={{ fontWeight: 700, fontSize: '0.75rem', color: 'var(--accent-red)', marginBottom: '4px' }}>⚠️ ISSUES</div>
                          {t.negatives.map((n, j) => <div key={j} style={{ fontSize: '0.8rem', padding: '3px 0 3px 12px', opacity: 0.85 }}>• {n}</div>)}
                        </div>
                      )}
                      {t.fixes?.length > 0 && (
                        <div style={{ margin: '8px 0' }}>
                          <div style={{ fontWeight: 700, fontSize: '0.75rem', color: 'var(--accent-yellow)', marginBottom: '4px' }}>🔧 FIXES</div>
                          {t.fixes.map((f, j) => <div key={j} style={{ fontSize: '0.8rem', padding: '3px 0 3px 12px', opacity: 0.85 }}>→ {f}</div>)}
                        </div>
                      )}
                      {t.positives?.length > 0 && (
                        <div style={{ margin: '8px 0' }}>
                          <div style={{ fontWeight: 700, fontSize: '0.75rem', color: 'var(--accent-green)', marginBottom: '4px' }}>✅ POSITIVES</div>
                          {t.positives.map((p, j) => <div key={j} style={{ fontSize: '0.8rem', padding: '3px 0 3px 12px', opacity: 0.85 }}>• {p}</div>)}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Win Trades - Summary */}
        <div className="panel" style={{ flex: 0.8, padding: 0 }}>
          <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border-dim)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h3 style={{ margin: 0, fontSize: '0.85rem', color: 'var(--accent-green)' }}>✅ WIN ANALYSIS</h3>
            <span className="text-muted" style={{ fontSize: '0.75rem' }}>{winTrades.length} trades</span>
          </div>
          <div className="scrollable" style={{ padding: '12px 16px', flex: 1 }}>
            {!loading && winTrades.length === 0 && <div style={{ padding: 40, textAlign: 'center', opacity: 0.3 }}>No winning trades</div>}
            {!loading && winTrades.map((t, i) => {
              const isExpanded = expandedIdx === `W${i}`;
              return (
                <div key={`W${i}`} style={{ marginBottom: '10px', borderRadius: '8px', border: '1px solid rgba(0,208,156,0.2)', background: 'rgba(0,208,156,0.04)', overflow: 'hidden' }}>
                  <div
                    onClick={() => setExpandedIdx(isExpanded ? null : `W${i}`)}
                    style={{ padding: '12px 16px', cursor: 'pointer', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                      <span style={{ fontWeight: 800, fontSize: '0.95rem', color: gradeColor(t.grade), background: gradeBg(t.grade), padding: '4px 10px', borderRadius: '6px', minWidth: '32px', textAlign: 'center' }}>{t.grade}</span>
                      <div>
                        <div style={{ fontWeight: 700, fontSize: '0.9rem' }}>{t.symbol}</div>
                        <div style={{ fontSize: '0.7rem', opacity: 0.6 }}>{t.strategy}</div>
                      </div>
                    </div>
                    <div className="text-green" style={{ fontWeight: 700 }}>+₹{t.pnl?.toFixed(0)}</div>
                  </div>
                  {isExpanded && (
                    <div style={{ padding: '0 16px 16px', borderTop: '1px solid rgba(0,208,156,0.1)' }}>
                      {t.positives?.length > 0 && t.positives.map((p, j) => (
                        <div key={j} style={{ fontSize: '0.8rem', padding: '8px 0 3px 0', opacity: 0.85 }}>✅ {p}</div>
                      ))}
                      {t.negatives?.length > 0 && t.negatives.map((n, j) => (
                        <div key={j} style={{ fontSize: '0.8rem', padding: '3px 0 3px 0', opacity: 0.7 }}>⚠️ {n}</div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

function App() {
  const [activeTab, setActiveTab] = useState('live');
  const [state, setState] = useState({
    pnl_history: [],
    news_feed: [],
    sector_pnl: {},
    index_data: { nifty50: null, banknifty: null, vix: null },
    universe_count: 0,
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
      const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      // In dev: Vite proxies /api/* to localhost:8000. In prod: same host serves the API.
      const wsUrl = `${wsProtocol}//${window.location.host}/api/ws`;
      console.log('[WS] Connecting to', wsUrl);
      ws = new WebSocket(wsUrl);
      ws.onopen = () => { console.log('[WS] Connected'); setWsConnected(true); };
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'state') {
            setState(prev => {
              const currentPnl = typeof data.pnl === 'number' ? data.pnl : 0;
              const newHistory = [...(prev.pnl_history || []).slice(-29), currentPnl];
              return { ...prev, ...data, pnl_history: newHistory };
            });
          }
        } catch (e) { console.error("Parse error:", e); }
      };
      ws.onerror = (err) => { console.error('[WS] Error', err); };
      ws.onclose = (e) => {
        console.warn(`[WS] Disconnected (code=${e.code}). Reconnecting in 3s...`);
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

  const agentStatusColor = (s) => s === 'active' ? '#00D09C' : s === 'stopped' ? '#FF4D4D' : s === 'stale' ? '#FFB319' : '#475569';

  return (
    <>
      <div className="sidebar">
        <div className="flex-col" style={{ marginBottom: '8px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <div style={{ width: 40, height: 40, borderRadius: 10, background: 'linear-gradient(135deg, #2E9DFF, #9061F9)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 800, fontSize: 18, color: '#fff' }}>V20</div>
            <div>
              <h2 style={{ letterSpacing: '-0.04em', fontSize: '1.2rem', margin: 0, fontWeight: 700 }}>BNF ENGINE</h2>
              <span className="text-muted" style={{ fontSize: '0.65rem', fontWeight: 600, letterSpacing: '0.05em' }}>QUANTUM TERMINAL</span>
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <div>
            <div className="stat-label" style={{marginBottom: 4}}>Local Time</div>
            <div style={{ fontSize: '1.8rem', fontWeight: 700, fontFamily: 'Outfit', color: 'var(--accent-blue)', letterSpacing: '-0.02em' }}>
              {digitalClock || '--:--:--'}
            </div>
          </div>

          <div className="agent-row">
            <span className="stat-label">Network</span>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <div className={`agent-dot ${wsConnected ? 'status-live' : 'status-offline'}`}></div>
              <span style={{ fontWeight: 600, fontSize: '0.8rem' }}>{wsConnected ? 'Connected' : 'Syncing'}</span>
            </div>
          </div>

          <div className="agent-row">
            <span className="stat-label">Uptime</span>
            <span style={{ fontWeight: 600, fontSize: '0.8rem', opacity: 0.8 }}>{state.uptime}</span>
          </div>
        </div>

        <div style={{ marginTop: 'auto' }}>
          <div className="stat-label" style={{ marginBottom: '12px' }}>Operational Nodes</div>
          <div className="scrollable" style={{ maxHeight: '300px' }}>
            {state.agents.map((agent, i) => (
              <div key={i} className="agent-row" style={{padding: '8px 0'}}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <div className="agent-dot" style={{ background: agentStatusColor(agent.status) }}></div>
                  <span style={{ fontWeight: 600, fontSize: '0.75rem', opacity: 0.9 }}>{agent.name}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="main-content">
        <div className="top-nav">
          <button className={`tab-btn ${activeTab === 'live' ? 'active' : ''}`} onClick={() => setActiveTab('live')}>EXCHANGE</button>
          <button className={`tab-btn ${activeTab === 'history' ? 'active' : ''}`} onClick={() => setActiveTab('history')}>JOURNAL</button>
          <button className={`tab-btn ${activeTab === 'simulator' ? 'active' : ''}`} onClick={() => setActiveTab('simulator')}>QUANT SIM</button>
          <button className={`tab-btn ${activeTab === 'news' ? 'active' : ''}`} onClick={() => setActiveTab('news')}>NEWS FEED</button>
          <button className={`tab-btn ${activeTab === 'analysis' ? 'active' : ''}`} onClick={() => setActiveTab('analysis')}>ANALYSIS</button>
          
          <div className="ticker-wrap" style={{marginLeft:'auto', display:'flex', alignItems:'center', gap:'20px', paddingLeft: '40px'}}>
             <div className="ticker-item"><span className="stat-label">NIFTY 50</span> <span className={state.index_data?.nifty50 ? 'text-green' : 'text-muted'} style={{fontWeight:800}}>{state.index_data?.nifty50 ? state.index_data.nifty50.toLocaleString('en-IN') : '—'}</span></div>
             <div className="ticker-item"><span className="stat-label">BANK NIFTY</span> <span className={state.index_data?.banknifty ? 'text-green' : 'text-muted'} style={{fontWeight:800}}>{state.index_data?.banknifty ? state.index_data.banknifty.toLocaleString('en-IN') : '—'}</span></div>
             <div className="ticker-item"><span className="stat-label">INDIA VIX</span> <span className={state.index_data?.vix ? 'text-blue' : 'text-muted'} style={{fontWeight:800}}>{state.index_data?.vix ?? '—'}</span></div>
          </div>
        </div>

        <div style={{ display: activeTab === 'live' ? 'contents' : 'none' }}>
          <LiveFloor state={state} logEndRef={logEndRef} />
        </div>
        <div style={{ display: activeTab === 'history' ? 'contents' : 'none' }}>
          <HistoryFloor />
        </div>
        <div style={{ display: activeTab === 'simulator' ? 'flex' : 'none', flex: 1, minHeight: 0 }}>
          <SimulatorFloor />
        </div>
        <div style={{ display: activeTab === 'news' ? 'flex' : 'none', flex: 1, minHeight: 0 }}>
          <NewsFeedFloor state={state} />
        </div>
        <div style={{ display: activeTab === 'analysis' ? 'contents' : 'none' }}>
          <AnalysisFloor />
        </div>
      </div>
    </>
  );
}

export default App;
