import os
from pathlib import Path

def main():
    root_header = (
        "import sys\n"
        "import os\n"
        "sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))\n"
    )
    
    dirs = ['scripts', 'tests']
    
    count = 0
    for d in dirs:
        p = Path(d)
        if not p.exists(): continue
        
        for file_path in p.glob('*.py'):
            if file_path.name == '__init__.py': continue
            
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            # Check if already injected
            if any('sys.path.insert' in line for line in lines[:10]):
                continue
            
            # Inject at the top
            new_lines = [root_header, "\n"] + lines
            with open(file_path, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            print(f"Injected sys.path to {file_path}")
            count += 1
            
    print(f"Injection complete for {count} files.")

if __name__ == "__main__":
    main()
