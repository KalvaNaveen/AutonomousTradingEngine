"""
Test simulator across different market cycles.
Runs 6 periods covering all market regimes, collects strategy-level stats.
"""
import os, sys, json, datetime, subprocess

os.environ["PAPER_MODE"] = "true"

# Market cycle periods (trading days offset from April 2026)
# Max possible offset is ~950 (leaves 260 bars for warmup out of 1246)
CYCLES = {
    # Period 1: Summer 2022 Bear/Chop (Jun-Sep 2022)
    "2022_BEAR_CHOP":     {"days": 60, "offset": 900, "top": 50},
    # Period 2: Adani Crash Volatility (Jan-Feb 2023)
    "2023_ADANI_CRASH":   {"days": 40, "offset": 770, "top": 50},
    # Period 3: 2023 stable bull run
    "2023_STABLE_BULL":   {"days": 100, "offset": 600, "top": 50},
    # Period 4: 2024 election cycle (Apr-Jun 2024)
    "2024_ELECTION_VOL":  {"days": 50, "offset": 450, "top": 50},
    # Period 5: Late 2025 choppy correction
    "2025_LATE_CORRECT":  {"days": 40, "offset": 120, "top": 50},
    # Period 6: Recent 30 days (most relevant for live)
    "2026_RECENT_30D":    {"days": 30, "offset": 0, "top": 50},
}

results = {}

for name, params in CYCLES.items():
    print(f"\n{'='*70}")
    print(f"  CYCLE: {name}")
    print(f"  Days: {params['days']} | Offset: {params['offset']} | Top: {params['top']}")
    print(f"{'='*70}")
    
    cmd = [
        sys.executable, "simulator.py",
        "--days", str(params["days"]),
        "--top", str(params["top"]),
        "--offset", str(params["offset"])
    ]
    
    try:
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            timeout=600,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            encoding='utf-8',
            errors='replace'
        )
        
        output = result.stdout + result.stderr
        
        # Parse key metrics from output
        lines = output.split("\n")
        total_trades = 0
        win_rate = 0.0
        net_pnl = 0.0
        max_dd = 0.0
        strat_breakdown = {}
        
        for line in lines:
            if "Total Trades" in line and ":" in line:
                try: total_trades = int(line.split(":")[1].strip())
                except: pass
            elif "Win Rate" in line and ":" in line:
                try: win_rate = float(line.split(":")[1].strip().replace("%", ""))
                except: pass
            elif "Net PnL" in line and ":" in line:
                try: net_pnl = float(line.split("Rs.")[1].strip().replace(",", ""))
                except: pass
            elif "Max DD" in line and ":" in line:
                try: max_dd = float(line.split(":")[1].strip().replace("%", ""))
                except: pass
            elif "|" in line and "Trades:" in line and "WR:" in line:
                # Strategy breakdown line
                parts = line.strip().split("|")
                if len(parts) >= 3:
                    strat_name = parts[0].strip()
                    try:
                        trades = int(parts[1].split(":")[1].strip())
                        wr = float(parts[2].split(":")[1].strip().replace("%", ""))
                        pnl_str = parts[3].split("Rs.")[1].strip().replace(",", "") if len(parts) > 3 else "0"
                        pnl = float(pnl_str)
                        strat_breakdown[strat_name] = {"trades": trades, "wr": wr, "pnl": pnl}
                    except:
                        pass
        
        results[name] = {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "net_pnl": net_pnl,
            "max_dd": max_dd,
            "strategies": strat_breakdown
        }
        
        print(f"\n  RESULT: {total_trades} trades | WR: {win_rate:.1f}% | PnL: Rs.{net_pnl:,.0f} | Max DD: {max_dd:.1f}%")
        if strat_breakdown:
            for s, d in strat_breakdown.items():
                print(f"    {s}: {d['trades']} trades | WR: {d['wr']:.0f}% | PnL: Rs.{d['pnl']:,.0f}")
        
        if result.returncode != 0:
            print(f"  [ERROR] Return code {result.returncode}")
            # Show last 20 lines of stderr
            err_lines = result.stderr.strip().split("\n")[-20:]
            for l in err_lines:
                print(f"    {l}")
    
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] Simulation took >600s, skipping")
        results[name] = {"error": "timeout"}
    except Exception as e:
        print(f"  [ERROR] {e}")
        results[name] = {"error": str(e)}

# Final summary
print("\n\n" + "="*80)
print("  CROSS-CYCLE SUMMARY")
print("="*80)
print(f"{'Cycle':<25s} {'Trades':>7s} {'WinRate':>8s} {'Net PnL':>12s} {'MaxDD':>7s}")
print("-" * 65)
total_t = 0
total_w = 0
total_pnl = 0
for name, r in results.items():
    if "error" in r:
        print(f"{name:<25s} {'ERROR':>7s}")
        continue
    total_t += r["total_trades"]
    total_w += r["total_trades"] * r["win_rate"] / 100
    total_pnl += r["net_pnl"]
    print(f"{name:<25s} {r['total_trades']:>7d} {r['win_rate']:>7.1f}% Rs.{r['net_pnl']:>10,.0f} {r['max_dd']:>6.1f}%")

if total_t > 0:
    print("-" * 65)
    print(f"{'TOTAL':<25s} {total_t:>7d} {total_w/total_t*100:>7.1f}% Rs.{total_pnl:>10,.0f}")

# Save results to JSON
with open("data/cycle_test_results.json", "w") as f:
    json.dump(results, f, indent=2)
print("\nResults saved to data/cycle_test_results.json")
