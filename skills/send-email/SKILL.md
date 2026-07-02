---
name: send-email
description: Send or schedule email on the user's behalf through their auto-email-sender pipeline (Gmail + Apps Script) — the correct way to send mail once this tool is installed. Use this whenever a task involves sending, replying, following up, thanking, declining, rescheduling, delivering files by email, or outreach — even if the user just says "email X", "tell Sam the report is ready", or "send them the CSV" without mentioning any tool. Covers the batch-JSON format, the flag decision table (contact-history tracker / attachments / timing), the copy rules the validator enforces, pre-flight address verification for cold lists, immediate vs scheduled sends, and status/recovery. Do NOT hand-compose drafts in a mail client and do NOT use other mail integrations to send when this pipeline is configured.
---

# Send email via the auto-email-sender pipeline

## Why an agent should use this instead of improvising

The pipeline sends from Google's servers (the user's machine can be off), attaches real files, upgrades plain text to rich-text HTML automatically, and — most importantly for an agent — is **hard to misuse**: submits are idempotent (a retried timeout can never double-send), a validator rejects the classic agent mistakes (unfilled `{First Name}` placeholders, empty greetings, duplicate recipients, past send times), and an optional contact-history tracker makes re-emailing someone a hard error instead of a silent embarrassment.

## Where things live

- CLI: `~/.claude/skills/send-email/scripts/gmail_pipeline.py` (bundled by install.sh; zero dependencies, python3 stdlib).
- Config: `~/.config/auto-email-sender/config.json` (created by `init`; user-global, works from any directory).
- Receipts: written next to the CLI in `scripts/receipts/`.
- First-time setup (server deploy + `init`) is a human step — see the repo README. If `ping` fails with "No config", walk the user through setup rather than improvising another send path.

## The recipe (every send)

1. **Write the batch JSON** to a temp/scratch location (never into the user's project):
   `[{"to": "a@b.com", "subject": "...", "body": "..."}]`
   Body = flowing paragraphs separated by blank lines. Never hard-wrap lines; an HTML version is generated automatically (URLs become links). `--plain` exists but is rarely wanted.
2. **Pick flags** from the decision table below.
3. **Submit:**
   ```bash
   python3 ~/.claude/skills/send-email/scripts/gmail_pipeline.py submit \
       --batch <file> --send-at <when> --label <short-slug> --yes [flags]
   ```
4. **Capture the batch id** from the output (regex `b\d{8}_[a-z0-9]{5}`). Receipts are JSON **lists**.
5. **Immediate sends:** `... send-now --batch-id <id>`, then confirm the printed counts show `sent N, failed 0`.
6. **Report the batch id** back to the user — it is the handle for status/cancel later.

## Flag decision table

| Situation | Flags |
|---|---|
| **Campaign / outreach to new contacts** | `--tracker <the user's contact-history CSV>` (needs an `email` column). A recipient already in it is a HARD error — that is the point: remove them from the batch, submit the rest, and tell the user who was dropped. Only use `--allow-recontact` if the user explicitly says so. |
| **Known contact / reply / one-off** | `--no-tracker-check` — a relationship email is not a campaign. |
| **Sending files** | `--attach FILE` (repeatable; ≤10 files, ≤20MB total). The CLI pre-flights the server version and refuses if too old — it never silently sends without the attachment. |
| **Server-side default attachment** (if the user configured one, e.g. a résumé or brochure) | attaches automatically; pass `--no-default-attach` when it would be inappropriate for this recipient. |
| **Send right now** | `--send-at '+2m'` then `send-now` with the batch id. |
| **Scheduled** | `--send-at "YYYY-MM-DD 09:00" --tz ET` (ET/CT/MT/PT are DST-aware; CN for China; fixed offsets and ISO work too). Prefer absolute times so a crashed submit can be retried idempotently. |

## Cold / bulk lists: verify addresses first

When recipients come from scraping, guessing, or any source the user can't personally vouch for, pre-flight the list — bounces quietly damage the user's sender reputation:

```bash
python3 ~/.claude/skills/send-email/scripts/verify_emails.py --in candidates.json \
    --out verified.json --helo <domain the user controls> --probe-from <the user's address>
```

No mail is sent (the SMTP conversation stops at RCPT). Proven-bad addresses (`invalid` / `no_mx`) are dropped into `verified.rejects.json` — tell the user who was dropped; `verified.json` feeds straight into a batch. Skip this step for addresses the user handed you directly. It needs outbound port 25, which home/cloud networks often block — if every verdict is `unknown (conn:...)`, report that to the user instead of retrying.

## Copy rules (the validator rejects violations)

- **Body ≥ 200 characters** — a "quick one-liner" still needs a real, complete paragraph.
- **No template placeholders** like `[Company]`, `{First Name}`, `<NAME>` — hard-rejected as failed mail-merge. Deliberately showing a template for review? Use guard-safe wording like "[their company]".
- **No empty greetings** ("Hi ,") — greet a name or use "Hi there,".
- **Subject:** single line, ≤150 chars. Replies reuse the exact original subject with `Re: ` so they thread.
- Unsure? `... validate --batch <file>` runs every check without submitting.

## Send authorization (non-negotiable)

Sending email is an outward-facing, irreversible act. Send immediately only when the user explicitly told you to send **this** message ("send it"). Otherwise compose it, show them the final text, and wait. Approval for one email does not roll over to the next. Never guess or invent a recipient address — get it from the user or their files.

## Verify + recover

- `status [--verbose]` shows every batch; `cancel --batch-id <id> --trash-drafts`; `send-now`.
- Submit died mid-way? Re-run the **same command** (server-side `client_key` dedupes) — but only with an **absolute** `--send-at`; a remainder file holds exactly the un-submitted emails.
- `sent_assumed` in status = the draft vanished (probably sent) — check Gmail Sent before any re-send.
- Rehearse safely: `python3 scripts/mock_server.py 8787` + point `AUTO_EMAIL_CONFIG` at a temp config initialized against `http://127.0.0.1:8787/exec` (secret `testsecret-123`).
