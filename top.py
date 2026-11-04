# add_all.py — bulk-check all proxies from proxies_clean.txt and add live to pool
import proxy_manager

lines = open('proxies_clean.txt').read().strip().splitlines()
print(f"Testing {len(lines)} proxies...")
s = proxy_manager.bulk_check_proxies_threaded(lines, max_workers=50)
print(f"\nLIVE: {s['live']} | DEAD: {s['dead']} | BLOCKED: {s['blocked']} | INVALID: {s['invalid']}")
print(f"\nPool stats: {proxy_manager.get_pool_stats()}")