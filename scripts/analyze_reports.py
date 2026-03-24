import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pdfplumber, os
from collections import defaultdict

dl = r'c:\Users\Admin\Downloads'

files = {
    'BNF_Report_20260320_1530 (14).pdf': 'Report_1800d',
    '5_6197366443504311614.pdf': 'Report_A',
    '5_6197366443504311613.pdf': 'Report_B',
    '5_6197366443504311612.pdf': 'Report_C',
}

for fname, label in files.items():
    path = os.path.join(dl, fname)
    print(f'\n===== {label}: {fname} =====')
    
    strategy_stats = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0, 'trades': 0, 'stop_losses': 0})
    
    with pdfplumber.open(path) as pdf:
        print('Pages:', len(pdf.pages))
        
        p1 = pdf.pages[0].extract_text()
        if p1:
            for line in p1.split('\n')[:15]:
                print('  ' + line.strip())
        
        last = pdf.pages[-1].extract_text()
        if last:
            print('--- Last Page ---')
            for line in last.split('\n'):
                print('  ' + line.strip())
        
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if not row or len(row) < 9:
                        continue
                    strat = str(row[2] or '').strip() if len(row) > 2 else ''
                    result = str(row[8] or '').strip() if len(row) > 8 else ''
                    pnl_str = str(row[7] or '').strip() if len(row) > 7 else ''
                    exit_type = str(row[9] or '').strip() if len(row) > 9 else ''
                    
                    if strat.startswith('S') and ('WIN' in result or 'LOSS' in result):
                        strategy_stats[strat]['trades'] += 1
                        if 'WIN' in result:
                            strategy_stats[strat]['wins'] += 1
                        else:
                            strategy_stats[strat]['losses'] += 1
                        if 'STOP' in exit_type:
                            strategy_stats[strat]['stop_losses'] += 1
                        try:
                            pnl_val = float(pnl_str.replace(',', '').replace('+', ''))
                            strategy_stats[strat]['pnl'] += pnl_val
                        except:
                            pass
        
        print('\n--- Per-Strategy Breakdown ---')
        for strat in sorted(strategy_stats.keys()):
            s = strategy_stats[strat]
            wr = (s['wins'] / s['trades'] * 100) if s['trades'] > 0 else 0
            avg_pnl = s['pnl'] / s['trades'] if s['trades'] > 0 else 0
            print(f"  {strat}: Trades={s['trades']}, W={s['wins']}, L={s['losses']}, "
                  f"WR={wr:.1f}%, PnL={s['pnl']:+,.0f}, AvgPnL={avg_pnl:+,.0f}, "
                  f"StopLosses={s['stop_losses']}")
