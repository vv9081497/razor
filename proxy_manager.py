"""proxy_manager.py — minimal stub. Proxy comes per-request from bot."""
import hashlib


def get_proxy_tag(proxy_str):
    if not proxy_str:
        return "direct"
    clean = proxy_str.strip()
    for p in ("http://", "https://", "socks5://"):
        if clean.lower().startswith(p):
            clean = clean[len(p):]
            break
    if "@" in clean:
        auth, hostport = clean.rsplit("@", 1)
        h = hashlib.md5(auth.encode()).hexdigest()[:8]
        return f"{hostport.lower()}_{h}"
    return clean.split("/")[0].lower()


def get_proxy_host(proxy_str):
    if not proxy_str:
        return "direct"
    clean = proxy_str.strip()
    for p in ("http://", "https://", "socks5://"):
        if clean.lower().startswith(p):
            clean = clean[len(p):]
            break
    host = clean.split("@")[-1].split(":")[0].strip().lower()
    return host or "direct"


def get_proxy_ip_port(proxy_str):
    if not proxy_str:
        return "direct"
    clean = proxy_str.strip()
    for p in ("http://", "https://", "socks5://"):
        if clean.lower().startswith(p):
            clean = clean[len(p):]
            break
    if "@" in clean:
        clean = clean.split("@")[-1]
    parts = clean.split(":")
    if len(parts) >= 2:
        return f"{parts[0].lower()}:{parts[1]}"
    return parts[0].lower()


def set_site_cooldown(site, hours=1, reason="site_error"): pass
def is_site_in_cooldown(site): return False
def get_site_cooldown_list(): return {}
def set_cooldown(proxy_host, hours=10, reason="waf_403"): pass
def is_in_cooldown(proxy_host): return False
def get_cooldown_list(): return {}
def record_dead_proxy(proxy_url, reason="waf_406"): pass
def _record_proxy_failure(proxy_host): return False
def _clear_proxy_failures(proxy_host): pass
def remove_from_pool(proxy_host): pass
def mark_proxy_used(proxy_host): pass
def get_pool_stats(): return {"total": 0, "live": 0, "cooldown": 0}
def get_all_live_proxies(): return []
def get_random_live_proxy(): return None
def validate_key(key): return key == "limca"
def _load_failure_counts(): return {}


FAILURE_THRESHOLD = 2
