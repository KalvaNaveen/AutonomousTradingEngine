"""
60% Win Rate Protocol — Quick Validation Test
Tests the Mean Reversion-only strategy array (S2, S6, S6V, S7) 
across 2 cycles to verify improved win rate after disabling breakout strategies.
"""
import os, sys, subprocess, json

os.environ["PAPER_MODE"] = "true"

CYCLES = {
    # Recent 30 days — most relevant for live deployment
    "2026_RECENT_30D":    {"days": 30, "offset": 0, "top": 50},
    # Late 2025 choppy correction — tests mean reversion in chop
    "2025_LATE_CORRECT":  {"days": 40, "offset": 120, "top": 50},
}

results = {}

for name, params in CYCLES.items():
    print(f"\n{'='*70}")
    print(f"  CYCLE: {name}")
    print(f"  Days: {params['days']} | Offset: {params['offset']} | Top: {params['top']}")
    print(f"  Active Strategies: S2(BB-MR), S6(TrendShort), S6V(VWAP), S7(MR-Long)")
    print(f"  Disabled: S1(MA-Cross), S3(ORB), S8(VolPivot), S9(MTF)")
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
            timeout=900,  # 15 min timeout
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
        daily_results = []
        
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
            # Capture per-strategy breakdown
            elif "trades" in line.lower() and "WR:" in line and "PnL:" in line:
                parts = line.strip().split("|")
                if len(parts) >= 3:
                    strat_name = parts[0].strip().rstrip(":")
                    try:
                        trades = int(parts[0].split("trades")[0].split()[-1])
                        wr_str = [p for p in parts if "WR:" in p][0]
                        wr = float(wr_str.split(":")[1].strip().replace("%", ""))
                        pnl_part = [p for p in parts if "PnL:" in p][0]
                        pnl = float(pnl_part.split("Rs.")[1].strip().replace(",", ""))
                        strat_breakdown[strat_name] = {"trades": trades, "wr": wr, "pnl": pnl}
                    except:
                        pass
            # Capture strategy-specific lines from simulator output
            for strat_tag in ["S2_BB_MEAN_REV", "S6_TREND_SHORT", "S6_VWAP_BAND", "S7_MEAN_REV_LONG"]:
                if strat_tag in line and "trades" in line.lower():
                    strat_breakdown[strat_tag] = line.strip()

        results[name] = {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "net_pnl": net_pnl,
            "max_dd": max_dd,
            "strategies": strat_breakdown,
        }
        
        print(f"\n  RESULT: {total_trades} trades | WR: {win_rate:.1f}% | PnL: Rs.{net_pnl:,.0f} | Max DD: {max_dd:.1f}%")
        if strat_breakdown:
            for s, d in strat_breakdown.items():
                if isinstance(d, dict):
                    print(f"    {s}: {d['trades']} trades | WR: {d['wr']:.0f}% | PnL: Rs.{d['pnl']:,.0f}")
                else:
                    print(f"    {d}")
        
        # Print last 30 lines of output for debug
        print("\n  --- Last 30 lines of sim output ---")
        for l in lines[-30:]:
            if l.strip():
                print(f"    {l}")
        
        if result.returncode != 0:
            print(f"  [ERROR] Return code {result.returncode}")
            err_lines = result.stderr.strip().split("\n")[-20:]
            for l in err_lines:
                print(f"    {l}")
    
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] Simulation took >900s, skipping")
        results[name] = {"error": "timeout"}
    except Exception as e:
        print(f"  [ERROR] {e}")
        results[name] = {"error": str(e)}

# Final summary
print("\n\n" + "="*80)
print("  60% WIN RATE PROTOCOL — VALIDATION SUMMARY")
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
    agg_wr = total_w / total_t * 100
    print(f"{'AGGREGATE':<25s} {total_t:>7d} {agg_wr:>7.1f}% Rs.{total_pnl:>10,.0f}")
    
    if agg_wr >= 60:
        print("\n  ✅ TARGET MET: Aggregate Win Rate >= 60%")
    elif agg_wr >= 55:
        print("\n  ⚠️ CLOSE: Aggregate Win Rate >= 55% but < 60% — needs tuning")
    else:
        print("\n  ❌ BELOW TARGET: Aggregate Win Rate < 55% — strategies need rework")

# Save
with open("data/winrate_test_results.json", "w") as f:
    json.dump(results, f, indent=2)
print("\nResults saved to data/winrate_test_results.json")
