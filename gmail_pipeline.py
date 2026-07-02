#!/usr/bin/env python3
"""
Auto Email Sender — LOCAL CLI (zero dependencies, python3 stdlib only)

Talks to the Apps Script web app (Code.gs) deployed on your Google account.
You write a batch JSON (the emails); this script does everything mechanical:
validate -> chunk -> submit -> status. Sending runs on Google's servers, so
your laptop can sleep or die after `submit`.

Hardened after adversarial review:
 - submit is IDEMPOTENT (client_key per chunk): retrying after a timeout/crash
   can never email the same recipient twice
 - receipts are written incrementally; partial failures emit a remainder file
   with exactly the un-submitted emails + recovery instructions
 - contact-history cross-check is a HARD error (catastrophic failure =
   re-emailing a real contact); override with --allow-recontact
 - US timezone aliases (ET/CT/MT/PT) resolve through zoneinfo per the actual
   date -- correct in summer AND winter

Usage:
  gmail_pipeline.py init --url <WEBAPP_/exec_URL> [--secret S]   (prompts if omitted)
  gmail_pipeline.py ping
  gmail_pipeline.py validate --batch FILE [--tracker CSV] [--tz MDT]
  gmail_pipeline.py submit   --batch FILE (--send-at "2026-07-10 08:00" --tz MDT | per-email send_at)
                             (--tracker CSV | --no-tracker-check) [--allow-recontact]
                             [--label NAME] [--chunk 25] [--no-default-attach] [--dry-run] [--yes]
                             [--attach FILE ...] [--plain]
    --attach FILE   attach FILE to EVERY email in the batch (repeatable; server v4+ --
                    pre-flight ping hard-fails with redeploy instructions on old servers)
    --plain         plain-text only; default also sends a rich-text (HTML) body so
                    recipients don't see the hard-wrapped "boxed" plain-text look
  gmail_pipeline.py status   [--batch-id ID] [--verbose]
  gmail_pipeline.py cancel   --batch-id ID [--trash-drafts]
  gmail_pipeline.py send-now --batch-id ID
  gmail_pipeline.py convert  --in old-format.json --out batch.json --date 2026-07-10
                             [--tzmap "ET=07:00,CT=08:00,MT=09:00,PT=10:00"] [--base-tz MDT]
                             [--skip-status scheduled,sent,skipped,dead,bounced]

Batch file format:
  [ {"to": "a@b.com", "subject": "...", "body": "...",            # required
     "firm": "...",                                               # optional metadata
     "send_at": "2026-07-10 07:00 MDT"}, ... ]                    # optional per-email time
  or {"emails": [ ... same ... ]}
"""

import argparse
import base64
import csv
import getpass
import hashlib
import html
import json
import mimetypes
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:          # pragma: no cover
    ZoneInfo = None

CONFIG_PATH = os.environ.get(
    "AUTO_EMAIL_CONFIG",
    os.path.expanduser("~/.config/auto-email-sender/config.json"),
)

# DST-aware IANA zones for the convenience aliases (resolved per actual date)
TZ_IANA = {
    "ET": "America/New_York", "CT": "America/Chicago",
    "MT": "America/Denver", "PT": "America/Los_Angeles",
    "CN": "Asia/Shanghai", "CHINA": "Asia/Shanghai", "BJT": "Asia/Shanghai",
}
# explicit fixed-offset names (use these to force a specific offset)
TZ_FIXED = {
    "MDT": -6, "MST": -7, "CDT": -5, "CST": -6, "EDT": -4, "EST": -5,
    "PDT": -7, "PST": -8, "UTC": 0, "GMT": 0,
}

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
PLACEHOLDER_RES = [
    re.compile(r"\{[^{}]{1,60}\}"),                                   # {firm}, {First Name}, {name2}
    re.compile(r"<\s*(name|first|last|firm|company)[^>]{0,40}>", re.I),  # <NAME>, <first name>
    re.compile(r"\[(Firm|Name|First|Company)(\s*Name)?\]", re.I),        # [Firm], [First Name]
    re.compile(r"\b(PLACEHOLDER|TODO|FIXME|XXX)\b"),
    re.compile(r"^(Hi|Dear|Hello)\s*[,;]", re.M),                        # empty greeting "Hi ,"
]


# ---------- config ----------

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        sys.exit(f"No config at {CONFIG_PATH}. Run: gmail_pipeline.py init --url ... --secret ...")
    for k in ("url", "secret"):
        if not cfg.get(k):
            sys.exit(f"config missing '{k}' — re-run init")
    return cfg


def cmd_init(args):
    secret = args.secret or getpass.getpass("secret (input hidden): ").strip()
    if len(secret) < 12:
        sys.exit("secret too short (>=12 chars)")
    d = os.path.dirname(CONFIG_PATH)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"url": args.url, "secret": secret}, f, indent=2)
    print(f"config written -> {CONFIG_PATH}")
    print("now test with: gmail_pipeline.py ping")


# ---------- HTTP ----------

class ApiError(Exception):
    pass


def call_api(cfg, payload, timeout=180):
    """POST to the web app. Raises ApiError on any failure (never exits)."""
    payload = dict(payload)
    payload["secret"] = cfg["secret"]
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        cfg["url"], data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # follows Apps Script 302
            body = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code} from web app: {e.read().decode()[:300]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError(f"network error: {e}")
    try:
        out = json.loads(body)
    except json.JSONDecodeError:
        raise ApiError("web app returned non-JSON (wrong URL? not deployed as "
                       "'Anyone with the link'?): " + body[:400])
    if not out.get("ok"):
        raise ApiError(f"web app error: {out.get('error')}")
    return out


def call_api_or_exit(payload):
    try:
        return call_api(load_config(), payload)
    except ApiError as e:
        sys.exit(str(e))


# ---------- time parsing ----------

def parse_send_at(s, default_tz=None):
    """'+10m'/'+2h', ISO with offset, or 'YYYY-MM-DD HH:MM[ TZ]'. Returns epoch ms. Raises ValueError."""
    s = str(s).strip()
    m = re.fullmatch(r"\+(\d+)([mh])", s)
    if m:
        delta = int(m.group(1)) * (60 if m.group(2) == "m" else 3600)
        return int((time.time() + delta) * 1000)
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            return int(dt.timestamp() * 1000)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})(?:\s+([A-Za-z]+))?", s)
    if not m:
        raise ValueError(f"can't parse time {s!r} (use 'YYYY-MM-DD HH:MM' + tz, ISO+offset, or '+10m')")
    tz_name = (m.group(3) or default_tz or "").upper()
    naive = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
    if tz_name in TZ_IANA and ZoneInfo is not None:
        dt = naive.replace(tzinfo=ZoneInfo(TZ_IANA[tz_name]))   # DST-correct for the date
    elif tz_name in TZ_FIXED:
        dt = naive.replace(tzinfo=timezone(timedelta(hours=TZ_FIXED[tz_name])))
    elif tz_name in TZ_IANA:                                    # zoneinfo missing — fall back
        fallback = {"ET": -4, "CT": -5, "MT": -6, "PT": -7, "CN": 8, "CHINA": 8, "BJT": 8}
        dt = naive.replace(tzinfo=timezone(timedelta(hours=fallback[tz_name])))
    else:
        known = ", ".join(sorted(list(TZ_IANA) + list(TZ_FIXED)))
        raise ValueError(f"unknown timezone {tz_name!r} — known: {known}")
    return int(dt.timestamp() * 1000)


def fmt_ms(ms):
    utc = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    if ZoneInfo is not None:
        mt = utc.astimezone(ZoneInfo("America/Denver"))
        cn = utc.astimezone(ZoneInfo("Asia/Shanghai"))
    else:
        mt = utc.astimezone(timezone(timedelta(hours=-6)))
        cn = utc.astimezone(timezone(timedelta(hours=8)))
    return f"{utc:%Y-%m-%d %H:%M}Z = {mt:%m-%d %H:%M} MT = {cn:%m-%d %H:%M} CN"


# ---------- rich text + attachments ----------

MAX_ATTACH_TOTAL = 20 * 1024 * 1024   # raw bytes; Gmail's cap is 25MB — leave headroom


def body_to_html(body):
    """Plain text -> simple HTML (like a hand-typed Gmail message), so mail clients
    don't hard-wrap the plain-text part into the narrow ragged-left column."""
    esc = html.escape(str(body)).replace("\r\n", "\n")
    esc = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', esc)
    return '<div dir="ltr">' + esc.replace("\n", "<br>\n") + "</div>"


def encode_attachments(paths):
    """[path, ...] -> ([{filename, mimeType, data(b64)}, ...], total_raw_bytes)."""
    atts, total = [], 0
    for p in paths:
        try:
            with open(p, "rb") as f:
                raw = f.read()
        except OSError as e:
            sys.exit(f"--attach {p}: {e}")
        if not raw:
            sys.exit(f"--attach {p}: file is empty")
        total += len(raw)
        if total > MAX_ATTACH_TOTAL:
            sys.exit(f"--attach total exceeds {MAX_ATTACH_TOTAL // (1024*1024)}MB raw — Gmail would reject it")
        mt = mimetypes.guess_type(p)[0] or "application/octet-stream"
        atts.append({"filename": os.path.basename(p), "mimeType": mt,
                     "data": base64.b64encode(raw).decode("ascii")})
    return atts, total


# ---------- batch load / validate ----------

def load_batch(path):
    with open(path) as f:
        data = json.load(f)
    emails = data.get("emails") if isinstance(data, dict) else data
    if not isinstance(emails, list) or not emails:
        sys.exit("batch file must be a non-empty list (or {'emails': [...]})")
    return emails


def load_tracker_emails(tracker_csv):
    """Lowercase addresses already in the campaign tracker (any status). Case-insensitive headers."""
    seen = set()
    try:
        with open(tracker_csv, newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                sys.exit(f"tracker has no header row: {tracker_csv}")
            lower_map = {h.lower().strip(): h for h in reader.fieldnames}
            if "email" not in lower_map:
                sys.exit(f"tracker {tracker_csv} has no 'email' column (headers: {reader.fieldnames})")
            col = lower_map["email"]
            for row in reader:
                addr = (row.get(col) or "").strip().lower()
                if addr:
                    seen.add(addr)
    except FileNotFoundError:
        sys.exit(f"tracker not found: {tracker_csv}")
    return seen


def validate_batch(emails, tracker_csv=None, default_send_at=None, default_tz=None,
                   allow_recontact=False):
    errors, warnings, notes = [], [], []
    seen_to = {}
    no_send_at = 0
    tracker = load_tracker_emails(tracker_csv) if tracker_csv else None
    now_ms = time.time() * 1000
    for i, em in enumerate(emails):
        tag = f"[{i}] {em.get('to', em.get('email', '(no to)'))}"
        to = (em.get("to") or em.get("email") or "").strip()
        if not EMAIL_RE.fullmatch(to):
            errors.append(f"{tag}: bad/missing 'to' address")
        lo = to.lower()
        if lo and lo in seen_to:
            errors.append(f"{tag}: duplicate of [{seen_to[lo]}]")
        seen_to.setdefault(lo, i)
        subj, body = em.get("subject", ""), em.get("body", "")
        if not subj.strip():
            errors.append(f"{tag}: empty subject")
        if len(subj) > 150:
            warnings.append(f"{tag}: subject {len(subj)} chars (long)")
        if re.search(r"[\r\n]", subj):
            errors.append(f"{tag}: newline in subject")
        if len(body.strip()) < 200:
            errors.append(f"{tag}: body only {len(body.strip())} chars (template missing?)")
        if len(body) > 6000:
            warnings.append(f"{tag}: body {len(body)} chars (very long)")
        for rx in PLACEHOLDER_RES:
            hit = rx.search(subj + "\n" + body)
            if hit:
                errors.append(f"{tag}: unfilled placeholder {hit.group(0)!r}")
                break
        if em.get("send_at"):
            try:
                ms = parse_send_at(em["send_at"], default_tz)
                if ms < now_ms - 60_000:
                    errors.append(f"{tag}: send_at {em['send_at']!r} is in the past")
            except ValueError as e:
                errors.append(f"{tag}: {e}")
        else:
            no_send_at += 1
            if not default_send_at:
                errors.append(f"{tag}: no send_at and no --send-at given")
        if tracker is not None and lo in tracker:
            msg = f"{tag}: ALREADY in tracker (contacted before)"
            if allow_recontact:
                warnings.append(msg + " — allowed by --allow-recontact")
            else:
                errors.append(msg + " — pass --allow-recontact to override")
    if no_send_at and default_send_at:
        notes.append(f"note: {no_send_at} emails have no send_at — the --send-at value applies")
    elif no_send_at and default_send_at is None:
        notes.append(f"note: {no_send_at} emails have no send_at — submit will require --send-at")
    return errors, warnings, notes


# ---------- commands ----------

def cmd_ping(args):
    out = call_api_or_exit({"action": "ping"})
    v = out.get("version")
    tail = f" (server v{v})" if v else " (server pre-v4 — no --attach support; redeploy Code.gs to upgrade)"
    print(f"pong from {out.get('account')} at {out.get('now')}{tail}")


def cmd_validate(args):
    emails = load_batch(args.batch)
    errors, warnings, notes = validate_batch(
        emails, args.tracker, default_send_at="(validate)", default_tz=args.tz,
        allow_recontact=args.allow_recontact)
    for n in notes:
        print(f"  NOTE  {n}")
    for w in warnings:
        print(f"  WARN  {w}")
    for e in errors:
        print(f"  ERROR {e}")
    print(f"{len(emails)} emails, {len(errors)} errors, {len(warnings)} warnings")
    sys.exit(1 if errors else 0)


def _safe_name(s):
    return re.sub(r"[^\w.\-]", "_", s or "batch")[:60]


def _client_key(label, ms, chunk, att_sig=""):
    h = hashlib.sha256()
    h.update(f"{label}|{ms}|{att_sig}|".encode())
    for em in sorted(chunk, key=lambda x: x["to"]):
        h.update(em["to"].lower().encode())
        h.update(hashlib.sha256((em["subject"] + " " + em["body"]).encode()).digest())
    return h.hexdigest()[:32]


def cmd_submit(args):
    if not args.tracker and not args.no_tracker_check:
        sys.exit("submit requires --tracker <campaign CSV> (recontact guard) or an explicit --no-tracker-check")
    if not 1 <= args.chunk <= 30:
        sys.exit("--chunk must be 1-30 (Apps Script accepts max 30 per POST)")
    cfg = load_config()
    emails = load_batch(args.batch)
    errors, warnings, notes = validate_batch(
        emails, args.tracker, args.send_at, args.tz, allow_recontact=args.allow_recontact)
    for n in notes:
        print(f"  NOTE  {n}")
    for w in warnings:
        print(f"  WARN  {w}")
    if errors:
        for e in errors:
            print(f"  ERROR {e}")
        sys.exit(f"{len(errors)} validation errors — fix the batch first")

    # attachments (applied to EVERY email in the batch) + server-capability gate
    attachments, att_bytes, att_sig = [], 0, ""
    if getattr(args, "attach", None):
        attachments, att_bytes = encode_attachments(args.attach)
        att_sig = hashlib.sha256("".join(a["data"] for a in attachments).encode()).hexdigest()[:16]
        if not args.dry_run:
            try:
                pong = call_api(cfg, {"action": "ping"})
            except ApiError as e:
                sys.exit(f"pre-flight ping failed: {e}")
            if int(pong.get("version") or 0) < 4:
                sys.exit("server deployment predates --attach (ping has no version).\n"
                         "  Upgrade (~2 min): script.google.com -> open the pipeline project ->\n"
                         "  paste the current tools/email-pipeline/Code.gs over the old code -> save ->\n"
                         "  Deploy -> Manage deployments -> pencil icon -> Version: New version -> Deploy.\n"
                         "  URL/secret/properties stay the same; then re-run this command.")

    # group by send time (all parses validated above)
    # NB: parse the --send-at default ONCE — per-email parsing of relative times
    # ('+10m') can straddle a millisecond boundary and shatter one batch into many
    default_ms = parse_send_at(args.send_at, args.tz) if args.send_at else None
    groups = {}
    for em in emails:
        ms = parse_send_at(em["send_at"], args.tz) if em.get("send_at") else default_ms
        entry = {"to": (em.get("to") or em.get("email")).strip(),
                 "subject": em["subject"], "body": em["body"]}
        if not getattr(args, "plain", False):
            entry["html"] = body_to_html(em["body"])
        groups.setdefault(ms, []).append(entry)
    now_ms = time.time() * 1000
    for ms in groups:
        if ms < now_ms - 60_000:
            sys.exit(f"send time {fmt_ms(ms)} is in the past — aborting before anything is submitted")

    label = args.label or os.path.splitext(os.path.basename(args.batch))[0]
    total = sum(len(v) for v in groups.values())
    if args.send_at and args.send_at.strip().startswith("+"):
        print("  NOTE  relative --send-at: crash-safe idempotent retry needs the ABSOLUTE time — "
              "if this run dies mid-submit, retry with the absolute UTC time shown in the PLAN below")
    print(f"\nPLAN: {total} emails in {len(groups)} send-time group(s), label '{label}':")
    for ms in sorted(groups):
        print(f"  {fmt_ms(ms)}  ->  {len(groups[ms])} emails")
    if args.no_default_attach:
        print("  (default attachment disabled)")
    if attachments:
        print(f"  (+{len(attachments)} attachment(s), {att_bytes/1024:.0f} KB raw: "
              + ", ".join(a["filename"] for a in attachments) + ")")
    if getattr(args, "plain", False):
        print("  (plain-text bodies — rich-text disabled)")
    if args.dry_run:
        print("dry-run: nothing submitted.")
        return
    if not args.yes:
        try:
            ans = input("submit? [y/N] ").strip().lower()
        except EOFError:
            sys.exit("no tty for confirmation — pass --yes")
        if ans != "y":
            sys.exit("aborted")

    # build the full chunk plan first, then submit with incremental receipts
    plan = []
    for ms in sorted(groups):
        ems = groups[ms]
        for c in range(0, len(ems), args.chunk):
            plan.append((ms, ems[c:c + args.chunk]))

    rdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "receipts")
    os.makedirs(rdir, exist_ok=True)
    stamp = int(time.time())
    rpath = os.path.join(rdir, f"{_safe_name(label)}-{stamp}.json")

    receipts, submitted_emails = [], set()
    failure = None
    for idx, (ms, chunk) in enumerate(plan):
        payload = {"action": "submit", "send_at_ms": ms, "emails": chunk,
                   "label": label, "client_key": _client_key(label, ms, chunk, att_sig)}
        if args.no_default_attach:
            payload["attach_default"] = False
        if attachments:
            payload["attachments"] = attachments
        try:
            out = call_api(cfg, payload)
        except ApiError as e:
            failure = (idx, str(e))
            break
        receipts.append(out)
        with open(rpath, "w") as f:                      # incremental — survives later crashes
            json.dump(receipts, f, indent=2)
        for em in chunk:
            submitted_emails.add(em["to"].lower())
        dd = "  (deduped — was already submitted)" if out.get("deduped") else ""
        fails = out.get("draft_failures") or []
        warn = f"  ⚠ {out['warning']}" if out.get("warning") else ""
        print(f"  [{idx+1}/{len(plan)}] batch {out['batch_id']}: drafted {out['drafted']}/{len(chunk)}{dd}{warn}"
              + (f"  !! {len(fails)} DRAFT FAILURES: {fails}" if fails else ""))
        if attachments and not out.get("deduped") and out.get("attached") != len(attachments):
            print(f"      ⚠ server did not confirm attachments (attached={out.get('attached')!r}) "
                  "— open the drafts in Gmail and check the files are on them")

    if failure is not None:
        idx, err = failure
        remainder = [em for ms, chunk in plan[idx:] for em in chunk
                     if em["to"].lower() not in submitted_emails]
        rem_path = os.path.join(rdir, f"remainder-{_safe_name(label)}-{stamp}.json")
        with open(rem_path, "w") as f:
            json.dump(remainder, f, indent=2)
        print(f"\n!! chunk {idx+1}/{len(plan)} FAILED: {err}")
        print(f"   already submitted: {len(submitted_emails)} emails in {len(receipts)} batch(es)"
              f" (receipt: {rpath})" if receipts else "   nothing was submitted before the failure.")
        print(f"   NOT submitted: {len(remainder)} emails -> {rem_path}")
        print("   RECOVERY: 1) gmail_pipeline.py status   (confirm what's live)")
        print(f"             2) re-run submit with --batch {rem_path} (same flags) — already-sent chunks")
        print("                are also safe to retry verbatim: client_key dedupes them server-side.")
        sys.exit(1)

    print(f"\nreceipt -> {rpath}")
    print("Submitted. Drafts are in Gmail now; Google's servers send them at the times above.")
    print("Laptop can sleep. Check later with: gmail_pipeline.py status")
    if any(r.get("draft_failures") for r in receipts):
        sys.exit(1)


def cmd_status(args):
    payload = {"action": "status"}
    if args.batch_id:
        payload["batch_id"] = args.batch_id
    if args.verbose:
        payload["verbose"] = True
    out = call_api_or_exit(payload)
    batches = out.get("batches", [])
    if out.get("corrupt_records"):
        print(f"⚠ corrupt records on server: {out['corrupt_records']}")
    if not batches:
        print("no batches on record")
        return
    tot = {}
    for b in batches:
        c = b["counts"]
        for k, v in c.items():
            tot[k] = tot.get(k, 0) + v
        nz = "  ".join(f"{k} {v}" for k, v in c.items() if v)
        print(f"{b['batch_id']:<18} {b['state']:<20} {b['send_at_utc']}  {nz:<40} {b.get('label','')}")
        if args.verbose:
            for it in b.get("items", []):
                line = f"    {it['state']:<13} {it['to']}"
                if it.get("lastError"):
                    line += f"  ({it['lastError'][:80]})"
                print(line)
    print("TOTAL: " + " | ".join(f"{k} {v}" for k, v in sorted(tot.items()) if v))
    if tot.get("sent_assumed"):
        print("note: 'sent_assumed' = draft was gone at send time (sent manually or by an earlier run);"
              " verify in Gmail Sent before re-sending anyone.")


def cmd_cancel(args):
    payload = {"action": "cancel", "batch_id": args.batch_id}
    if args.trash_drafts:
        payload["trash_drafts"] = True
    out = call_api_or_exit(payload)
    msg = f"cancelled {out.get('cancelled', 0)} pending emails"
    if args.trash_drafts:
        msg += f", trashed {out.get('trashed', 0)} drafts"
    if out.get("note"):
        msg += f"  ({out['note']})"
    print(msg)


def cmd_send_now(args):
    out = call_api_or_exit({"action": "send_now", "batch_id": args.batch_id})
    print(f"batch {out['batch_id']} -> {out['state']}  counts={out['counts']}")


def cmd_convert(args):
    """tracker-style JSON (firm/email/subject/body/tz/...) -> pipeline batch with per-email send_at."""
    with open(args.infile) as f:
        rows = json.load(f)
    tzmap = {}
    for pair in args.tzmap.split(","):
        k, v = pair.split("=")
        tzmap[k.strip().upper()] = v.strip()
    skip_status = {s.strip().lower() for s in args.skip_status.split(",") if s.strip()}
    out, skipped, tz_warned = [], [], set()
    for r in rows:
        status = str(r.get("status", "")).strip().lower()
        if status in skip_status:                       # exact match — 'unsent' must NOT match 'sent'
            skipped.append(f"{r.get('firm')}: status={r.get('status')}")
            continue
        to = (r.get("email") or "").strip()
        if not to:
            skipped.append(f"{r.get('firm')}: no email")
            continue
        tz = str(r.get("tz", "ET")).upper()
        if tz not in tzmap:
            if tz not in tz_warned:
                print(f"  WARN unknown tz {tz!r} ({r.get('firm')}) — using ET slot")
                tz_warned.add(tz)
            tz = "ET"
        out.append({
            "to": to,
            "subject": r.get("subject", ""),
            "body": r.get("body", ""),
            "firm": r.get("firm", ""),
            "send_at": f"{args.date} {tzmap.get(tz, tzmap['ET'])} {args.base_tz}",
        })
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"wrote {len(out)} emails -> {args.out}  (skipped {len(skipped)})")
    for s in skipped[:15]:
        print(f"  skip: {s}")
    if len(skipped) > 15:
        print(f"  ... +{len(skipped)-15} more")


# ---------- main ----------

def main():
    p = argparse.ArgumentParser(description="auto-email-sender CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init")
    s.add_argument("--url", required=True)
    s.add_argument("--secret", help="omit to be prompted (keeps it out of shell history)")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("ping")
    s.set_defaults(fn=cmd_ping)

    s = sub.add_parser("validate")
    s.add_argument("--batch", required=True)
    s.add_argument("--tracker")
    s.add_argument("--tz", default="MT")
    s.add_argument("--allow-recontact", action="store_true")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("submit")
    s.add_argument("--batch", required=True)
    s.add_argument("--send-at", help="e.g. '2026-06-11 08:00' (with --tz), ISO+offset, or '+10m'")
    s.add_argument("--tz", default="MT", help="ET/CT/MT/PT (DST-aware) or MDT/EST/... (fixed) or CN")
    s.add_argument("--tracker", help="campaign CSV — re-contacting an address in it is a HARD error")
    s.add_argument("--no-tracker-check", action="store_true", help="explicitly skip the recontact guard")
    s.add_argument("--allow-recontact", action="store_true", help="downgrade tracker hits to warnings")
    s.add_argument("--label")
    s.add_argument("--chunk", type=int, default=25)
    s.add_argument("--no-default-attach", action="store_true",
                   help="do not attach the server-configured default attachment (if any)")
    s.add_argument("--attach", action="append", metavar="FILE",
                   help="attach FILE to every email in the batch (repeatable; needs server v4 — "
                        "submit pre-flights a version check and tells you how to redeploy if too old)")
    s.add_argument("--plain", action="store_true",
                   help="plain-text bodies only (default also sends rich-text HTML so text flows normally)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("status")
    s.add_argument("--batch-id")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("cancel")
    s.add_argument("--batch-id", required=True)
    s.add_argument("--trash-drafts", action="store_true")
    s.set_defaults(fn=cmd_cancel)

    s = sub.add_parser("send-now")
    s.add_argument("--batch-id", required=True)
    s.set_defaults(fn=cmd_send_now)

    s = sub.add_parser("convert")
    s.add_argument("--in", dest="infile", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--date", required=True, help="YYYY-MM-DD send date")
    s.add_argument("--tzmap", default="ET=07:00,CT=08:00,MT=09:00,PT=10:00",
                   help="recipient-tz -> send time in --base-tz; default = ~9 AM recipient local")
    s.add_argument("--base-tz", default="MDT")
    s.add_argument("--skip-status", default="scheduled,sent,skipped,dead,bounced",
                   help="EXACT status values to skip (comma-separated)")
    s.set_defaults(fn=cmd_convert)

    args = p.parse_args()
    try:
        args.fn(args)
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
