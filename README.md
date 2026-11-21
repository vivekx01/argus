# Argus 👁

The hundred-eyed watcher: an autonomous **AI + finance news scout and researcher**. It is built as a harness around [Claude Code](https://claude.com/claude-code) and runs on a home server.

Argus watches about 40 sources around the clock and has Claude judge what matters. It messages you on Telegram with breaking alerts, a morning brief and weekly trends. It also does deep research on request, including reading full research papers.

## Architecture

| Part | What | Where |
|---|---|---|
| 🧠 Brain | Claude Code, run headless (`claude -p`) on a Claude subscription | `argus/lib.py` → `claude()` |
| 👀 Senses | RSS/API watchers that use no AI, deduplicate stories, and treat many outlets carrying the same story as a signal | `argus/sources.py`, `jobs.py fetch` |
| ⏰ Heartbeat | systemd user timers | `deploy/systemd/` |
| 💾 Memory | SQLite (items, scores, votes, runs) plus notes Argus maintains itself | `data/`, `notes/` (server only) |
| 🗣 Voice | Telegram bot that answers only the owner's chat ID | `argus/bot.py` |
| 📜 Identity | Mission, rules, rubric | `CLAUDE.md`, `prompts/` |
| 🛡 Guardrails | Tool allowlist/denylist; can't edit its own wiring or read secrets | `.claude/settings.json` |

```
 sources ──fetch (every 30 min)──▶ SQLite ──triage (6x/day)──▶ 🚨 alerts (score ≥ 9)
                                     │                    └──▶ scores, clusters, hooks
                                     ├──brief (07:30 IST)───▶ ☀️ top 10 + best video of the day
                                     └──weekly (Sun)────────▶ 📈 themes, video ideas, updates notes/taste.md
 Telegram ⇄ bot.py ──▶ chat, /research, 👍/👎, 🔎 dig deeper
```

## Schedule (IST)
- **fetch:** every 30 min
- **triage:** 00:05, 09:05, 12:05, 15:05, 18:05, 21:05
- **brief:** 07:30 (runs a triage first)
- **weekly:** Sunday 10:00

## Telegram commands
`/research <topic>` · `/scan` · `/brief` · `/status` · `/new` · or just talk to it.

## Server setup
Run these as the `argus` user, with Claude Code installed at `~/.local/bin/claude` and a venv at `~/venv`:

```bash
git clone git@github.com:vivekx01/argus.git ~/argus
# secrets (never in git):
#   ~/.argus/env           export CLAUDE_CODE_OAUTH_TOKEN=...   (from `claude setup-token`)
#   ~/.argus/telegram.env  TELEGRAM_BOT_TOKEN=...  OWNER_CHAT_ID=...
bash ~/argus/deploy/install.sh
```

To update: `cd ~/argus && git pull && bash deploy/install.sh`

## Manual runs
```bash
cd ~/argus
~/venv/bin/python -m argus.jobs fetch
~/venv/bin/python -m argus.jobs triage --quiet
~/venv/bin/python -m argus.jobs status
journalctl --user -u argus-bot -f
```
