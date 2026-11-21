"""Argus jobs. Run as:  python -m argus.jobs <fetch|triage|brief|weekly|research "topic"|status>

fetch   : senses. No AI. Pull every source into SQLite.
triage  : brain. Score new items, send breaking alerts.
brief   : brain. Morning brief from the last 24h.
weekly  : brain. Trends + video ideas + update its own taste notes.
research: brain. Deep research on a topic, report saved + sent.
"""
from __future__ import annotations

import argparse
import calendar
import contextlib
import datetime as dt
import fcntl
import json
import re
import sys
import time

import feedparser
import httpx
from bs4 import BeautifulSoup

from .lib import (ARXIV_ID, DATA, HOME, NOTES, REPORTS, ClaudeError, claude, db, esc, extract_json, kv_get,
                  kv_set, log_run, norm_title, prompt_file, send, send_document, strip_tags, upsert_paper)
from .sources import SOURCES

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ArgusResearchBot/1.0 (personal news reader)"}
MAX_AGE_H = 36            # ignore items older than this on first sight
TRIAGE_BATCH = 150
ALERT_SCORE = 9
MAX_ALERTS_PER_RUN = 3


# ================================================================ brain lock
@contextlib.contextmanager
def brain_lock(wait_s: int = 1800):
    """Only one Claude job at a time: protects Pro usage limits and keeps the db sane."""
    f = open(DATA / "brain.lock", "w")
    t0 = time.time()
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() - t0 > wait_s:
                raise TimeoutError("brain busy")
            time.sleep(10)
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


# ================================================================ fetch (senses)
def _entry_time(e) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return dt.datetime.fromtimestamp(calendar.timegm(t), dt.timezone.utc)
    return None


def _rss(url: str):
    r = httpx.get(url, headers=UA, timeout=30, follow_redirects=True)
    r.raise_for_status()
    f = feedparser.parse(r.content)
    is_arxiv = "arxiv.org" in url
    for e in f.entries[:1000 if is_arxiv else 60]:
        t = _entry_time(e)
        summary = strip_tags(e.get("summary", ""))
        it = {
            "url": e.get("link", ""),
            "title": strip_tags(e.get("title", "")),
            "summary": summary[:500],
            "published": t.isoformat() if t else "",
            "_t": t,
        }
        m = ARXIV_ID.search(e.get("link", "") or e.get("id", ""))
        if is_arxiv and m:
            abstract = re.sub(r"^.*?Abstract:\s*", "", summary, flags=re.S)
            it["_paper"] = {
                "arxiv_id": m.group(1),
                "title": it["title"],
                "authors": e.get("author", "") or ", ".join(a.get("name", "") for a in e.get("authors", [])),
                "published": it["published"],
                "categories": ", ".join(tg.get("term", "") for tg in e.get("tags", [])),
                "abstract": abstract,
            }
            it["summary"] = abstract[:500]
            it["_library_only"] = True     # hundreds/day: library yes, news triage no (HF Daily Papers curates)
        yield it


def _hf_daily(url: str):
    for d in httpx.get(url, headers=UA, timeout=30).json():
        p = d.get("paper", {})
        if not p.get("id"):
            continue
        yield {
            "url": f"https://arxiv.org/abs/{p.get('id')}",
            "title": p.get("title", ""),
            "summary": f"[HF upvotes: {p.get('upvotes', 0)}] " + (p.get("summary") or "")[:450],
            "published": d.get("publishedAt", ""),
            "_t": None,
            "_paper": {
                "arxiv_id": p["id"],
                "title": p.get("title", ""),
                "authors": ", ".join(a.get("name", "") for a in p.get("authors", []) if a.get("name")),
                "published": p.get("publishedAt", ""),
                "abstract": p.get("summary", ""),
                "upvotes": int(p.get("upvotes") or 0),
            },
        }


def _github_trending(url: str):
    soup = BeautifulSoup(httpx.get(url, headers=UA, timeout=30).text, "html.parser")
    for art in soup.select("article.Box-row")[:25]:
        a = art.select_one("h2 a")
        if not a:
            continue
        repo = re.sub(r"\s+", "", a.get_text())
        desc = art.select_one("p")
        stars = art.select_one("span.d-inline-block.float-sm-right")
        yield {
            "url": f"https://github.com/{repo}",
            "title": f"{repo} (GitHub trending)",
            "summary": f"[{stars.get_text(strip=True) if stars else ''}] {desc.get_text(strip=True) if desc else ''}"[:500],
            "published": "",
            "_t": None,
        }


FETCHERS = {"rss": _rss, "hf_daily": _hf_daily, "github_trending": _github_trending}


def fetch() -> str:
    c = db()
    now = dt.datetime.now(dt.timezone.utc)
    added = dupes = papers_seen = 0
    failed = []
    try:
        health = json.loads(kv_get(c, "source_status", "{}") or "{}")
    except json.JSONDecodeError:
        health = {}
    for name, cat, kind, url in SOURCES:
        before = (added, papers_seen)
        try:
            for it in FETCHERS[kind](url):
                if not it["url"] or not it["title"]:
                    continue
                if it.get("_paper"):          # every paper we see goes into the library, even if old
                    upsert_paper(c, {**it["_paper"], "source": name})
                    papers_seen += 1
                    if it.get("_library_only"):
                        continue
                if it["_t"] and (now - it["_t"]).total_seconds() > MAX_AGE_H * 3600:
                    continue
                if c.execute("SELECT 1 FROM items WHERE url=?", (it["url"],)).fetchone():
                    continue
                n = norm_title(it["title"])
                same = c.execute("SELECT id FROM items WHERE norm=? AND fetched_at > datetime('now','-3 days')",
                                 (n,)).fetchone()
                if same:  # same headline from another outlet: a signal, not a new item
                    c.execute("UPDATE items SET seen=seen+1 WHERE id=?", (same["id"],))
                    dupes += 1
                    continue
                c.execute("INSERT OR IGNORE INTO items(url,title,norm,source,category,published,summary) "
                          "VALUES(?,?,?,?,?,?,?)",
                          (it["url"], it["title"], n, name, cat, it["published"], it["summary"]))
                added += 1
            c.commit()
            prev = health.get(name, {})
            health[name] = {"category": cat, "ok": True, "added": added - before[0], "papers": papers_seen - before[1],
                            "at": now.isoformat(timespec="seconds"), "last_ok": now.isoformat(timespec="seconds"),
                            "fails": 0, "error": "", "total": prev.get("total", 0) + added - before[0]}
        except Exception as e:
            failed.append(f"{name}: {type(e).__name__}")
            prev = health.get(name, {})
            health[name] = {**prev, "category": cat, "ok": False, "added": 0, "at": now.isoformat(timespec="seconds"),
                            "fails": prev.get("fails", 0) + 1, "error": f"{type(e).__name__}: {str(e)[:160]}"}
        time.sleep(1)  # be polite
    known = {s[0] for s in SOURCES}
    kv_set(c, "source_status", json.dumps({k: v for k, v in health.items() if k in known}))
    info = f"added={added} dupes={dupes} papers={papers_seen} failed={len(failed)} {failed}"
    log_run("fetch", True, info)
    return info


# ================================================================ triage (brain)
def _taste_context(c) -> str:
    rows = c.execute("""SELECT i.title, f.vote FROM feedback f JOIN items i ON i.id=f.item_id
                        ORDER BY f.id DESC LIMIT 40""").fetchall()
    votes = "\n".join(f"{'👍' if r['vote'] > 0 else '👎'} {r['title']}" for r in rows) or "(no votes yet)"
    taste = (NOTES / "taste.md").read_text() if (NOTES / "taste.md").exists() else "(no taste notes yet)"
    return f"## Owner's recent votes\n{votes}\n\n## Taste notes\n{taste}"


def triage(quiet: bool = False) -> str:
    c = db()
    # anything older than 2 days that was never triaged is stale
    c.execute("UPDATE items SET status='skipped' WHERE status='new' AND fetched_at < datetime('now','-2 days')")
    rows = c.execute("SELECT * FROM items WHERE status='new' ORDER BY id DESC LIMIT ?", (TRIAGE_BATCH,)).fetchall()
    if not rows:
        return "nothing new"
    recent_alerts = c.execute("SELECT title FROM items WHERE alerted=1 AND fetched_at > datetime('now','-3 days')").fetchall()
    items = [{"id": r["id"], "src": r["source"], "cat": r["category"], "seen": r["seen"],
              "title": r["title"], "summary": (r["summary"] or "")[:280]} for r in rows]
    prompt = prompt_file(
        "triage",
        ITEMS=json.dumps(items, ensure_ascii=False),
        TASTE=_taste_context(c),
        RECENT_ALERTS="\n".join(f"- {r['title']}" for r in recent_alerts) or "(none)",
        NOW=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    out = claude(prompt, timeout=1200)
    verdicts = extract_json(out["result"])
    by_id = {r["id"]: r for r in rows}
    alerts = []
    for v in verdicts:
        iid = v.get("id")
        if iid not in by_id:
            continue
        c.execute("UPDATE items SET status='triaged', score=?, why=?, hook=?, cluster=? WHERE id=?",
                  (int(v.get("score", 0)), v.get("why", ""), v.get("hook", ""), v.get("cluster", ""), iid))
        if v.get("alert") and int(v.get("score", 0)) >= ALERT_SCORE:
            alerts.append(v)
    # anything the model didn't return still counts as seen
    c.execute(f"UPDATE items SET status='triaged', score=COALESCE(score,0) WHERE status='new' AND id IN "
              f"({','.join(str(i) for i in by_id)})")
    c.commit()

    alerts.sort(key=lambda v: -int(v.get("score", 0)))
    for v in alerts[:MAX_ALERTS_PER_RUN]:
        r = by_id[v["id"]]
        c.execute("UPDATE items SET alerted=1 WHERE id=?", (r["id"],))
        c.commit()
        if quiet:
            continue
        msg = (f"🚨 <b>{esc(r['title'])}</b>\n"
               f"<i>{esc(r['source'])} · score {v.get('score')}/10</i>\n\n"
               f"{esc(v.get('why', ''))}\n\n"
               f"🎬 <b>Hook:</b> {esc(v.get('hook', ''))}\n\n"
               f"<a href=\"{esc(r['url'])}\">Read source</a>")
        send(msg, buttons=[[{"text": "👍", "callback_data": f"up:{r['id']}"},
                            {"text": "👎", "callback_data": f"down:{r['id']}"},
                            {"text": "🔎 Dig deeper", "callback_data": f"dig:{r['id']}"}],
                           [{"text": "🎬 Make a video", "callback_data": f"vid:{r['id']}"}]])
    info = f"triaged={len(rows)} returned={len(verdicts)} alerts={min(len(alerts), MAX_ALERTS_PER_RUN)} ({out['_seconds']}s)"
    log_run("triage", True, info)
    return info


# ================================================================ brief (brain)
def brief() -> str:
    try:
        triage()                      # make sure the overnight items are scored
    except Exception as e:
        log_run("triage", False, f"pre-brief: {e}")
    c = db()
    rows = c.execute("""SELECT id, title, source, category, url, score, why, hook, seen, cluster FROM items
                        WHERE status='triaged' AND score >= 5 AND fetched_at > datetime('now','-26 hours')
                        ORDER BY score DESC, seen DESC LIMIT 40""").fetchall()
    if not rows:
        send("☀️ <b>Morning brief</b>\n\nQuiet night: nothing scored above 5. Argus is still watching 👁")
        return "empty"
    out = claude(prompt_file("brief", ITEMS=json.dumps([dict(r) for r in rows], ensure_ascii=False),
                             DATE=dt.date.today().strftime("%A, %d %B %Y")), timeout=1200)
    text = out["result"].strip()
    send(text)
    kv_set(c, "last_brief", json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                        "text": text}))
    info = f"items={len(rows)} ({out['_seconds']}s)"
    log_run("brief", True, info)
    return info


# ================================================================ weekly (brain)
def weekly() -> str:
    c = db()
    rows = c.execute("""SELECT id, title, source, category, score, why, cluster, seen FROM items
                        WHERE status='triaged' AND score >= 6 AND fetched_at > datetime('now','-7 days')
                        ORDER BY score DESC LIMIT 120""").fetchall()
    out = claude(prompt_file("weekly", ITEMS=json.dumps([dict(r) for r in rows], ensure_ascii=False),
                             TASTE=_taste_context(c)), timeout=1800)
    send(out["result"].strip())
    log_run("weekly", True, f"items={len(rows)} ({out['_seconds']}s)")
    return "ok"


# ================================================================ research (brain)
def research(topic: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:50] or "topic"
    path = REPORTS / f"{dt.date.today():%Y-%m-%d}-{slug}.md"
    c = db()
    rid = c.execute("INSERT INTO reports(topic, path) VALUES(?,?)", (topic, str(path))).lastrowid
    c.commit()
    out = claude(prompt_file("research", TOPIC=topic, REPORT_PATH=str(path), REPORT_ID=rid), timeout=2400)
    result = out["result"].strip()
    c.execute("UPDATE reports SET summary=? WHERE id=?", (strip_tags(result)[:2000], rid))
    c.commit()
    send(result)
    if path.exists():
        send_document(path, caption=f"📄 Full report: {topic}")
    n = c.execute("SELECT COUNT(*) n FROM report_papers WHERE report_id=?", (rid,)).fetchone()["n"]
    log_run("research", True, f"#{rid} {topic}: {n} papers linked ({out['_seconds']}s)")
    return str(path)


# ================================================================ video (brain + CC Studio MCP)
def video(request: str) -> str:
    """Make a Reel/Short with CC Studio. `request` is a topic, or 'item:<id>' for a story in the db."""
    m = re.fullmatch(r"item:(\d+)", request.strip())
    context = ""
    if m:
        r = db().execute("SELECT title, url, source, why, hook, summary FROM items WHERE id=?", (int(m.group(1)),)).fetchone()
        if r:
            context = json.dumps(dict(r), ensure_ascii=False)
            request = r["title"]
    out = claude(prompt_file("video", REQUEST=request, CONTEXT=context or "(none: research the topic yourself)"),
                 timeout=3600)
    send(out["result"].strip())
    log_run("video", True, f"{request[:80]} ({out['_seconds']}s)")
    return "ok"


# ================================================================ backup (no AI)
BACKUPS = HOME / "backups"
KEEP_BACKUPS = 30


def backup() -> str:
    """Consistent snapshot of the db (sqlite online backup) + notes + reports, as one dated tarball."""
    import sqlite3
    import tarfile
    BACKUPS.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d")
    snap = BACKUPS / f".argus-{stamp}.db"
    src, dst = db(), sqlite3.connect(snap)
    src.backup(dst)
    dst.close(); src.close()
    tar = BACKUPS / f"argus-{stamp}.tar.gz"
    no_pdfs = lambda ti: None if ti.name.lower().endswith(".pdf") else ti   # re-downloadable; text is in the db
    with tarfile.open(tar, "w:gz") as t:
        t.add(snap, "argus.db")
        t.add(NOTES, "notes", filter=no_pdfs)
        t.add(REPORTS, "reports", filter=no_pdfs)
    snap.unlink()
    old = sorted(BACKUPS.glob("argus-*.tar.gz"))[:-KEEP_BACKUPS]
    for f in old:
        f.unlink()
    info = f"{tar.name} {tar.stat().st_size // 1024} KB, {len(list(BACKUPS.glob('argus-*.tar.gz')))} backups kept"
    log_run("backup", True, info)
    return info


# ================================================================ status
def status() -> str:
    c = db()
    total = c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
    today = c.execute("SELECT COUNT(*) n FROM items WHERE fetched_at > datetime('now','-1 day')").fetchone()["n"]
    pending = c.execute("SELECT COUNT(*) n FROM items WHERE status='new'").fetchone()["n"]
    runs = c.execute("SELECT job, started, ok, info FROM runs ORDER BY id DESC LIMIT 8").fetchall()
    p = c.execute("SELECT COUNT(*) n, SUM(fulltext IS NOT NULL) ft, SUM(read_at IS NOT NULL) rd FROM papers").fetchone()
    nrep = c.execute("SELECT COUNT(*) n FROM reports").fetchone()["n"]
    lines = [f"📦 {total} items total, {today} in the last 24h, {pending} waiting for triage",
             f"📚 {p['n']} papers in library, {p['ft'] or 0} with full text, {p['rd'] or 0} read · {nrep} reports", ""]
    lines += [f"{'✅' if r['ok'] else '❌'} {r['job']} @ {r['started']} UTC: {r['info'][:120]}" for r in runs]
    return "\n".join(lines)


# ================================================================ cli
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=["fetch", "triage", "brief", "weekly", "research", "video", "status", "backup"])
    ap.add_argument("topic", nargs="*")
    ap.add_argument("--quiet", action="store_true", help="triage without sending alerts")
    a = ap.parse_args()
    try:
        if a.job == "fetch":
            print(fetch())
        elif a.job == "status":
            print(status())
        elif a.job == "backup":
            print(backup())
        else:
            with brain_lock():
                if a.job == "triage":
                    print(triage(quiet=a.quiet))
                elif a.job == "brief":
                    print(brief())
                elif a.job == "weekly":
                    print(weekly())
                elif a.job == "research":
                    print(research(" ".join(a.topic)))
                elif a.job == "video":
                    print(video(" ".join(a.topic)))
    except (ClaudeError, TimeoutError, ValueError) as e:
        log_run(a.job, False, str(e))
        print(f"{a.job} failed: {e}", file=sys.stderr)
        if a.job != "fetch":
            send(f"⚠️ Argus {a.job} failed: {esc(str(e)[:500])}")
        sys.exit(1)


if __name__ == "__main__":
    main()
