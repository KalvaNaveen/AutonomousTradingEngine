import os
import subprocess
import re
from pathlib import Path

move_map = {
    'agents': ['data_agent.py', 'execution_agent.py', 'risk_agent.py', 'scanner_agent.py',
               'fundamental_agent.py', 'stage_agent.py', 'vcp_agent.py', 'market_status_agent.py',
               'sector_agent.py', 'earnings_agent.py', 'macro_agent.py', 'order_flow_agent.py',
               'report_agent.py'],
    'core': ['state_manager.py', 'journal.py', 'auto_login.py', 'blackout_calendar.py',
             'paper_broker.py', 'go_bridge.py'],
    'storage': ['daily_cache.py', 'tick_store.py', 'historical_db.py'],
    'scripts': ['update_eod_data.py', 'migrate_json_to_db.py', 'analyze_reports.py', 'fill_monitor.py', 'start_executor.py', 'check.py'],
    'tests': ['test_suite.py', 'paper_agent.py']
}

module_to_pkg = {}
for pkg, files in move_map.items():
    for f in files:
        mod = f[:-3]
        module_to_pkg[mod] = pkg

def main():
    print("1. Creating directories...")
    for pkg in move_map.keys():
        os.makedirs(pkg, exist_ok=True)
        init_file = os.path.join(pkg, '__init__.py')
        if not os.path.exists(init_file):
            with open(init_file, 'w') as f:
                f.write("")

    print("2. Moving files...")
    for pkg, files in move_map.items():
        for f in files:
            if os.path.exists(f):
                target = os.path.join(pkg, f)
                # Ensure git mv is used to preserve history
                res = subprocess.run(["git", "mv", f, target], capture_output=True)
                if res.returncode != 0:
                    os.rename(f, target)
                print(f"Moved {f} -> {target}")

    print("3. Refactoring imports across all .py files...")
    all_py_files = list(Path('.').rglob('*.py'))
    
    for py_file in all_py_files:
        if '.venv' in str(py_file) or '__pycache__' in str(py_file) or '.gemini' in str(py_file):
            continue
            
        with open(py_file, 'r', encoding='utf-8') as f:
            content = f.read()

        new_content = content
        
        for mod, pkg in module_to_pkg.items():
            # Match: from data_agent import -> from agents.data_agent import
            from_pattern = r'^(\s*from\s+)' + re.escape(mod) + r'(\s+import\s+)'
            new_content = re.sub(from_pattern, r'\1' + f"{pkg}.{mod}" + r'\2', new_content, flags=re.MULTILINE)
            
            # Match: import data_agent -> from agents import data_agent
            import_pattern = r'^(\s*import\s+)' + re.escape(mod) + r'(\s*(?:as\s+\w+)?\s*)$'
            def replacer(match):
                # Returns: from agents import data_agent [as X]
                end_match = match.group(2)
                return match.group(1).replace('import', 'from').strip() + f" {pkg} import " + mod + end_match
                
            new_content = re.sub(import_pattern, replacer, new_content, flags=re.MULTILINE)

        if new_content != content:
            with open(py_file, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"Refactored imports in: {py_file}")

    print("\n[Refactor Complete]")

if __name__ == "__main__":
    main()
