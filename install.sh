#!/usr/bin/env bash
# Install auto-email-sender as a Claude Code skill (user-level: available in every repo).
set -euo pipefail

DEST="${1:-$HOME/.claude/skills/send-email}"
SRC="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$DEST/scripts"
cp "$SRC/skills/send-email/SKILL.md" "$DEST/SKILL.md"
cp "$SRC/gmail_pipeline.py"          "$DEST/scripts/gmail_pipeline.py"
cp "$SRC/verify_emails.py"           "$DEST/scripts/verify_emails.py"
cp "$SRC/mock_server.py"             "$DEST/scripts/mock_server.py"

echo "✓ Skill installed to $DEST"
echo
if [ -f "$HOME/.config/auto-email-sender/config.json" ]; then
  echo "✓ Existing pipeline config found — you're done. Restart Claude Code and try: \"email <someone> that ...\""
else
  echo "Next (one-time, ~5 min):"
  echo "  1. Deploy Code.gs to script.google.com (see README.md, Setup section)"
  echo "  2. python3 \"$DEST/scripts/gmail_pipeline.py\" init --url '<your /exec URL>'"
  echo "  3. python3 \"$DEST/scripts/gmail_pipeline.py\" ping   # expect: pong ... (server v4)"
  echo "  4. Restart Claude Code — then just ask it to send an email."
fi
