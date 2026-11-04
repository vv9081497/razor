"""
site_rotator.py — Round-robin site rotation from sites.txt / site.txt
"""
import os
import threading
import itertools
from typing import Optional

_SITES_FILES = ["sites.txt", "site.txt"]
_sites: list[str] = []
_lock = threading.Lock()
_counter = itertools.count()
_loaded = False


def _find_file() -> Optional[str]:
    for name in _SITES_FILES:
        if os.path.exists(name):
            return name
    return None


def _load():
    global _sites, _loaded
    if _loaded:
        return
    path = _find_file()
    if not path:
        _loaded = True
        return
    seen = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or not s.startswith("http"):
                continue
            key = s.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            _sites.append(s)
    _loaded = True
    print(f"[site_rotator] loaded {len(_sites)} sites from {path}", flush=True)


def get_next_site() -> Optional[str]:
    _load()
    if not _sites:
        return None
    with _lock:
        idx = next(_counter) % len(_sites)
        return _sites[idx]


def reload():
    global _sites, _loaded
    with _lock:
        _sites = []
        _loaded = False
    _load()


def get_all() -> list[str]:
    _load()
    return list(_sites)


def count() -> int:
    _load()
    return len(_sites)