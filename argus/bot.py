"""Argus's voice and ears: a Telegram bot that obeys exactly one person (OWNER_CHAT_ID).

Everyone else is ignored silently. Heavy work runs as separate job processes so the bot stays responsive.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from telegram import Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from .jobs import brain_lock, status
from .lib import OWNER, ROOT, TG_TOKEN, ClaudeError, claude, db, esc, kv_get, kv_set, send

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)   # its INFO lines include the bot token in the URL
log = logging.getLogger("argus.bot")
ONLY_OWNER = filters.Chat(chat_id=OWNER)
PY = sys.executable

HELP = """👁 <b>Argus</b>: I watch AI and finance news so you don't have to.

Just talk to me normally, or:
/research &lt;topic&gt;: deep dive with papers and sources, plus a full report file
/scan: triage new items right now
/brief: today's brief right now
/status: what I've been doing
/new: start a fresh conversation

On alerts: 👍/👎 teach me your taste, 🔎 digs deeper."""


async def run_job(*args: str) -> None:
    """Spawn a job process; it sends its own results. We only report crashes."""
    p = await asyncio.create_subprocess_exec(PY, "-m", "argus.jobs", *args, cwd=ROOT,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    if p.returncode != 0:
        log.warning("job %s failed: %s", args, err.decode()[-500:])


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(HELP)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(status())


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔭 Scanning new items now. You'll only hear back if something is big.")
    asyncio.create_task(run_job("triage"))


async def cmd_brief(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("☀️ Putting the brief together, give me a few minutes...")
    asyncio.create_task(run_job("brief"))


async def cmd_research(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(ctx.args).strip()
    if not topic:
        await update.message.reply_text("What should I research? e.g. /research AI agents in hedge funds")
        return
    await update.message.reply_html(f"🔎 Researching <b>{esc(topic)}</b>. This can take 10–30 min, I'll send the report.")
    asyncio.create_task(run_job("research", topic))


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = db(); kv_set(c, "chat_session", "")
    await update.message.reply_text("🧹 Fresh conversation started.")


def _chat_turn(text: str) -> str:
    c = db()
    sid = kv_get(c, "chat_session") or None
    prompt = (f"[Telegram message from the owner]\n{text}\n\n"
              "Reply for Telegram (HTML subset only: <b>, <i>, <a href>, <code>, <pre>; no markdown). Be concise.")
    with brain_lock(wait_s=900):
        try:
            out = claude(prompt, timeout=1500, resume=sid)
        except ClaudeError:
            if not sid:
                raise
            out = claude(prompt, timeout=1500)          # stale session: start over
    kv_set(c, "chat_session", out.get("session_id", ""))
    return out["result"].strip()


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_chat_action(OWNER, "typing")
    try:
        reply = await asyncio.to_thread(_chat_turn, update.message.text)
    except TimeoutError:
        reply = "⏳ I'm busy with another job (triage/research). Try again in a few minutes."
    except ClaudeError as e:
        reply = f"⚠️ Brain error: {esc(str(e)[:400])}"
    await asyncio.to_thread(send, reply)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q.from_user or q.from_user.id != OWNER:
        return
    action, _, sid = (q.data or "").partition(":")
    item_id = int(sid) if sid.isdigit() else 0
    c = db()
    row = c.execute("SELECT title, url FROM items WHERE id=?", (item_id,)).fetchone()
    if action in ("up", "down"):
        c.execute("INSERT INTO feedback(item_id, vote) VALUES(?,?)", (item_id, 1 if action == "up" else -1))
        c.commit()
        await q.answer("Noted 👍" if action == "up" else "Noted, fewer like this 👎")
    elif action == "dig" and row:
        await q.answer("Digging in 🔎")
        await ctx.bot.send_message(OWNER, f"🔎 Digging deeper into: {row['title']}")
        asyncio.create_task(run_job("research", f"{row['title']} ({row['url']}): what happened, why it matters, "
                                                "second-order effects, and video angles"))
    else:
        await q.answer()


def main():
    if not TG_TOKEN or not OWNER:
        sys.exit("missing TELEGRAM_BOT_TOKEN / OWNER_CHAT_ID in ~/.argus/telegram.env")
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_help, filters=ONLY_OWNER))
    app.add_handler(CommandHandler("status", cmd_status, filters=ONLY_OWNER))
    app.add_handler(CommandHandler("scan", cmd_scan, filters=ONLY_OWNER))
    app.add_handler(CommandHandler("brief", cmd_brief, filters=ONLY_OWNER))
    app.add_handler(CommandHandler("research", cmd_research, filters=ONLY_OWNER))
    app.add_handler(CommandHandler("new", cmd_new, filters=ONLY_OWNER))
    app.add_handler(MessageHandler(ONLY_OWNER & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Argus bot up, owner=%s", OWNER)
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
