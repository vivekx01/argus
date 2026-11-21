#!/usr/bin/env bash
# Install or refresh Argus on the server. Run as the `argus` user from ~/argus:
#   bash deploy/install.sh
# Idempotent: safe to re-run after every `git pull`.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f ~/.argus/env ] || { echo "missing ~/.argus/env (CLAUDE_CODE_OAUTH_TOKEN)"; exit 1; }
[ -f ~/.argus/telegram.env ] || { echo "missing ~/.argus/telegram.env"; exit 1; }

~/venv/bin/pip install -q -r requirements.txt

# Headless runs never show the trust dialog, and an untrusted workspace ignores .claude/settings.json allow rules.
# The owner set up this repo, so mark it trusted (the same thing as accepting the dialog once).
CJ=~/.claude.json; [ -f "$CJ" ] || echo '{}' > "$CJ"
jq --arg p "$PWD" '.projects[$p].hasTrustDialogAccepted = true' "$CJ" > "$CJ.tmp" && mv "$CJ.tmp" "$CJ"
chmod 600 "$CJ"
mkdir -p data/papers notes reports ~/.config/systemd/user

for f in deploy/systemd/*; do
  ln -sf "$PWD/$f" ~/.config/systemd/user/"$(basename "$f")"
done
systemctl --user daemon-reload
systemctl --user enable --now argus-fetch.timer argus-triage.timer argus-brief.timer argus-weekly.timer argus-backup.timer
systemctl --user enable argus-bot.service
systemctl --user restart argus-bot.service

echo "Argus installed."
systemctl --user list-timers 'argus-*' --no-pager
