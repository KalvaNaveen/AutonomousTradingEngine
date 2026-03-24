import os
from pathlib import Path

def main():
    replacements = {
        '[PASS]': '[PASS]',
        '[FAIL]': '[FAIL]',
        '[WARN]': '[WARN]',
        '[STOP]': '[STOP]',
        '[INFO]': '[INFO]',
        '-': '-', # The 'u' with grave in the logs looked like a mess
        'Rs.': 'Rs.'
    }
    
    all_py_files = list(Path('.').rglob('*.py'))
    
    count = 0
    for file_path in all_py_files:
        if '.venv' in str(file_path) or '__pycache__' in str(file_path) or '.git' in str(file_path):
            continue
            
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        new_content = content
        for char, repl in replacements.items():
            new_content = new_content.replace(char, repl)
            
        if new_content != content:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"Sanitized: {file_path}")
            count += 1
            
    print(f"Sanitized results across {count} files.")

if __name__ == "__main__":
    main()
