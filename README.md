# auto-email-sender

Schedule and send Gmail from your own account, from the command line, with the actual sending running on **Google's servers** — submit a batch, close your laptop, and the emails still go out on time.

Built as a two-part system with **zero third-party dependencies**:

```
┌─────────────────┐   HTTPS (JSON + shared secret)   ┌──────────────────────────┐
│ gmail_pipeline.py│ ───────────────────────────────▶ │ Code.gs (Apps Script     │
│ local CLI        │                                  │ web app on YOUR account) │
│ python3 stdlib   │ ◀─────────────────────────────── │ drafts now, sends later  │
└─────────────────┘          status / receipts        │ via time-based triggers  │
                                                      └──────────────────────────┘
```

## Why not just use Gmail's "Schedule send"?

- No 100-scheduled-emails cap (Apps Script triggers instead).
- Real file attachments per batch (`--attach report.pdf data.csv`), plus an optional standing default attachment configured server-side.
- Rich-text (HTML) bodies generated automatically from plain text, so recipients never see the hard-wrapped 72-character "boxed" look; URLs become links. (`--plain` to opt out.)
- **Idempotent submits**: retrying a timed-out submit can never send anyone the same email twice (server-side `client_key` dedupe).
- **Contact-history guard**: point `--tracker` at a CSV of everyone you've already emailed; re-contacting any of them is a hard error unless you explicitly override.
- Validation before anything sends: duplicate recipients, empty/over-long subjects, unfilled `{First Name}`-style template placeholders, past send times, bodies suspiciously short.
- Crash-safe: incremental receipts, a remainder file with exactly the un-submitted emails, a rescue trigger, and a daily sweep that re-delivers anything a lost trigger stranded.

## Setup (~5 minutes, once)

1. Generate a secret: `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`
2. Open [script.google.com](https://script.google.com) with the Google account you send from → **New project** → paste all of `Code.gs` → save.
3. **Project Settings → Script properties** → add `SECRET` = the string from step 1.
   Optional properties: `SENDER_NAME` (display name on outgoing mail), `DEFAULT_ATTACHMENT_FILE_ID` (a Drive file id to attach to every email unless a batch opts out with `--no-default-attach`).
4. **Services** (left sidebar) → **+** → add **Gmail API**.
5. **Deploy → New deployment → Web app** → Execute as **Me**, access **Anyone with the link** → authorize → copy the `/exec` URL.
6. In the editor's function dropdown pick **`installDailySweep`** → **Run**. (This daily backstop re-delivers any batch whose trigger got lost — required, not optional.)
7. Locally:
   ```bash
   python3 gmail_pipeline.py init --url '<your /exec URL>'   # prompts for the secret
   python3 gmail_pipeline.py ping                            # expect: pong ... (server v4)
   ```

## Usage

Write a batch file (`batch.json`):

```json
[
  {"to": "someone@example.com",
   "subject": "Quarterly report",
   "body": "Hi Sam,\n\nAttached is the Q3 report we discussed. The summary tab has the highlights.\n\nBest,\nAlex"}
]
```

Then:

```bash
# schedule for a specific time (DST-aware US aliases: ET/CT/MT/PT; also CN, fixed offsets, ISO, or '+10m')
python3 gmail_pipeline.py submit --batch batch.json --send-at "2026-07-10 09:00" --tz ET \
    --no-tracker-check --label report-0710 --attach q3-report.xlsx

# or send immediately
python3 gmail_pipeline.py submit --batch batch.json --send-at '+2m' --no-tracker-check --label hello --yes
python3 gmail_pipeline.py send-now --batch-id <id printed above>

# manage
python3 gmail_pipeline.py status --verbose
python3 gmail_pipeline.py cancel --batch-id <id> --trash-drafts
```

For campaigns, keep a CSV with an `email` column of everyone already contacted and pass `--tracker contacts.csv` — the submit hard-fails if any recipient is already in it (that's the point; `--allow-recontact` downgrades to a warning).

Per-email send times are supported too (a `send_at` field on each email), so one submit can fan out across time zones.

## Testing without sending anything

```bash
python3 mock_server.py 8787   # in one terminal
AUTO_EMAIL_CONFIG=/tmp/test-config.json python3 gmail_pipeline.py init \
    --url http://127.0.0.1:8787/exec --secret testsecret-123
AUTO_EMAIL_CONFIG=/tmp/test-config.json python3 gmail_pipeline.py submit --batch test-batch.json \
    --send-at '+10m' --no-tracker-check --yes
```

The mock implements the full POST contract (idempotency included) in memory.

## Security notes

- The secret lives in Apps Script **Script properties** (server) and `~/.config/auto-email-sender/config.json` chmod 600 (local). It is never in code or in this repo.
- Someone with the URL but not the secret gets `auth failed` and nothing else.
- The server re-validates recipients (blocks comma-injected multi-recipient tricks) and only ever reads the default attachment from its own Script property, never from the request.
- Rotating the secret = change the Script property + re-run `init`. No redeploy needed.

## Files

| File | Purpose |
|---|---|
| `Code.gs` | Server: idempotent batch intake → drafts with attachments → scheduled send with rescue trigger + daily sweep → status/cancel/retry |
| `gmail_pipeline.py` | Local CLI: init / ping / validate / submit / status / cancel / send-now / convert |
| `mock_server.py` | In-memory fake server for end-to-end rehearsal |
| `test-batch.json` | Two-email smoke test (edit the addresses to your own) |
| `config.example.json` | Shape of the local config written by `init` |

MIT licensed.
