#!/usr/bin/env python3
# test_sites.py — bulk-test sites through local API with one card
import argparse, asyncio, json, os, sys, time
from datetime import datetime
import httpx

GOOD = {'charged','approved','declined','3ds_or_otp','ccn','rzp_risk_decline',
        'live_limit','auth_failed','rzp_bad_request'}
NO_CARD = {'international_not_supported','currency_mismatch','invalid_fee',
           'amount_mismatch','mandatory_item','card_type_not_allowed',
           'card_network_not_enabled','online_payments_not_enabled'}
DEAD = {'gay_site','merchant_blocked','merchant_not_activated',
        'payment_page_inactive','site_unavailable','site_change'}
RECHECK = {'rzp_server_error','server_error'}

def classify(r):
    s = str(r.get('status','')).lower()
    rs = str(r.get('reason','')).lower()
    m = str(r.get('message','')).lower()
    if s == 'charged': return 'GOOD', 'charged'
    if s == 'approved': return 'GOOD', f'approved({rs})'
    if s == 'dead' and rs in GOOD: return 'GOOD', f'declined({rs})'
    if rs in NO_CARD: return 'SKIP_NO_CARD', rs
    if rs in DEAD: return 'SKIP_DEAD', rs
    if rs in RECHECK or 'server error' in m: return 'RECHECK', rs or 'server_error'
    if 'waf' in rs or '403' in m or '406' in m: return 'WAF', rs or 'waf'
    if 'no_live_proxies' in m: return 'POOL_EMPTY', 'api_503_empty_pool'
    if 'timeout' in m or 'connect' in m: return 'ERROR', 'network'
    if s == 'dead': return 'GOOD', f'declined({rs or "?"})'
    return 'ERROR', rs or s or 'unknown'

async def one(client, api, card, site, key, proxy, amount, timeout):
    params = {'cc': card, 'site': site, 'amount': str(amount)}
    if key: params['key'] = key
    elif proxy: params['proxy'] = proxy
    else: return {'site':site,'verdict':'ERROR','why':'no_key_or_proxy','result':{}}
    t0 = time.time()
    try:
        r = await client.get(f'{api}/razorpay', params=params, timeout=timeout)
        try: data = r.json()
        except: data = {'status':'error','message':f'non-json http {r.status_code}','reason':'non_json'}
    except httpx.TimeoutException:
        data = {'status':'error','message':'api timeout','reason':'timeout'}
    except Exception as e:
        data = {'status':'error','message':str(e)[:200],'reason':'exception'}
    v, why = classify(data)
    return {'site':site,'verdict':v,'why':why,'result':data,'time_s':round(time.time()-t0,2)}

async def main(a):
    sites, seen = [], set()
    for line in open(a.sites, encoding='utf-8'):
        s = line.strip()
        if not s or s.startswith('#') or not s.startswith('http'): continue
        k = s.lower().rstrip('/')
        if k in seen: continue
        seen.add(k); sites.append(s)
    if not sites: sys.exit(f'no valid sites in {a.sites}')

    os.makedirs(a.out, exist_ok=True)
    jl = os.path.join(a.out, 'results.jsonl')
    done = {}
    if os.path.exists(jl):
        for line in open(jl, encoding='utf-8'):
            try:
                e = json.loads(line); done[e['site']] = e
            except: pass

    if a.retry_failed:
        to_test = [s for s in sites if s not in done or done[s]['verdict'] in ('WAF','ERROR','RECHECK','POOL_EMPTY')]
    elif a.retry_waf:
        to_test = [s for s in sites if s not in done or done[s]['verdict'] == 'WAF']
    else:
        to_test = [s for s in sites if s not in done]

    print(f'total {len(sites)} | done {len(done)} | to test {len(to_test)}')
    print(f'concurrency={a.concurrency} mode={"pool" if a.key else "proxy"}')
    print('-'*70)

    if a.key:
        try:
            r = httpx.get(f'{a.api}/pool-status', timeout=5)
            p = r.json()
            if p.get('live', 0) == 0:
                print(f'ERROR: pool empty ({p.get("live")} live). Add proxies first.')
                sys.exit(1)
            print(f'Pool OK: {p["live"]} live proxies\n')
        except Exception as e:
            print(f'WARN: pool check failed: {e}\n')

    if not to_test:
        write_summary(a, done, sites); return

    sem = asyncio.Semaphore(a.concurrency)
    lock = asyncio.Lock()
    n = [0]
    fh = open(jl, 'a', encoding='utf-8')

    async def worker(client, site):
        async with sem:
            r = await one(client, a.api, a.card, site, a.key, a.proxy, a.amount, a.timeout)
            async with lock:
                n[0] += 1
                fh.write(json.dumps(r) + '\n'); fh.flush()
                done[site] = r
                print(f'[{n[0]:>4}/{len(to_test)}] {r["verdict"]:<13} {r["why"][:30]:<30} {site}')

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            await asyncio.gather(*[worker(client, s) for s in to_test])
    finally:
        fh.close()
    write_summary(a, done, sites)

def write_summary(a, done, all_sites):
    b = {'GOOD':[], 'SKIP_NO_CARD':[], 'SKIP_DEAD':[], 'RECHECK':[],
         'WAF':[], 'ERROR':[], 'POOL_EMPTY':[]}
    for site, r in done.items():
        b.setdefault(r.get('verdict','ERROR'), []).append(site)
    files = {}
    for k, items in b.items():
        p = os.path.join(a.out, f'{k.lower()}.txt')
        with open(p, 'w', encoding='utf-8') as f:
            for s in sorted(items): f.write(s+'\n')
        files[k] = p
    summary = {'tested':len(done), 'total_input':len(all_sites),
               'counts':{k:len(v) for k,v in b.items()},
               'files':files, 'generated_at':datetime.now().isoformat()}
    sp = os.path.join(a.out, 'summary.json')
    json.dump(summary, open(sp,'w',encoding='utf-8'), indent=2)
    print('\n' + '='*70); print('SUMMARY'); print('='*70)
    for k,v in summary['counts'].items(): print(f'  {k:<15} {v}')
    print('-'*70)
    print(f'  good_sites.txt -> {files["GOOD"]}')
    print(f'  summary.json   -> {sp}')

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--sites', required=True)
    ap.add_argument('--card', required=True)
    ap.add_argument('--api', default='http://localhost:9000')
    ap.add_argument('--key', default='limca')
    ap.add_argument('--proxy', default=None)
    ap.add_argument('--amount', type=int, default=100)
    ap.add_argument('--out', default='./results')
    ap.add_argument('--concurrency', type=int, default=3)
    ap.add_argument('--timeout', type=float, default=120.0)
    ap.add_argument('--retry-failed', action='store_true')
    ap.add_argument('--retry-waf', action='store_true')
    asyncio.run(main(ap.parse_args()))