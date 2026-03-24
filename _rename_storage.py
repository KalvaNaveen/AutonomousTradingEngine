import os
import subprocess
import re
from pathlib import Path

def main():
    print("1. Moving DB and CSV files into 'data/'...")
    files_to_move = ['data/engine_state.db', 'data/journal.db', 'data/nifty250.csv']
    os.makedirs('data', exist_ok=True)
    
    for f in files_to_move:
        if os.path.exists(f):
            target = f"data/{f}"
            res = subprocess.run(["git", "mv", f, target], capture_output=True)
            if res.returncode != 0:
                os.rename(f, target)
            print(f"Moved {f} -> {target}")

    print("2. Renaming 'storage' directory to 'storage'...")
    if os.path.exists("storage"):
        res = subprocess.run(["git", "mv", "storage", "storage"], capture_output=True)
        if res.returncode != 0:
            os.rename("storage", "storage")
        print("Renamed storage -> storage")
        
    print("3. Refactoring hardcoded paths and imports...")
    
    all_py_files = list(Path('.').rglob('*.py'))
    all_py_files.extend(list(Path('.').rglob('*.md')))
    
    for file_path in all_py_files:
        if '.venv' in str(file_path) or '__pycache__' in str(file_path) or '.git' in str(file_path):
            continue
            
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        new_content = content
        
        # Replace the hardcoded module name and paths
        new_content = new_content.replace('storage', 'storage')
        
        # safely replace data/journal.db to data/journal.db if not already prefixed
        new_content = re.sub(r'(?<!data/)(?<!data\\)journal\.db', 'data/journal.db', new_content)
        new_content = re.sub(r'(?<!data/)(?<!data\\)engine_state\.db', 'data/engine_state.db', new_content)
        new_content = re.sub(r'(?<!data/)(?<!data\\)nifty250\.csv', 'data/nifty250.csv', new_content)
        
        if new_content != content:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"Refactored: {file_path}")

    print("\n[Data and Storage Refactor Complete]")

if __name__ == "__main__":
    main()
