"""Argus reads on its own, and tells the owner what's worth it.

explore : a bounded reading session on one interest, or (about 20% of the time) Argus's own curiosity next to them.
          The explorer agent reads and returns JSON. Code writes the topic note, logs the reading and queues shares.
share   : no AI. Every 15 min, sends the best queued share if the outreach budget allows (chatty, never spammy).
"""
from __future__ import annotations

import datetime as dt
import html
import json
import random
import re

from . import mind
from .lib import NOTES, OWNER, claude, db, esc, extract_json, log_run, prompt_file, send

TOPICS = NOTES / "topics"
MAX_SESSIONS_PER_DAY = 3       # Pro usage: the owner's own chats always come first
HEAVY_CHAT_TURNS = 30          # if they talked this much today, skip reading
CURIOSITY_P = 0.2
DAILY_CAP = 5                  # unprompted messages per IST day, morning brief included
MIN_GAP_MIN = 90
CHATTING_MIN = 20              # don't interrupt a live conversation
UNPROMPTED = ("brief", "share", "alert", "interest", "watch_digest", "weekly")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60] or "misc"


def _day_start() -> str:
    n = mind.now_utc().astimezone(mind.IST)
    return mind.sql_utc(n.replace(hour=0, minute=0, second=0, microsecond=0))


def sync_watches(c) -> None:
    """Every owner-approved watch is the news feed of an interest (v2: interests are the umbrella)."""
    for w in c.execute("SELECT id, name, purpose FROM watches WHERE id != 1 AND status='active'").fetchall():
        if not c.execute("SELECT 1 FROM interests WHERE watch_id=?", (w["id"],)).fetchone():
            iid, _ = mind.adopt_interest(c, w["name"], w["purpose"] or "", "watch")
            if iid:
                c.execute("UPDATE interests SET watch_id=? WHERE id=? AND watch_id IS NULL", (w["id"], iid))
    c.commit()


def _pick(c):
    ints = c.execute("SELECT * FROM interests WHERE status='active'").fetchall()
    if not ints:
        return None
    recent = {r[0] for r in c.execute("SELECT interest_id FROM readings WHERE at > datetime('now','-20 hours')")}
    pool = [i for i in ints if i["id"] not in recent] or ints
    return random.choices(pool, weights=[max(0.05, i["weight"]) for i in pool])[0]


def _news_for(c, it) -> list[dict]:
    if it is None:
        return []
    if it["watch_id"]:
        rows = c.execute("""SELECT id, title, source, score, why FROM items WHERE watch_id=? AND status='triaged'
                            AND score >= 6 AND fetched_at > datetime('now','-3 days') ORDER BY score DESC LIMIT 12""",
                         (it["watch_id"],)).fetchall()
    else:
        words = [w for w in re.findall(r"\w+", it["name"]) if len(w) > 2]
        if not words:
            return []
        q = " OR ".join(f'"{w}"' for w in words)
        rows = c.execute("""SELECT i.id, i.title, i.source, i.score, i.why FROM items_fts f JOIN items i ON i.id=f.rowid
                            WHERE items_fts MATCH ? AND i.fetched_at > datetime('now','-3 days')
                            AND COALESCE(i.score, 0) >= 5 ORDER BY i.score DESC LIMIT 10""", (q,)).fetchall()
    return [dict(r) for r in rows]


def explore(progress=None, force: bool = False) -> str:
    c = db()
    sync_watches(c)
    today = _day_start()
    if not force:
        n = c.execute("SELECT COUNT(*) n FROM readings WHERE at > ?", (today,)).fetchone()["n"]
        if n >= MAX_SESSIONS_PER_DAY:
            return "daily reading cap reached"
        turns = c.execute("SELECT COUNT(*) n FROM inbox WHERE kind='owner' AND at > ?", (today,)).fetchone()["n"]
        if turns >= HEAVY_CHAT_TURNS:
            return "owner chatted a lot today; saving usage for them"
    curious = random.random() < CURIOSITY_P
    it = None if curious else _pick(c)
    curious = it is None
    TOPICS.mkdir(parents=True, exist_ok=True)

    names = [r["name"] for r in c.execute("SELECT name FROM interests WHERE status='active' ORDER BY weight DESC")]
    if curious:
        mode = ("CURIOSITY. This session is yours: pick one subject you're genuinely curious about that sits next to "
                f"the owner's interests ({', '.join(names)}) or connects two of them, and isn't one of them already. "
                "Something they'd likely find fascinating but haven't asked about. If you've read about it before, "
                f"your note is in notes/topics/ (existing: {', '.join(p.stem for p in TOPICS.glob('*.md')) or 'none'}).")
        note, recent = "(you pick the topic; read its existing note if there is one)", ""
    else:
        mode = f"INTEREST: {it['name']}. Why they care: {it['why'] or '(not recorded)'}."
        path = TOPICS / f"{slug(it['name'])}.md"
        note = path.read_text()[:6000] if path.exists() else "(no note yet: this is your first session on it)"
        recent = "\n".join(f"- {r['at'][:10]}: {r['title']}: {r['summary']}" for r in c.execute(
            "SELECT at, title, summary FROM readings WHERE interest_id=? ORDER BY id DESC LIMIT 6", (it["id"],)))
    loops = []
    for r in c.execute("SELECT open_loops FROM conversations WHERE done=1 ORDER BY id DESC LIMIT 6"):
        loops += mind._j(r["open_loops"])
    shared = "\n".join(f"- {r['text']}" for r in c.execute(
        "SELECT text FROM share_queue WHERE created_at > datetime('now','-10 days') ORDER BY id DESC LIMIT 20"))
    owner = mind.OWNER_MD.read_text()[:3000] if mind.OWNER_MD.exists() else "(no notes yet)"

    out = claude(prompt_file("explore", MODE=mode, OWNER=owner, NOTE=note, RECENT=recent or "(none)",
                             LOOPS="\n".join(f"- {x}" for x in loops[:10]) or "(none)",
                             NEWS=json.dumps(_news_for(c, it), ensure_ascii=False) or "[]",
                             SHARED=shared or "(nothing yet)",
                             DATE=dt.datetime.now(mind.IST).strftime("%A %d %B %Y")),
                 timeout=1800, agent="explorer", label="explore", progress=progress)
    j = extract_json(out["result"])
    if not isinstance(j, dict):
        raise ValueError("explorer reply wasn't a JSON object")
    topic = it["name"] if it else " ".join(str(j.get("topic") or "misc").split())[:60]
    path = TOPICS / f"{slug(topic)}.md"
    new_note = str(j.get("note") or "").strip()
    if len(new_note) > 200:
        path.write_text(f"# {topic}\n\n" + re.sub(r"^#\s+.*\n+", "", new_note)[:8000] + "\n")
    sources = [s for s in (j.get("sources") or []) if isinstance(s, dict) and str(s.get("url", "")).startswith("http")]
    rid = c.execute("INSERT INTO readings(interest_id, topic, title, summary, sources, note_path, curiosity) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (it["id"] if it else None, topic, str(j.get("title", ""))[:200], str(j.get("summary", ""))[:1500],
                     json.dumps(sources[:12]), str(path.relative_to(NOTES.parent)), int(curious))).lastrowid
    queued = 0
    for sh in (j.get("shares") or [])[:3]:
        if not isinstance(sh, dict) or len(str(sh.get("text", ""))) < 20:
            continue
        url = str(sh.get("url") or "")
        try:
            score = max(1, min(10, int(sh.get("score", 5))))
        except (TypeError, ValueError):
            score = 5
        c.execute("INSERT INTO share_queue(reading_id, interest_id, kind, text, url, score) VALUES(?,?,?,?,?,?)",
                  (rid, it["id"] if it else None, str(sh.get("kind", "find"))[:20], str(sh["text"])[:700],
                   url if url.startswith("http") else "", score))
        queued += 1
    c.commit()
    info = f"{'curiosity' if curious else 'interest'}: {topic}: {j.get('title', '')} ({queued} share(s), {out['_seconds']}s)"
    log_run("explore", True, info[:300])
    return info


# ------------------------------------------------------------------ outreach
def _share_html(r) -> str:
    link = f' <a href="{html.escape(r["url"], quote=True)}">source</a>' if r["url"] else ""
    return f"💡 {esc(r['text'])}{link}"


def best_share(c, min_eff: float = 0):
    rows = c.execute("""SELECT s.*, COALESCE(i.weight, 0.5) w FROM share_queue s LEFT JOIN interests i ON i.id=s.interest_id
                        WHERE s.status='queued' AND COALESCE(i.status, 'active')='active'""").fetchall()
    scored = sorted(((r["score"] + 2 * (r["w"] - 0.5), r) for r in rows), key=lambda x: -x[0])
    return scored[0][1] if scored and scored[0][0] >= min_eff else None


def mark_sent(c, sid: int, mid=None) -> None:
    c.execute("UPDATE share_queue SET status='sent', sent_at=datetime('now'), tg_message_id=? WHERE id=?", (mid, sid))
    c.commit()


def share_tick() -> str:
    c = db()
    c.execute("UPDATE share_queue SET status='expired' WHERE status='queued' AND created_at < datetime('now','-2 days')")
    c.commit()
    if mind.quiet_now():
        return "quiet hours"
    today, now = _day_start(), mind.now_utc()
    marks = ",".join("?" * len(UNPROMPTED))
    sent = c.execute(f"SELECT COUNT(*) n, MAX(at) last FROM outbox WHERE chat_id=? AND kind IN ({marks}) AND at > ?",
                     (OWNER, *UNPROMPTED, today)).fetchone()
    if sent["n"] >= DAILY_CAP:
        return "daily cap reached"
    if sent["last"] and mind.from_sql(sent["last"]) > now - dt.timedelta(minutes=MIN_GAP_MIN):
        return "too soon after the last message"
    talk = c.execute("SELECT MAX(at) m FROM inbox WHERE kind='owner'").fetchone()["m"]
    if talk and mind.from_sql(talk) > now - dt.timedelta(minutes=CHATTING_MIN):
        return "owner is chatting"
    shares_today = c.execute("SELECT COUNT(*) n FROM outbox WHERE chat_id=? AND kind='share' AND at > ?",
                             (OWNER, today)).fetchone()["n"]
    r = best_share(c, min_eff=6 + shares_today)     # each extra message today needs to be better than the last
    if not r:
        return "nothing good enough queued"
    mid = send(_share_html(r), [[{"text": "👍", "callback_data": f"sh:up:{r['id']}"},
                                 {"text": "👎", "callback_data": f"sh:down:{r['id']}"},
                                 {"text": "💬 Tell me more", "callback_data": f"sh:more:{r['id']}"}]],
               True, OWNER, "share")
    mark_sent(c, r["id"], mid)
    return f"sent share #{r['id']}"


def share_reaction(action: str, sid: int) -> tuple[str, dict | None]:
    """👍/👎 on a share teaches Argus what to bring up more. Returns (toast, share row)."""
    c = db()
    r = c.execute("SELECT * FROM share_queue WHERE id=?", (sid,)).fetchone()
    if not r:
        return "That one's gone", None
    if action in ("up", "down"):
        c.execute("UPDATE share_queue SET reaction=? WHERE id=?", (1 if action == "up" else -1, sid))
        if r["interest_id"]:
            mind.engage(c, r["interest_id"], 1.0 if action == "up" else 0.0)
        c.commit()
        return ("Noted, more like this 👍" if action == "up" else "Noted, less of this 👎"), dict(r)
    return "", dict(r)


def morning_line(c) -> tuple[str, int | None]:
    """One thing Argus has been reading, for the bottom of the morning brief."""
    r = best_share(c, min_eff=6)
    if not r:
        return "", None
    link = f' <a href="{html.escape(r["url"], quote=True)}">source</a>' if r["url"] else ""
    return f"🧠 <b>Been reading:</b> {esc(r['text'])}{link}", r["id"]
