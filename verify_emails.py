#!/usr/bin/env python3
"""
Bulk email verifier (zero dependencies): MX lookup + SMTP RCPT probe + catch-all
detection. Pre-flight your recipient list BEFORE you send, so bad addresses never
cost you a bounce (and your sender reputation).

No mail is ever sent — the SMTP conversation stops at RCPT TO, then QUIT.

Input : JSON list of records, each {"email": "...", "alternates": ["..."], ...}
        (any extra keys are passed through untouched)
Output: --out gets the records safe to feed into a send batch, each with a
        "verify" object: {"verdict", "detail", "mx", "catch_all", "chosen_email"}
        verdict: valid | catch_all | invalid | no_mx | unknown (tempfail/timeout/blocked)
        PROVEN-bad records (verdict invalid / no_mx) are DROPPED into a sibling
        <out>.rejects.json file; pass --keep-all to keep everything in --out instead.
        When a record has "alternates", each candidate is tried in order and the
        first 'valid' one replaces .email (chosen_email records the winner).

Usage:
    verify_emails.py --in candidates.json --out verified.json \
        [--workers 14] [--helo yourdomain.com] [--probe-from you@yourdomain.com]

Notes:
  * Outbound port 25 is blocked on most home/cloud networks — you'll see lots of
    'unknown (conn:...)' there. Run from a host that can reach port 25.
  * Use a real domain you control for --helo / --probe-from; some servers reject
    probes from mismatched or throwaway senders.
  * 'catch_all' means the server accepts every address, so individual mailboxes
    can't be confirmed — treat as softer than 'valid'.
"""
import argparse
import json
import os
import random
import smtplib
import socket
import string
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict


def mx_lookup(domain, cache={}, lock=threading.Lock()):
    with lock:
        if domain in cache:
            return cache[domain]
    hosts = []
    try:
        out = subprocess.run(['dig', '+short', 'MX', domain, '+time=4', '+tries=2'],
                             capture_output=True, text=True, timeout=15).stdout
        recs = []
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) == 2 and parts[0].isdigit():
                recs.append((int(parts[0]), parts[1].rstrip('.')))
        hosts = [h for _, h in sorted(recs)]
        if not hosts:  # fall back to A record (implicit MX, RFC 5321)
            a = subprocess.run(['dig', '+short', 'A', domain, '+time=4', '+tries=2'],
                               capture_output=True, text=True, timeout=15).stdout.strip()
            if a:
                hosts = [domain]
    except Exception:
        hosts = []
    with lock:
        cache[domain] = hosts
    return hosts


class DomainProber:
    """One SMTP session per domain; probes all recipients + a random catch-all canary."""
    def __init__(self, domain, helo, probe_from):
        self.domain, self.helo, self.probe_from = domain, helo, probe_from
        self.result = {}      # email -> (code, msg)
        self.catch_all = None
        self.error = None

    def probe(self, emails):
        hosts = mx_lookup(self.domain)
        if not hosts:
            self.error = 'no_mx'
            return
        canary = ''.join(random.choices(string.ascii_lowercase, k=14)) + '.zz9@' + self.domain
        for host in hosts[:1]:
            try:
                with smtplib.SMTP(host, 25, timeout=8) as s:
                    s.helo(self.helo)
                    try:
                        if s.has_extn('starttls'):
                            s.starttls()
                            s.ehlo(self.helo)
                    except Exception:
                        pass
                    s.mail(self.probe_from)
                    for e in emails:
                        try:
                            code, msg = s.rcpt(e)
                        except smtplib.SMTPServerDisconnected:
                            raise
                        except Exception as ex:
                            code, msg = 0, str(ex).encode()
                        self.result[e] = (code, (msg or b'').decode(errors='replace')[:120])
                        time.sleep(0.08)
                    try:
                        c2, _ = s.rcpt(canary)
                        self.catch_all = (250 <= c2 < 260)
                    except Exception:
                        self.catch_all = None
                return
            except Exception as ex:
                self.error = f'conn:{type(ex).__name__}'
                continue


def verdict_for(code, catch_all):
    if code is None:
        return 'unknown'
    if 250 <= code < 260:
        return 'catch_all' if catch_all else 'valid'
    if code in (550, 551, 553, 554, 521) or (500 <= code < 560 and code != 552):
        return 'invalid'
    if 400 <= code < 500:
        return 'unknown'   # greylist / tempfail
    return 'unknown'


DROP_VERDICTS = ('invalid', 'no_mx')


def partition_records(records):
    """Split annotated records into (keep, reject). Only PROVEN-bad addresses —
    hard bounce or no mail server — are rejected; unknown/catch_all pass through
    because absence of proof isn't proof of badness."""
    keep, reject = [], []
    for r in records:
        verdict = (r.get('verify') or {}).get('verdict')
        (reject if verdict in DROP_VERDICTS else keep).append(r)
    return keep, reject


def main():
    ap = argparse.ArgumentParser(description="Bulk email verifier (MX + SMTP RCPT, no mail sent)")
    ap.add_argument('--in', dest='inp', required=True, help="JSON list of {email, alternates?, ...}")
    ap.add_argument('--out', required=True, help="output JSON path")
    ap.add_argument('--workers', type=int, default=14, help="concurrent domain probes")
    ap.add_argument('--helo', default='example.com', help="HELO/EHLO hostname (use a domain you control)")
    ap.add_argument('--probe-from', default='verify@example.com', help="MAIL FROM probe address")
    ap.add_argument('--keep-all', action='store_true',
                    help="keep invalid/no_mx records in --out instead of dropping them to <out>.rejects.json")
    a = ap.parse_args()

    records = json.load(open(a.inp))
    # group every candidate email by domain
    by_domain = defaultdict(list)
    for r in records:
        cands = [r.get('email', '')] + list(r.get('alternates') or [])
        cands = [c.strip().lower() for c in cands if c and '@' in c]
        r['_cands'] = list(dict.fromkeys(cands))
        for c in r['_cands']:
            by_domain[c.split('@')[1]].append(c)

    probers = {}

    def work(dom):
        p = DomainProber(dom, a.helo, a.probe_from)
        p.probe(sorted(set(by_domain[dom])))
        return dom, p

    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(work, d): d for d in by_domain}
        for f in as_completed(futs):
            dom, p = f.result()
            probers[dom] = p
            done += 1
            if done % 50 == 0:
                print(f'  ...{done}/{len(by_domain)} domains', file=sys.stderr, flush=True)

    tally = defaultdict(int)
    for r in records:
        chosen, vinfo = None, None
        for c in r['_cands']:
            dom = c.split('@')[1]
            p = probers.get(dom)
            if p is None or p.error == 'no_mx':
                vinfo = {'verdict': 'no_mx', 'detail': 'no MX/A record', 'mx': False, 'catch_all': None}
                continue
            if p.error and not p.result:
                vinfo = {'verdict': 'unknown', 'detail': p.error, 'mx': True, 'catch_all': p.catch_all}
                continue
            code, msg = p.result.get(c, (None, ''))
            v = verdict_for(code, p.catch_all)
            vinfo = {'verdict': v, 'detail': f'{code} {msg}'.strip(), 'mx': True, 'catch_all': p.catch_all}
            if v == 'valid':
                chosen = c
                break
            if v == 'catch_all' and chosen is None:
                chosen = c   # acceptable; keep but try later candidates for a hard-valid
        if chosen:
            r['email'] = chosen
        r['verify'] = vinfo or {'verdict': 'unknown', 'detail': 'no candidates', 'mx': None, 'catch_all': None}
        r['verify']['chosen_email'] = chosen
        tally[r['verify']['verdict']] += 1
        r.pop('_cands', None)

    keep, reject = (records, []) if a.keep_all else partition_records(records)
    dropped = ''
    if reject:
        base, ext = os.path.splitext(a.out)
        rej_path = base + '.rejects' + (ext or '.json')
        json.dump(reject, open(rej_path, 'w'), indent=1, ensure_ascii=False)
        dropped = f' — dropped {len(reject)} proven-bad -> {rej_path}'
    json.dump(keep, open(a.out, 'w'), indent=1, ensure_ascii=False)
    print(f'wrote {a.out} ({len(keep)} sendable): '
          + ' | '.join(f'{k} {v}' for k, v in sorted(tally.items())) + dropped, file=sys.stderr)


if __name__ == '__main__':
    main()
