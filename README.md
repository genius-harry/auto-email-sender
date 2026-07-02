# auto-email-sender

**Give your AI agent the ability to send email — safely.** A Gmail sending pipeline where the actual sending runs on **Google's servers** (submit and close your laptop), designed so that an agent operating it unsupervised is hard-pressed to embarrass you. Ships as a ready-to-install **Claude Code skill**, and doubles as a zero-dependency CLI for humans.

```
┌────────────────────┐   HTTPS (JSON + shared secret)   ┌──────────────────────────┐
│ your agent / you    │ ───────────────────────────────▶ │ Code.gs (Apps Script     │
│ gmail_pipeline.py   │                                  │ web app on YOUR account) │
│ (python3 stdlib)    │ ◀─────────────────────────────── │ drafts now, sends later  │
└────────────────────┘          status / receipts        │ via time-based triggers  │
                                                         └──────────────────────────┘
```

## Why agents need this exact shape

Handing an LLM agent a raw "send email" function is asking for trouble. This pipeline is built agent-first:

- **Idempotent submits** — an agent that retries a timed-out request can never double-send (server-side `client_key` dedupe). Crash mid-batch? Re-run the same command; already-sent chunks are skipped.
- **A validator that catches classic agent mistakes** before anything sends: unfilled `{First Name}` / `[Company]` template placeholders, empty greetings ("Hi ,"), duplicate recipients, past send times, suspiciously short bodies, newlines in subjects.
- **Contact-history guard** — point `--tracker` at a CSV of everyone already emailed and re-contacting any of them becomes a hard error, not a silent spam incident.
- **Attachment integrity** — the CLI verifies the server confirmed every attachment; it refuses (with instructions) rather than silently sending without files.
- **Receipts and batch ids for everything** — every send is auditable and cancellable (`status` / `cancel` / `send-now`).
- **An in-memory mock server** — agents (and their test harnesses) can rehearse the full flow with zero risk of real email.

The bundled skill also teaches the agent etiquette: show the user the final text and get an explicit "send it" before anything leaves.

## Install as a Claude Code skill (~5 min)

```bash
git clone https://github.com/genius-harry/auto-email-sender.git
cd auto-email-sender
./install.sh          # copies the skill + CLI to ~/.claude/skills/send-email/
```

Then the one-time server setup (below), and restart Claude Code. From then on — in **any** repo — saying *"email Sam that the report is ready, attach q3.xlsx"* triggers the skill and the agent does the rest correctly: batch JSON → validation → submit → confirmation with a batch id.

Not on Claude Code? `skills/send-email/SKILL.md` is a self-contained operating manual any agent framework can load as a tool guide, and the CLI is plain stdlib Python.

## One-time server setup (both tracks)

1. Generate a secret: `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`
2. Open [script.google.com](https://script.google.com) with the Google account you send from → **New project** → paste all of `Code.gs` → save.
3. **Project Settings → Script properties** → add `SECRET` = the string from step 1.
   Optional: `SENDER_NAME` (display name), `DEFAULT_ATTACHMENT_FILE_ID` (a Drive file attached to every email unless a batch opts out with `--no-default-attach`).
4. **Services** → **+** → add **Gmail API**.
5. **Deploy → New deployment → Web app** → Execute as **Me**, access **Anyone with the link** → authorize → copy the `/exec` URL.
6. Function dropdown → **`installDailySweep`** → **Run** (daily backstop that re-delivers anything a lost trigger stranded — required).
7. Connect and smoke-test:
   ```bash
   python3 gmail_pipeline.py init --url '<your /exec URL>'   # prompts for the secret
   python3 gmail_pipeline.py ping                            # expect: pong ... (server v4)
   # edit test-batch.json to your own addresses, then:
   python3 gmail_pipeline.py submit --batch test-batch.json --send-at '+10m' --no-tracker-check --yes
   ```

## The hardcore track: CLI for humans

```bash
# schedule for a specific time (DST-aware aliases ET/CT/MT/PT; also CN, fixed offsets, ISO, '+10m')
python3 gmail_pipeline.py submit --batch batch.json --send-at "2026-07-10 09:00" --tz ET \
    --no-tracker-check --label report-0710 --attach q3-report.xlsx

# send immediately
python3 gmail_pipeline.py submit --batch batch.json --send-at '+2m' --no-tracker-check --label hello --yes
python3 gmail_pipeline.py send-now --batch-id <id printed above>

# manage
python3 gmail_pipeline.py status --verbose
python3 gmail_pipeline.py cancel --batch-id <id> --trash-drafts
```

Batch format — a JSON list (bodies are plain text; a rich-text HTML version is generated automatically so recipients never see the hard-wrapped "boxed" look; URLs become links; `--plain` opts out):

```json
[{"to": "someone@example.com",
  "subject": "Quarterly report",
  "body": "Hi Sam,\n\nAttached is the Q3 report we discussed. The summary tab has the highlights.\n\nBest,\nAlex",
  "send_at": "2026-07-10 09:00 ET"}]
```

Per-email `send_at` lets one submit fan out across time zones. For campaigns, pass `--tracker contacts.csv` (needs an `email` column) — re-contacting anyone already in it hard-fails (`--allow-recontact` downgrades to a warning).

## Testing without sending anything

```bash
python3 mock_server.py 8787   # terminal 1
AUTO_EMAIL_CONFIG=/tmp/test-config.json python3 gmail_pipeline.py init \
    --url http://127.0.0.1:8787/exec --secret testsecret-123
AUTO_EMAIL_CONFIG=/tmp/test-config.json python3 gmail_pipeline.py submit \
    --batch test-batch.json --send-at '+10m' --no-tracker-check --yes
```

The mock implements the full POST contract (idempotency included) in memory.

## Security notes

- The secret lives in Apps Script **Script properties** (server) and `~/.config/auto-email-sender/config.json` chmod 600 (local) — never in code or this repo.
- Someone with the URL but not the secret gets `auth failed` and nothing else.
- The server re-validates recipients (blocks comma-injected multi-recipient tricks) and only reads the default attachment from its own Script property, never from the request.
- Rotate the secret anytime: change the Script property + re-run `init`. No redeploy.

## Files

| File | Purpose |
|---|---|
| `Code.gs` | Server: idempotent batch intake → drafts with attachments → scheduled send with rescue trigger + daily sweep → status/cancel/retry |
| `gmail_pipeline.py` | CLI: init / ping / validate / submit / status / cancel / send-now / convert |
| `skills/send-email/SKILL.md` | The Claude Code skill: recipe, flag decision table, copy rules, authorization etiquette |
| `install.sh` | Installs the skill + CLI to `~/.claude/skills/send-email/` |
| `mock_server.py` | In-memory fake server for end-to-end rehearsal |
| `test-batch.json` | Two-email smoke test (edit the addresses to your own) |
| `config.example.json` | Shape of the local config written by `init` |

MIT licensed.
