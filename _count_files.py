import os
count = 0
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ('.venv', '.git', 'node_modules', '.kilo', 'ai-office', 'ai-office-worktrees')]
    for f in files:
        if f.endswith('.py'):
            count += 1
print(f"Python files (excl venv/git/node_modules/.kilo/ai-office): {count}")
# Also count all files
total = 0
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ('.venv', '.git', '.ruff_cache', '.pytest_cache')]
    for f in files:
        total += 1
print(f"All files (excl caches): {total}")
