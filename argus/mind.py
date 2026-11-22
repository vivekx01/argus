"""Argus's memory of the owner: facts, memos, interests, conversation summaries and reminders.

The chat model never writes these tables. It ends a reply with hidden markers, and code applies them:
  [[INTEREST: name | why]]     adopt a new interest (the owner is told, with a Stop button)
  [[FACT: text]]               something lasting learned about the owner
  [[REMEMBER: text]]           something the owner asked Argus to remember
  [[FORGET: id]]               drop a memo or fact
  [[REMIND: when | text]]      when (IST) = 'YYYY-MM-DD HH:MM' | 'daily HH:MM' | 'weekdays HH:MM' |
                               'weekly mon HH:MM' | 'monthly 15 HH:MM'
  [[CANCEL_REMINDER: id]]

Jobs:
  memory  : close a chat session idle for SESSION_IDLE_H and summarize it (Haiku). Every 20 min, cheap no-op if idle.
  reflect : nightly. Rewrites notes/owner.md from facts, memos, interests and recent conversations (Sonnet).
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import re
import time
from zoneinfo import ZoneInfo

from .lib import NOTES, OWNER, claude, db, esc, extract_json, kv_get, kv_set, log_run, prompt_file, send

IST = ZoneInfo("Asia/Kolkata")
UTC = dt.timezone.utc
SQL_FMT = "%Y-%m-%d %H:%M:%S"
OWNER_MD = NOTES / "owner.md"
SESSION_IDLE_H = 3
QUIET = (dt.time(22, 30), dt.time(7, 30))      # IST: no unprompted messages (reminders the owner set still go)
NO_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Task",
            "NotebookEdit", "TodoWrite"]
MARKER = re.compile(r"\[\[(INTEREST|FACT|REMEMBER|FORGET|REMIND|CANCEL_REMINDER):\s*(.*?)\]\]", re.S)


# ------------------------------------------------------------------ time helpers
def sql_utc(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime(SQL_FMT)


def from_sql(s: str) -> dt.datetime:
    return dt.datetime.strptime(s[:19], SQL_FMT).replace(tzinfo=UTC)


def ist_label(t: dt.datetime) -> str:
    return t.astimezone(IST).strftime("%a %d %b, %H:%M IST")


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def quiet_now() -> bool:
    t = now_utc().astimezone(IST).time()
    return t >= QUIET[0] or t < QUIET[1]


# ------------------------------------------------------------------ reminders: parsing
DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ONCE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T]+(\d{1,2}):(\d{2})")
RULE = re.compile(r"^(daily|weekdays|weekly\s+(mon|tue|wed|thu|fri|sat|sun)[a-z]*|monthly\s+(\d{1,2}))\s+"
                  r"(\d{1,2}):(\d{2})$", re.I)


def next_due(rule: str, after: dt.datetime) -> dt.datetime:
    """Next time a recurring rule fires strictly after `after`."""
    m = RULE.match(" ".join(rule.lower().split()))
    if not m:
        raise ValueError(f"I can't read the repeat rule {rule!r}")
    hh, mm = int(m.group(4)), int(m.group(5))
    if hh > 23 or mm > 59:
        raise ValueError("that isn't a valid time")
    a = after.astimezone(IST)
    kind = m.group(1).split()[0]
    if kind == "monthly":
        dom = int(m.group(3))
        if not 1 <= dom <= 31:
            raise ValueError("that isn't a valid day of the month")
        y, mo = a.year, a.month
        for _ in range(14):
            try:
                cand = dt.datetime(y, mo, dom, hh, mm, tzinfo=IST)
                if cand > a:
                    return cand
            except ValueError:            # e.g. the 31st in a 30-day month: skip that month
                pass
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        raise ValueError("couldn't find the next date")
    for d in range(0, 9):
        day = (a + dt.timedelta(days=d)).date()
        cand = dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)
        if cand <= a:
            continue
        if kind == "weekdays" and cand.weekday() >= 5:
            continue
        if kind == "weekly" and cand.weekday() != DOW.index(m.group(2).lower()):
            continue
        return cand
    raise ValueError("couldn't find the next date")


def parse_when(s: str, now: dt.datetime | None = None) -> tuple[dt.datetime, str]:
    """'2026-11-23 09:00' (IST) → (due, ''); 'weekdays 09:30' → (next due, rule)."""
    s, now = " ".join((s or "").split()), now or now_utc()
    m = ONCE.match(s)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            t = dt.datetime(y, mo, d, h, mi, tzinfo=IST)
        except ValueError:
            raise ValueError(f"{s!r} isn't a real date and time")
        if t < now - dt.timedelta(minutes=2):
            raise ValueError(f"{ist_label(t)} is already in the past")
        return t, ""
    if RULE.match(s):
        rule = s.lower()
        return next_due(rule, now), rule
    raise ValueError(f"I couldn't read the time {s!r}")


# ------------------------------------------------------------------ writes (only ever from code)
def add_memory(c, kind: str, text: str, source: str) -> int | None:
    text = " ".join((text or "").split())[:500]
    if len(text) < 3:
        return None
    for r in c.execute("SELECT id, text FROM memories WHERE kind=? AND active=1", (kind,)).fetchall():
        if difflib.SequenceMatcher(None, r["text"].lower(), text.lower()).ratio() > 0.85:
            return r["id"]          # already known
    return c.execute("INSERT INTO memories(kind, text, source) VALUES(?,?,?)", (kind, text, source)).lastrowid


def engage(c, interest_id: int, amount: float = 1.0) -> None:
    """Exponential moving average toward recent engagement: w' = clamp(0.7w + 0.3e, 0.05, 1)."""
    c.execute("UPDATE interests SET weight = MIN(1.0, MAX(0.05, 0.7*weight + 0.3*?)), last_engaged=datetime('now') "
              "WHERE id=?", (amount, interest_id))


def adopt_interest(c, name: str, why: str = "", origin: str = "chat") -> tuple[int | None, bool]:
    """Returns (id, is_new). A subject the owner dropped is never re-adopted silently."""
    name = " ".join((name or "").split())[:60].strip(" .")
    if len(name) < 2:
        return None, False
    r = c.execute("SELECT id, status FROM interests WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if r:
        if r["status"] == "active":
            engage(c, r["id"])
        return r["id"], False
    iid = c.execute("INSERT INTO interests(name, why, origin, weight, last_engaged) VALUES(?,?,?,0.6,datetime('now'))",
                    (name, (why or "")[:300], origin)).lastrowid
    return iid, True


def add_reminder(c, text: str, due: dt.datetime, rule: str = "") -> int:
    return c.execute("INSERT INTO reminders(text, due, rule) VALUES(?,?,?)",
                     (" ".join(text.split())[:500], sql_utc(due), rule)).lastrowid


def _interest_note(name: str, iid: int) -> tuple[str, dict]:
    return (f"📌 <i>New interest: <b>{esc(name)}</b>. I'll start reading up on it. Say stop if not.</i>",
            {"text": f"🚫 Stop following {name[:24]}", "callback_data": f"int:stop:{iid}"})


def apply_markers(reply: str) -> tuple[str, list]:
    """Apply and strip the memory markers in an owner chat reply. Returns (reply, extra button rows)."""
    found = MARKER.findall(reply or "")
    if not found:
        return reply, []
    reply = re.sub(r"\n{3,}", "\n\n", MARKER.sub("", reply)).strip()
    c = db()
    notes, rows = [], []
    for kind, body in found:
        body = " ".join(body.split())
        try:
            if kind == "INTEREST":
                name, _, why = body.partition("|")
                iid, new = adopt_interest(c, name, why.strip(), "chat")
                if new:
                    n, b = _interest_note(name.strip(), iid)
                    notes.append(n); rows.append([b])
            elif kind == "FACT":
                add_memory(c, "fact", body, "chat")
            elif kind == "REMEMBER":
                add_memory(c, "memo", body, "chat")
            elif kind == "FORGET":
                mid = body.lstrip("#").strip()
                if mid.isdigit():
                    c.execute("UPDATE memories SET active=0 WHERE id=?", (int(mid),))
            elif kind == "REMIND":
                when, _, what = body.partition("|")
                if not what.strip():
                    raise ValueError("there was nothing to remind you about")
                due, rule = parse_when(when)
                rid = add_reminder(c, what, due, rule)
                notes.append(f"⏰ <i>Reminder set: {ist_label(due)}{f' (repeats {esc(rule)})' if rule else ''}. "
                             f"{esc(what.strip())}</i>")
                rows.append([{"text": "❌ Cancel reminder", "callback_data": f"rem:cancel:{rid}"}])
            elif kind == "CANCEL_REMINDER":
                rid = body.lstrip("#").strip()
                if rid.isdigit():
                    c.execute("UPDATE reminders SET status='cancelled' WHERE id=?", (int(rid),))
        except ValueError as e:
            notes.append(f"⚠️ <i>I couldn't set that reminder: {esc(str(e))}. What time should it be?</i>")
    c.commit()
    if notes:
        reply = (reply + "\n\n" if reply else "") + "\n".join(notes)
    return reply or "👌", rows


# ------------------------------------------------------------------ reminders: delivery (bot calls this every 30s)
def send_due_reminders() -> int:
    c = db()
    now = now_utc()
    rows = c.execute("SELECT * FROM reminders WHERE status='pending' AND due <= ? ORDER BY due LIMIT 10",
                     (sql_utc(now),)).fetchall()
    for r in rows:
        due = from_sql(r["due"])
        # advance or close it before sending, so a crash can never re-send in a loop
        if r["rule"]:
            c.execute("UPDATE reminders SET due=?, sent_at=datetime('now') WHERE id=?",
                      (sql_utc(next_due(r["rule"], max(now, due))), r["id"]))
        else:
            c.execute("UPDATE reminders SET status='sent', sent_at=datetime('now') WHERE id=?", (r["id"],))
        c.commit()
        late = now - due > dt.timedelta(minutes=10)
        msg = f"⏰ {esc(r['text'])}" + (f"\n<i>(Late: this was due {ist_label(due)}. I was offline.)</i>" if late else "")
        send(msg, [[{"text": "✅ Done", "callback_data": f"rem:done:{r['id']}"},
                    {"text": "⏰ In 1h", "callback_data": f"rem:s1h:{r['id']}"},
                    {"text": "📅 Tomorrow", "callback_data": f"rem:tmr:{r['id']}"}]], True, OWNER, "reminder")
    return len(rows)


def reminder_button(action: str, rid: int) -> str:
    """Handle a reminder button. Returns a short toast for the owner."""
    c = db()
    r = c.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
    if not r:
        return "That reminder is gone"
    now = now_utc()
    if action == "cancel":
        c.execute("UPDATE reminders SET status='cancelled' WHERE id=?", (rid,))
        msg = "Cancelled"
    elif action == "done":
        if not r["rule"]:
            c.execute("UPDATE reminders SET status='done' WHERE id=?", (rid,))
        msg = "Done ✅"
    elif action in ("s1h", "tmr"):
        if action == "s1h":
            due = now + dt.timedelta(hours=1)
        else:   # tomorrow, at the clock time it was originally for
            base = from_sql(r["sent_at"] or r["due"]).astimezone(IST) if r["rule"] else from_sql(r["due"]).astimezone(IST)
            day = (now.astimezone(IST) + dt.timedelta(days=1)).date()
            due = dt.datetime(day.year, day.month, day.day, base.hour, base.minute, tzinfo=IST)
        if r["rule"]:      # keep the repeating one as is; snooze a one-off copy
            add_reminder(c, r["text"], due)
        else:
            c.execute("UPDATE reminders SET status='pending', due=? WHERE id=?", (sql_utc(due), rid))
        msg = f"Okay, {ist_label(due)}"
    else:
        return ""
    c.commit()
    return msg


def drop_interest(iid: int) -> str:
    c = db()
    r = c.execute("SELECT name FROM interests WHERE id=?", (iid,)).fetchone()
    if not r:
        return "Not found"
    c.execute("UPDATE interests SET status='dropped' WHERE id=?", (iid,))
    c.commit()
    return f"Okay, dropped {r['name']}"


# ------------------------------------------------------------------ context for a new chat session
def _j(s: str | None) -> list:
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


def context_pack(c) -> str:
    """What Argus should know walking into a new conversation: who the owner is and where things stand."""
    parts = []
    if OWNER_MD.exists() and OWNER_MD.read_text().strip():
        parts.append("[Your notes on the owner (notes/owner.md). Use them naturally; never recite them.]\n"
                     + OWNER_MD.read_text()[:4500])
        since = kv_get(c, "last_reflect", "1970-01-01") or "1970-01-01"
        facts = c.execute("SELECT text FROM memories WHERE kind='fact' AND active=1 AND at > ? ORDER BY id DESC LIMIT 20",
                          (since,)).fetchall()
        head = "[Learned about them since those notes]"
    else:
        facts = c.execute("SELECT text FROM memories WHERE kind='fact' AND active=1 ORDER BY id DESC LIMIT 40").fetchall()
        head = "[What you know about the owner so far]"
    if facts:
        parts.append(head + "\n" + "\n".join(f"- {r['text']}" for r in reversed(facts)))
    ints = c.execute("SELECT name, why, weight FROM interests WHERE status='active' ORDER BY weight DESC").fetchall()
    if ints:
        parts.append("[Their interests, most engaged first]\n"
                     + "\n".join(f"- {r['name']} ({r['weight']:.1f}){': ' + r['why'] if r['why'] else ''}" for r in ints))
    memos = c.execute("SELECT id, text, at FROM memories WHERE kind='memo' AND active=1 ORDER BY id DESC LIMIT 25").fetchall()
    if memos:
        parts.append("[Things they asked you to remember]\n"
                     + "\n".join(f"- #{r['id']} ({r['at'][:10]}) {r['text']}" for r in reversed(memos)))
    convs = c.execute("SELECT started, summary, open_loops FROM conversations WHERE done=1 AND COALESCE(summary,'')!='' "
                      "ORDER BY id DESC LIMIT 3").fetchall()
    if convs:
        lines = ["[Your last conversations with them, oldest first. Pick threads back up when it's natural.]"]
        for r in reversed(convs):
            when = from_sql(r["started"]).astimezone(IST).strftime("%a %d %b")
            loops = _j(r["open_loops"])
            lines.append(f"- {when}: {r['summary']}" + (f" Open: {'; '.join(loops)}" if loops else ""))
        parts.append("\n".join(lines))
    reads = c.execute("SELECT topic, title, summary, note_path, at FROM readings ORDER BY id DESC LIMIT 5").fetchall()
    if reads:
        parts.append("[Your own recent reading sessions. Full topic notes are in notes/topics/; Read one when the topic "
                     "comes up]\n" + "\n".join(f"- {r['at'][:10]} {r['topic']}: {r['title']}. {r['summary']} ({r['note_path']})"
                                            for r in reversed(reads)))
    shares = c.execute("SELECT text, sent_at, reaction FROM share_queue WHERE status='sent' ORDER BY id DESC LIMIT 5").fetchall()
    if shares:
        react = {1: " (they liked it)", -1: " (they didn't care for it)"}
        parts.append("[Things you recently texted them on your own]\n" + "\n".join(
            f"- {r['sent_at'][:16]} UTC: {r['text']}{react.get(r['reaction'], '')}" for r in reversed(shares)))
    rems = c.execute("SELECT id, text, due, rule FROM reminders WHERE status='pending' ORDER BY due LIMIT 10").fetchall()
    if rems:
        parts.append("[Upcoming reminders]\n" + "\n".join(
            f"- #{r['id']} {ist_label(from_sql(r['due']))}{' (' + r['rule'] + ')' if r['rule'] else ''}: {r['text']}"
            for r in rems))
    return "\n\n".join(parts)


# ------------------------------------------------------------------ sessions → conversation summaries
def get_meta(c) -> dict:
    try:
        m = json.loads(kv_get(c, "chat_meta", "{}") or "{}")
        return m if isinstance(m, dict) else {}
    except json.JSONDecodeError:
        return {}


def close_session(meta: dict) -> bool:
    """Queue a finished chat session for summarizing. Idempotent per session id."""
    sid = meta.get("sid")
    if not sid:
        return False
    c = db()
    last = meta.get("last_ts") or time.time()
    prev = c.execute("SELECT MAX(ended) m FROM conversations").fetchone()["m"] or ""
    started = meta.get("started") or max(sql_utc(dt.datetime.fromtimestamp(last - 6 * 3600, UTC)), prev)
    ended = sql_utc(dt.datetime.fromtimestamp(last + 120, UTC))
    c.execute("INSERT OR IGNORE INTO conversations(sid, started, ended, turns) VALUES(?,?,?,?)",
              (sid, started, ended, meta.get("turns", 0)))
    c.commit()
    return True


def close_idle_session() -> bool:
    c = db()
    meta = get_meta(c)
    if not meta.get("sid") or time.time() - meta.get("last_ts", 0) < SESSION_IDLE_H * 3600:
        return False
    close_session(meta)
    cur = get_meta(db())
    if cur.get("sid") == meta["sid"]:           # the owner didn't start talking again meanwhile
        kv_set(c, "chat_meta", json.dumps({"last_outbox": cur.get("last_outbox", 0)}))
    return True


def pending_work() -> bool:
    c = db()
    meta = get_meta(c)
    idle = bool(meta.get("sid")) and time.time() - meta.get("last_ts", 0) >= SESSION_IDLE_H * 3600
    return idle or bool(c.execute("SELECT 1 FROM conversations WHERE done=0 LIMIT 1").fetchone())


def _transcript(c, started: str, ended: str, cap: int = 24000) -> str:
    rows = [(r["at"], "Owner", r["text"]) for r in c.execute(
        "SELECT at, text FROM inbox WHERE kind='owner' AND at BETWEEN ? AND ?", (started, ended))]
    rows += [(r["at"], "Argus", r["text"]) for r in c.execute(
        "SELECT at, text FROM outbox WHERE kind='chat' AND chat_id=? AND at BETWEEN ? AND ?", (OWNER, started, ended))]
    rows.sort(key=lambda x: x[0])
    text = "\n".join(f"{who}: {' '.join((t or '').split())[:1500]}" for _, who, t in rows)
    return text[-cap:]


def summarize_sessions() -> str:
    """Close an idle session, then summarize every pending one: summary, open loops, facts, interests."""
    close_idle_session()
    c = db()
    done, new_interests = 0, []
    for conv in c.execute("SELECT * FROM conversations WHERE done=0 ORDER BY id LIMIT 5").fetchall():
        tx = _transcript(c, conv["started"], conv["ended"])
        if len(tx) < 40:
            c.execute("UPDATE conversations SET done=1, summary='' WHERE id=?", (conv["id"],))
            c.commit()
            continue
        names = ", ".join(r["name"] for r in c.execute("SELECT name FROM interests ORDER BY weight DESC"))
        facts = "\n".join(f"- {r['text']}" for r in c.execute(
            "SELECT text FROM memories WHERE kind='fact' AND active=1 ORDER BY id DESC LIMIT 60"))
        out = claude(prompt_file("remember", INTERESTS=names or "(none)", FACTS=facts or "(none)",
                                 WHEN=from_sql(conv["started"]).astimezone(IST).strftime("%a %d %b %Y"),
                                 TRANSCRIPT=tx),
                     timeout=600, model="haiku", disallow=NO_TOOLS, label="memory")
        j = extract_json(out["result"])
        if not isinstance(j, dict):
            raise ValueError("memory summary wasn't a JSON object")
        c.execute("UPDATE conversations SET summary=?, topics=?, open_loops=?, done=1 WHERE id=?",
                  (str(j.get("summary", ""))[:1500], json.dumps(j.get("topics") or [])[:600],
                   json.dumps([str(x)[:200] for x in (j.get("open_loops") or [])][:6]), conv["id"]))
        for f in (j.get("facts") or [])[:10]:
            add_memory(c, "fact", str(f), "summary")
        for it in (j.get("interests") or [])[:4]:
            if isinstance(it, dict):
                iid, new = adopt_interest(c, str(it.get("name", "")), str(it.get("why", "")), "summary")
                if new:
                    new_interests.append((str(it.get("name", "")).strip(), iid))
        c.commit()
        done += 1
    if new_interests:
        q = json.loads(kv_get(c, "interest_announce", "[]") or "[]") + new_interests
        kv_set(c, "interest_announce", json.dumps(q))
    flush_announcements()
    info = f"summarized {done} conversation(s), {len(new_interests)} new interest(s)"
    log_run("memory", True, info)
    return info


def flush_announcements() -> None:
    """Tell the owner about interests picked up from chat summaries (outside quiet hours)."""
    c = db()
    q = json.loads(kv_get(c, "interest_announce", "[]") or "[]")
    if not q or quiet_now():
        return
    kv_set(c, "interest_announce", "[]")
    live = [(n, i) for n, i in q if c.execute("SELECT 1 FROM interests WHERE id=? AND status='active'", (i,)).fetchone()]
    if not live:
        return
    names = ", ".join(f"<b>{esc(n)}</b>" for n, _ in live)
    send(f"📌 From our chats I've picked up that you're into {names}. I'll start reading up. Say stop if not.",
         [[{"text": f"🚫 Stop {n[:24]}", "callback_data": f"int:stop:{i}"}] for n, i in live[:4]],
         True, OWNER, "interest")


# ------------------------------------------------------------------ nightly: rewrite notes/owner.md
def reflect() -> str:
    summarize_sessions()
    c = db()
    c.execute("UPDATE interests SET weight = MAX(0.05, weight*0.97) WHERE status='active' "
              "AND (last_engaged IS NULL OR last_engaged < datetime('now','-1 day'))")
    c.commit()
    since = kv_get(c, "last_reflect", "1970-01-01") or "1970-01-01"
    changed = c.execute("SELECT (SELECT COUNT(*) FROM memories WHERE at > ?) + "
                        "(SELECT COUNT(*) FROM conversations WHERE done=1 AND ended > ?) n", (since, since)).fetchone()["n"]
    if not changed and OWNER_MD.exists():
        return "owner notes: nothing new"
    facts = "\n".join(f"#{r['id']} ({r['at'][:10]}) {r['text']}" for r in c.execute(
        "SELECT id, at, text FROM memories WHERE kind='fact' AND active=1 ORDER BY id"))
    memos = "\n".join(f"- {r['text']}" for r in c.execute(
        "SELECT text FROM memories WHERE kind='memo' AND active=1 ORDER BY id DESC LIMIT 30"))
    ints = "\n".join(f"- {r['name']} ({r['weight']:.2f}, {r['status']}): {r['why'] or ''}" for r in c.execute(
        "SELECT name, why, weight, status FROM interests ORDER BY weight DESC"))
    convs = "\n".join(
        f"- {from_sql(r['started']).astimezone(IST):%d %b}: {r['summary']} Open: {'; '.join(_j(r['open_loops'])) or '-'}"
        for r in c.execute("SELECT started, summary, open_loops FROM conversations WHERE done=1 "
                           "AND COALESCE(summary,'')!='' ORDER BY id DESC LIMIT 12"))
    current = OWNER_MD.read_text() if OWNER_MD.exists() else "(none yet)"
    out = claude(prompt_file("owner_notes", CURRENT=current, FACTS=facts or "(none)", MEMOS=memos or "(none)",
                             INTERESTS=ints or "(none)", CONVERSATIONS=convs or "(none)"),
                 timeout=900, model="sonnet", disallow=NO_TOOLS, label="reflect")
    j = extract_json(out["result"])
    md = str((j or {}).get("owner_md", "")).strip() if isinstance(j, dict) else ""
    if len(md) < 80:
        raise ValueError("owner notes came back empty")
    OWNER_MD.write_text(md[:6000] + "\n")
    retire = [int(x) for x in (j.get("retire_fact_ids") or []) if str(x).isdigit()]
    for fid in retire:
        c.execute("UPDATE memories SET active=0 WHERE id=? AND kind='fact'", (fid,))
    kv_set(c, "last_reflect", sql_utc(now_utc()))
    info = f"owner notes rewritten ({len(md)} chars), {len(retire)} fact(s) retired"
    log_run("reflect", True, info)
    return info
