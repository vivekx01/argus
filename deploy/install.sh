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
mkdir -p data/papers data/downloads notes reports ~/.config/systemd/user

# guest sandbox: outside the repo so no CLAUDE.md, settings, db or MCP config is visible to guest chats
mkdir -p ~/argus-guest && cp deploy/guest-CLAUDE.md ~/argus-guest/CLAUDE.md

# retired units (daily videos were removed): stop them and drop their dangling links
for u in argus-daily.timer argus-daily.service; do
  systemctl --user disable --now "$u" 2>/dev/null || true
  [ -e deploy/systemd/"$u" ] || rm -f ~/.config/systemd/user/"$u"
done

for f in deploy/systemd/*; do
  ln -sf "$PWD/$f" ~/.config/systemd/user/"$(basename "$f")"
done
systemctl --user daemon-reload
systemctl --user enable --now argus-fetch.timer argus-triage.timer argus-brief.timer argus-weekly.timer argus-backup.timer argus-watchdigest.timer
systemctl --user enable argus-bot.service argus-web.service
systemctl --user restart argus-bot.service argus-web.service

echo "Argus installed."
systemctl --user list-timers 'argus-*' --no-pager
