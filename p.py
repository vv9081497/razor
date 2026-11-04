# fix_proxies.py — convert user:pass@host:port -> host:port:user:pass
import re

def convert(line):
    line = line.strip()
    if not line or line.startswith('#'):
        return None
    # already host:port:user:pass (4+ colon parts, no @)
    if '@' not in line:
        parts = line.split(':')
        if len(parts) >= 4:
            return line
        return None
    # user:pass@host:port
    try:
        auth, hp = line.rsplit('@', 1)
        user, pw = auth.split(':', 1)
        host, port = hp.rsplit(':', 1)
        if not host or not port.isdigit():
            return None
        return f"{host}:{port}:{user}:{pw}"
    except Exception:
        return None

out, seen = [], set()
with open('proxies.txt') as f:
    for line in f:
        p = convert(line)
        if p and p not in seen:
            seen.add(p)
            out.append(p)

with open('proxies_clean.txt', 'w') as f:
    f.write('\n'.join(out) + '\n')

print(f"input: {len(open('proxies.txt').read().splitlines())} lines")
print(f"clean: {len(out)} valid proxies -> proxies_clean.txt")
print("\nfirst 5:")
for p in out[:5]:
    print('  ', p)