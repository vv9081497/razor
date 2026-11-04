"""
proxy_auto_load.py — Auto-load proxies from proxies.txt, re-test every hour.
"""
import os
import threading
import time
import proxy_manager

_PROXIES_FILE = os.environ.get("PROXIES_FILE", "proxies.txt")
_RELOAD_INTERVAL = int(os.environ.get("PROXY_RELOAD_INTERVAL", "3600"))
_thread: threading.Thread | None = None
_stop = threading.Event()


def _normalize(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    for p in ("http://", "https://"):
        if line.lower().startswith(p):
            line = line[len(p):]
    if "@" in line:
        try:
            auth, hp = line.rsplit("@", 1)
            user, pw = auth.split(":", 1)
            host, port = hp.rsplit(":", 1)
            if port.isdigit():
                return f"{host}:{port}:{user}:{pw}"
        except Exception:
            return None
    parts = line.split(":")
    if len(parts) >= 4 and parts[1].isdigit():
        return line
    return None


def load_once() -> dict:
    if not os.path.exists(_PROXIES_FILE):
        print(f"[proxy_auto_load] no {_PROXIES_FILE}, skipping", flush=True)
        return {"live": 0, "dead": 0, "blocked": 0, "invalid": 0, "total": 0}
    seen, clean = set(), []
    with open(_PROXIES_FILE, encoding="utf-8") as f:
        for line in f:
            n = _normalize(line)
            if n and n not in seen:
                seen.add(n)
                clean.append(n)
    if not clean:
        return {"live": 0, "dead": 0, "blocked": 0, "invalid": 0, "total": 0}
    print(f"[proxy_auto_load] testing {len(clean)} proxies from {_PROXIES_FILE}...", flush=True)
    t0 = time.time()
    summary = proxy_manager.bulk_check_proxies_threaded(clean, max_workers=50)
    elapsed = time.time() - t0
    print(f"[proxy_auto_load] DONE {elapsed:.0f}s — "
          f"LIVE={summary['live']} DEAD={summary['dead']} "
          f"BLOCKED={summary['blocked']} INVALID={summary['invalid']}", flush=True)
    print(f"[proxy_auto_load] pool: {proxy_manager.get_pool_stats()}", flush=True)
    return summary


def _loop():
    while not _stop.is_set():
        try:
            load_once()
        except Exception as e:
            print(f"[proxy_auto_load] loop error: {e}", flush=True)
        _stop.wait(_RELOAD_INTERVAL)


def start_background_loader():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="proxy-auto-load")
    _thread.start()
    print(f"[proxy_auto_load] background loader started (interval={_RELOAD_INTERVAL}s)", flush=True)


def stop_background_loader():
    _stop.set()