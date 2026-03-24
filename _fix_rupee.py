import os
from pathlib import Path

def main():
    all_py_files = list(Path('.').rglob('*.py'))
    
    count = 0
    for file_path in all_py_files:
        if '.venv' in str(file_path) or '__pycache__' in str(file_path) or '.git' in str(file_path):
            continue
            
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        if 'Rs.' in content:
            new_content = content.replace('Rs.', 'Rs.')
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"Removed Rs. from {file_path}")
            count += 1
            
    print(f"Fixed encoding issues across {count} files.")

if __name__ == "__main__":
    main()
