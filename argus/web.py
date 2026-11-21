"""Argus dashboard: a read-only window into the harness.

  ~/venv/bin/python -m argus.web     (systemd: argus-web.service, port 8787)

Public access goes through Nginx Proxy Manager (argus.homeserver.codeworks.systems) with basic auth.
Everything here is read-only: the db is opened mode=ro, and no endpoint changes state.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import sqlite3
import subprocess

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from .lib import DATA, HOME, ROOT, SECRETS, access_load, db, fts_query
from .sources import SOURCES

db().close()   # one writable open at startup applies schema migrations; every request after is read-only
app = FastAPI(title="Argus", docs_url=None, redoc_url=None)
STATIC = ROOT / "argus" / "static"
UNITS = ["argus-bot.service", "argus-web.service", "argus-fetch.timer", "argus-triage.timer",
         "argus-brief.timer", "argus-weekly.timer", "argus-backup.timer"]
JOB_EVERY_MIN = {"fetch": 30, "triage": 9 * 60, "brief": 24 * 60, "weekly": 7 * 24 * 60, "backup": 24 * 60}


def ro() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DATA / 'argus.db'}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def rows(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def _systemd(unit: str) -> dict:
    props = "ActiveState,SubState,Result,ExecMainStatus,NextElapseUSecRealtime,LastTriggerUSec,ActiveEnterTimestamp"
    try:
        out = subprocess.run(["systemctl", "--user", "show", unit, "-p", props], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception as e:
        return {"unit": unit, "error": str(e)}
    d = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {"unit": unit, "active": d.get("ActiveState"), "sub": d.get("SubState"), "result": d.get("Result"),
            "next": d.get("NextElapseUSecRealtime") or "", "last": d.get("LastTriggerUSec") or "",
            "since": d.get("ActiveEnterTimestamp") or ""}


def _kv(c, k, default=None):
    r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def _ago_min(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(ts.replace(" ", "T"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return round((dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 60, 1)
    except ValueError:
        return None


def _health(c) -> dict:
    """Overall health: ok | degraded | down, with reasons."""
    reasons = []
    units = [_systemd(u) for u in UNITS]
    for u in units:
        if u.get("active") != "active":
            reasons.append(f"{u['unit']} is {u.get('active')}")
    last = {r["job"]: r for r in rows(c, "SELECT job, MAX(id) id, started, ok, info FROM runs GROUP BY job")}
    jobs = {}
    for job, every in JOB_EVERY_MIN.items():
        r = last.get(job)
        ago = _ago_min(r["started"]) if r else None
        late = ago is not None and ago > every * 2 + 30
        jobs[job] = {"last": r["started"] if r else None, "ago_min": ago, "ok": bool(r["ok"]) if r else None,
                     "info": r["info"] if r else "", "late": late}
        if r and not r["ok"]:
            reasons.append(f"last {job} failed")
        if late:
            reasons.append(f"{job} hasn't run in {int(ago)} min")
    fails24 = c.execute("SELECT COUNT(*) FROM runs WHERE ok=0 AND started > datetime('now','-1 day')").fetchone()[0]
    try:
        src = json.loads(_kv(c, "source_status", "{}") or "{}")
    except json.JSONDecodeError:
        src = {}
    bad_src = [k for k, v in src.items() if not v.get("ok")]
    if len(bad_src) > len(SOURCES) / 3:
        reasons.append(f"{len(bad_src)} sources failing")
    disk = shutil.disk_usage("/")
    if disk.free / disk.total < 0.1:
        reasons.append("disk under 10% free")
    token_age_days = None
    env = SECRETS / "env"
    if env.exists():
        token_age_days = int((dt.datetime.now().timestamp() - env.stat().st_mtime) // 86400)
        if token_age_days > 335:
            reasons.append(f"Claude token expires in ~{365 - token_age_days} days")
    down = any(u["unit"] == "argus-bot.service" and u.get("active") != "active" for u in units)
    level = "down" if down else ("degraded" if reasons else "ok")
    return {"level": level, "reasons": reasons, "units": units, "jobs": jobs, "fails_24h": fails24,
            "disk": {"free_gb": round(disk.free / 1e9, 1), "total_gb": round(disk.total / 1e9, 1)},
            "db_mb": round((DATA / "argus.db").stat().st_size / 1e6, 2),
            "token_age_days": token_age_days, "bad_sources": bad_src}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/summary")
def summary():
    """Tiny payload for the homepage tile."""
    c = ro()
    h = _health(c)
    one = lambda sql: c.execute(sql).fetchone()[0]
    return {
        "health": h["level"],
        "items_24h": one("SELECT COUNT(*) FROM items WHERE fetched_at > datetime('now','-1 day')"),
        "alerts_24h": one("SELECT COUNT(*) FROM items WHERE alerted=1 AND fetched_at > datetime('now','-1 day')"),
        "papers": one("SELECT COUNT(*) FROM papers"),
        "last_fetch_min": h["jobs"]["fetch"]["ago_min"],
        "pending": one("SELECT COUNT(*) FROM items WHERE status='new'"),
    }


@app.get("/api/overview")
def overview():
    c = ro()
    one = lambda sql, a=(): c.execute(sql, a).fetchone()[0]
    try:
        src = json.loads(_kv(c, "source_status", "{}") or "{}")
    except json.JSONDecodeError:
        src = {}
    counts24 = {r["source"]: r["n"] for r in rows(
        c, "SELECT source, COUNT(*) n FROM items WHERE fetched_at > datetime('now','-1 day') GROUP BY source")}
    sources = [{"name": n, "category": cat, "kind": kind, "url": url, "items_24h": counts24.get(n, 0), **src.get(n, {})}
               for n, cat, kind, url in SOURCES]
    hourly = rows(c, """SELECT strftime('%Y-%m-%dT%H:00', fetched_at) h, COUNT(*) n FROM items
                        WHERE fetched_at > datetime('now','-48 hours') GROUP BY h ORDER BY h""")
    scores = rows(c, """SELECT score, COUNT(*) n FROM items WHERE status='triaged'
                        AND fetched_at > datetime('now','-7 days') GROUP BY score ORDER BY score""")
    cats = rows(c, "SELECT category, COUNT(*) n FROM items WHERE fetched_at > datetime('now','-1 day') GROUP BY category")
    brain = rows(c, """SELECT job, started, ok, info FROM runs WHERE job IN ('triage','brief','weekly','research','video')
                       AND started > datetime('now','-1 day') ORDER BY id DESC""")
    brain_secs = sum(int(m.group(1)) for r in brain for m in [re.search(r"\((\d+)s\)", r["info"] or "")] if m)
    chats24 = one("SELECT COUNT(*) FROM inbox WHERE kind IN ('owner','guest') AND at > datetime('now','-1 day')")
    backups = sorted((HOME / "backups").glob("argus-*.tar.gz"))
    lb = _kv(c, "last_brief")
    return {
        "health": _health(c),
        "counts": {
            "items": one("SELECT COUNT(*) FROM items"),
            "items_24h": one("SELECT COUNT(*) FROM items WHERE fetched_at > datetime('now','-1 day')"),
            "pending": one("SELECT COUNT(*) FROM items WHERE status='new'"),
            "alerts_24h": one("SELECT COUNT(*) FROM items WHERE alerted=1 AND fetched_at > datetime('now','-1 day')"),
            "alerts_7d": one("SELECT COUNT(*) FROM items WHERE alerted=1 AND fetched_at > datetime('now','-7 days')"),
            "papers": one("SELECT COUNT(*) FROM papers"),
            "papers_read": one("SELECT COUNT(*) FROM papers WHERE read_at IS NOT NULL"),
            "reports": one("SELECT COUNT(*) FROM reports"),
            "votes": one("SELECT COUNT(*) FROM feedback"),
            "upvotes": one("SELECT COUNT(*) FROM feedback WHERE vote>0"),
        },
        "brain": {"runs_24h": len(brain), "seconds_24h": brain_secs, "chats_24h": chats24},
        "sources": sources,
        "hourly": hourly,
        "scores": scores,
        "categories": cats,
        "backups": [{"name": b.name, "kb": b.stat().st_size // 1024} for b in backups[-7:]][::-1],
        "backup_count": len(backups),
        "last_brief": json.loads(lb) if lb else None,
    }


@app.get("/api/items")
def items(q: str = "", category: str = "", min_score: int = Query(-1), alerted: bool = False, limit: int = 60):
    c = ro()
    where, args = ["1=1"], []
    base = "SELECT i.id, i.title, i.url, i.source, i.category, i.score, i.why, i.hook, i.seen, i.status, i.alerted, i.fetched_at, i.cluster FROM items i"
    if q.strip():
        base += " JOIN items_fts f ON f.rowid = i.id"
        where.append("items_fts MATCH ?"); args.append(fts_query(q))
    if category:
        where.append("i.category = ?"); args.append(category)
    if min_score >= 0:
        where.append("i.score >= ?"); args.append(min_score)
    if alerted:
        where.append("i.alerted = 1")
    order = "i.score DESC, i.id DESC" if min_score >= 0 else "i.id DESC"
    return rows(c, f"{base} WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?", (*args, min(limit, 300)))


@app.get("/api/papers")
def papers(q: str = "", limit: int = 40):
    c = ro()
    cols = "p.arxiv_id, p.title, p.authors, p.upvotes, p.source, p.first_seen, p.pages, p.read_at, p.summary, p.url"
    if q.strip():
        return rows(c, f"SELECT {cols} FROM papers_fts f JOIN papers p ON p.rowid=f.rowid WHERE papers_fts MATCH ? "
                       "ORDER BY bm25(papers_fts) LIMIT ?", (fts_query(q), limit))
    return rows(c, f"SELECT {cols} FROM papers p ORDER BY (p.read_at IS NOT NULL) DESC, p.upvotes DESC, "
                   "p.first_seen DESC LIMIT ?", (limit,))


@app.get("/api/reports")
def reports():
    c = ro()
    return rows(c, """SELECT r.id, r.topic, r.created_at, r.path, substr(r.summary,1,400) summary,
                      (SELECT COUNT(*) FROM report_papers rp WHERE rp.report_id=r.id) papers
                      FROM reports r ORDER BY r.id DESC LIMIT 50""")


@app.get("/api/report/{rid}")
def report(rid: int):
    r = ro().execute("SELECT path FROM reports WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404)
    p = (ROOT / r["path"]) if not r["path"].startswith("/") else __import__("pathlib").Path(r["path"])
    if not p.exists() or ROOT not in p.resolve().parents:
        raise HTTPException(404, "report file missing")
    return JSONResponse({"markdown": p.read_text(errors="replace")})


@app.get("/api/runs")
def runs(limit: int = 80, job: str = ""):
    c = ro()
    if job:
        return rows(c, "SELECT * FROM runs WHERE job=? ORDER BY id DESC LIMIT ?", (job, limit))
    return rows(c, "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/access")
def access():
    """Who can talk to Argus. Names and ids only: no message contents from guests or strangers."""
    c = ro()
    a = access_load()
    msg = {r["chat_id"]: r for r in rows(c, """SELECT chat_id, MAX(at) last, COUNT(*) n, kind FROM inbox
                                                WHERE at > datetime('now','-7 days') GROUP BY chat_id""")}
    guests = [{"chat_id": int(k), **v, "msgs_7d": msg.get(int(k), {}).get("n", 0),
               "last": msg.get(int(k), {}).get("last")} for k, v in a["guests"].items()]
    return {
        "codeword_set": bool(a["codeword_hash"]),
        "guests": guests,
        "requests": rows(c, "SELECT chat_id, username, name, at, status FROM access_requests ORDER BY at DESC LIMIT 30"),
        "strangers_7d": rows(c, """SELECT chat_id, MAX(username) username, MAX(name) name, COUNT(*) n, MAX(at) last
                                   FROM inbox WHERE kind='stranger' AND at > datetime('now','-7 days')
                                   GROUP BY chat_id ORDER BY last DESC LIMIT 20"""),
    }


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8787, log_level="warning")


if __name__ == "__main__":
    main()
