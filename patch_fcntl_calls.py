# patch_fcntl_calls.py — guard fcntl.flock() calls on Windows
import os, re

def patch(path):
    if not os.path.exists(path):
        print(f"[SKIP] {path} missing"); return
    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    changed = 0
    out = []
    for line in lines:
        m = re.match(r'^(\s*)fcntl\.flock\(', line)
        if m and 'if fcntl is not None' not in line:
            indent = m.group(1)
            out.append(indent + 'if fcntl is not None: ' + line[len(indent):])
            changed += 1
        else:
            out.append(line)
    if changed:
        with open(path, 'w', encoding='utf-8') as f:
            f.writelines(out)
        print(f"[OK] {path} — {changed} flock() calls guarded")
    else:
        print(f"[SKIP] {path} — no unguarded flock() calls")

for fn in ['proxy_manager.py', 'session_cache.py', 'tg_bot.py']:
    patch(fn)

print("\nNow: restart API, then re-run top.py")