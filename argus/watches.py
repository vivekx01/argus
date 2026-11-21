"""Watches: what Argus keeps an eye on.

Watch 1 is built in: the AI & Finance channel (sources in argus/sources.py, scoring in prompts/triage.md,
the 07:30 brief and the Sunday weekly). Every other watch is created from a spec that Argus proposes and the
owner confirms with a tap. Each watch has its own sources, scoring rubric, alert bar and digest.

CLI (Argus uses these from chat):
  ~/venv/bin/python -m argus.watches list
  ~/venv/bin/python -m argus.watches discover <url>     best way to watch a site: rss > sitemap > page
  ~/venv/bin/python -m argus.watches show <id>
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import re
import time
from urllib.parse import quote_plus, urljoin, urlparse
from zoneinfo import ZoneInfo

import feedparser
import httpx
from bs4 import BeautifulSoup

from .lib import (claude, db, esc, extract_json, log_run, norm_title, prompt_file, send, strip_tags)

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ArgusResearchBot/1.0 (personal news reader)"}
IST = ZoneInfo("Asia/Kolkata")
MAX_WATCHES, MAX_SOURCES, MAX_AGE_H = 10, 25, 36
TRIAGE_BATCH, MAX_ALERTS_PER_RUN, NEW_PER_SOURCE = 120, 3, 30
KINDS = ("rss", "gnews", "sitemap", "page", "site")


def gnews_url(query: str, region: str = "IN") -> str:
    region = (region or "IN").upper()
    lang = {"IN": "en-IN", "GB": "en-GB"}.get(region, "en-US")
    return f"https://news.google.com/rss/search?q={quote_plus(query)}+when:1d&hl={lang}&gl={region}&ceid={region}:en"


def _norm_url(u: str) -> str:
    u = (u or "").strip()
    return u if re.match(r"https?://", u) else "https://" + u


def _h(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:12]


# ================================================================ reading sources
def _feed_entries(content: bytes) -> list:
    return feedparser.parse(content).entries


def _rss_items(url: str):
    r = httpx.get(url, headers=UA, timeout=30, follow_redirects=True)
    r.raise_for_status()
    now = dt.datetime.now(dt.timezone.utc)
    for e in _feed_entries(r.content)[:80]:
        t = None
        for k in ("published_parsed", "updated_parsed"):
            if e.get(k):
                import calendar
                t = dt.datetime.fromtimestamp(calendar.timegm(e[k]), dt.timezone.utc)
                break
        if t and (now - t).total_seconds() > MAX_AGE_H * 3600:
            continue
        yield {"url": e.get("link", ""), "title": strip_tags(e.get("title", "")),
               "summary": strip_tags(e.get("summary", ""))[:500], "published": t.isoformat() if t else ""}


def _page_links(url: str, page_html: str) -> dict[str, str]:
    """Article-looking links on a page, same site only: {url: link text}."""
    soup = BeautifulSoup(page_html, "html.parser")
    host = urlparse(url).netloc.removeprefix("www.")
    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        u = urljoin(url, a["href"]).split("#")[0]
        p = urlparse(u)
        if p.scheme not in ("http", "https") or p.netloc.removeprefix("www.") != host or u.rstrip("/") == url.rstrip("/"):
            continue
        text = " ".join(a.get_text(" ").split())
        if len(text) < 25 or re.search(r"/(tags?|category|author|page|login|signup|about|contact|privacy|terms|careers)(/|$)", p.path):
            continue
        out.setdefault(u, text[:300])
        if len(out) >= 200:
            break
    return out


def _sitemap_entries(url: str, depth: int = 0) -> list[tuple[str, str]]:
    r = httpx.get(url, headers=UA, timeout=30, follow_redirects=True)
    r.raise_for_status()
    xml = r.text

    def parse(tag):
        out = []
        for chunk in xml.split(f"<{tag}>")[1:]:
            chunk = chunk.split(f"</{tag}>")[0]
            loc = re.search(r"<loc>\s*(.*?)\s*</loc>", chunk, re.S)
            lm = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", chunk, re.S)
            if loc:
                out.append((html.unescape(loc.group(1)), lm.group(1) if lm else ""))
        return out

    if "<sitemapindex" in xml[:3000] and depth == 0:
        subs = sorted(parse("sitemap"), key=lambda s: s[1], reverse=True)[:2]   # newest child sitemaps
        return [e for loc, _ in subs for e in _sitemap_entries(loc, 1)]
    return parse("url")[:5000]


def _slug_title(u: str) -> str:
    seg = [s for s in urlparse(u).path.split("/") if s]
    t = re.sub(r"\.(html?|php|aspx?)$", "", seg[-1] if seg else u)
    t = re.sub(r"[-_]+", " ", re.sub(r"\b\d{5,}\b", "", t)).strip()
    return t[:1].upper() + t[1:] if t else u


FEED_PATHS = ["/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml", "/feed/", "/blog/feed",
              "/blog/rss.xml", "/news/rss.xml"]


def discover(url: str) -> dict:
    """Best way to watch a URL: its own feed > an advertised/common feed > sitemap > page links.
    Never tries to get around a site that refuses automated readers: it reports that instead."""
    url = _norm_url(url)
    try:
        r = httpx.get(url, headers=UA, timeout=25, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        why = "the site refuses automated readers" if code in (401, 403, 429) else "error"
        return {"error": f"HTTP {code}: {why}. Fallback: a Google News search for site:{urlparse(url).netloc}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: can't reach {url}"}
    final = str(r.url)
    if _feed_entries(r.content):
        return {"kind": "rss", "url": final, "count": len(_feed_entries(r.content)), "note": "the URL is a feed"}
    soup = BeautifulSoup(r.text, "html.parser")
    cands = [urljoin(final, l["href"]) for l in soup.find_all("link", href=True)
             if "alternate" in (l.get("rel") or []) and re.search(r"rss|atom", l.get("type") or "")]
    pu = urlparse(final)
    base = f"{pu.scheme}://{pu.netloc}"
    if pu.path.strip("/"):
        cands += [final.rstrip("/") + p for p in FEED_PATHS[:5]]          # section feeds first
    cands += [base + p for p in FEED_PATHS]
    tried = set()
    for cu in cands:
        if cu in tried:
            continue
        tried.add(cu)
        try:
            fr = httpx.get(cu, headers=UA, timeout=15, follow_redirects=True)
            if fr.status_code == 200 and _feed_entries(fr.content):
                return {"kind": "rss", "url": str(fr.url), "count": len(_feed_entries(fr.content)), "note": "found a feed"}
        except Exception:
            pass
        time.sleep(0.3)
    links = _page_links(final, r.text)
    if pu.path.strip("/") and len(links) >= 5:
        return {"kind": "page", "url": final, "count": len(links), "note": "no feed; watching this page for new article links"}
    for sm in (base + "/sitemap.xml", base + "/sitemap_index.xml", base + "/news-sitemap.xml"):
        try:
            if (n := len(_sitemap_entries(sm))):
                return {"kind": "sitemap", "url": sm, "count": n, "note": "no feed; watching the sitemap for new pages"}
        except Exception:
            pass
    if len(links) >= 5:
        return {"kind": "page", "url": final, "count": len(links), "note": "no feed or sitemap; watching the page for new links"}
    return {"error": f"no feed, sitemap or article links found. Fallback: a Google News search for site:{pu.netloc}"}


def _fetch_source(src: dict, state: dict) -> tuple[list[dict], dict]:
    """New items from one watch source, plus its updated state. Page/sitemap sources baseline silently first."""
    kind = src["kind"]
    if kind in ("rss", "gnews"):
        url = src["url"] if kind == "rss" else gnews_url(src["query"], src.get("region") or "IN")
        return list(_rss_items(url)), state
    seen = set(state.get("seen", []))
    if kind == "sitemap":
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()[:10]
        entries = _sitemap_entries(src["url"])
        fresh = [(u, "") for u, lm in entries if _h(u) not in seen and (not lm or lm[:10] >= cutoff)]
        allh = [_h(u) for u, _ in entries]
    else:  # page
        r = httpx.get(src["url"], headers=UA, timeout=30, follow_redirects=True)
        r.raise_for_status()
        links = _page_links(str(r.url), r.text)
        fresh = [(u, t) for u, t in links.items() if _h(u) not in seen]
        allh = [_h(u) for u in links]
    first_run = not state.get("init")
    new_state = {"init": True, "seen": (list(seen) + allh)[-20000:]}
    if first_run:
        return [], new_state                          # baseline: everything already there counts as "seen"
    items = []
    for u, text in fresh[:NEW_PER_SOURCE]:
        title = text or _slug_title(u)
        if kind == "sitemap" and len(items) < 8:      # sitemaps have no titles: fetch a few real ones
            try:
                t = BeautifulSoup(httpx.get(u, headers=UA, timeout=15, follow_redirects=True).text, "html.parser").title
                title = " ".join(t.get_text().split())[:300] if t else title
            except Exception:
                pass
        items.append({"url": u, "title": title, "summary": "", "published": ""})
    return items, new_state


# ================================================================ fetch (senses, no AI)
def fetch_watches() -> str:
    c = db()
    added = failed = 0
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    srcs = c.execute("""SELECT s.*, w.status wstatus FROM watch_sources s JOIN watches w ON w.id = s.watch_id
                        WHERE w.status='active'""").fetchall()
    for s in srcs:
        try:
            state = json.loads(s["state"] or "{}")
        except json.JSONDecodeError:
            state = {}
        n = 0
        try:
            items, state = _fetch_source(dict(s), state)
            for it in items:
                if not it["url"] or not it["title"]:
                    continue
                nt = norm_title(it["title"])
                same = c.execute("SELECT id FROM items WHERE watch_id=? AND norm=? AND fetched_at > datetime('now','-3 days')",
                                 (s["watch_id"], nt)).fetchone()
                if same:
                    c.execute("UPDATE items SET seen=seen+1 WHERE id=?", (same["id"],))
                    continue
                cur = c.execute("INSERT OR IGNORE INTO items(watch_id,url,title,norm,source,category,published,summary) "
                                "VALUES(?,?,?,?,?,?,?,?)", (s["watch_id"], it["url"], it["title"], nt, s["name"],
                                                            f"watch:{s['watch_id']}", it["published"], it["summary"]))
                n += cur.rowcount
            c.execute("UPDATE watch_sources SET state=?, ok=1, fails=0, error='', last_ok=?, last_at=?, last_added=? WHERE id=?",
                      (json.dumps(state), now, now, n, s["id"]))
        except Exception as e:
            failed += 1
            c.execute("UPDATE watch_sources SET ok=0, fails=fails+1, error=?, last_at=?, last_added=0 WHERE id=?",
                      (f"{type(e).__name__}: {str(e)[:160]}", now, s["id"]))
        c.commit()
        added += n
        time.sleep(1)
    info = f"watch sources={len(srcs)} added={added} failed={failed}"
    if srcs:
        log_run("watchfetch", True, info)
    return info


# ================================================================ triage (brain), every watch except the channel
def _votes(c, wid: int) -> str:
    rows = c.execute("""SELECT i.title, f.vote FROM feedback f JOIN items i ON i.id=f.item_id
                        WHERE i.watch_id=? ORDER BY f.id DESC LIMIT 30""", (wid,)).fetchall()
    return "\n".join(f"{'👍' if r['vote'] > 0 else '👎'} {r['title']}" for r in rows) or "(no votes yet)"


def _triage_model() -> str:
    from .jobs import triage_model
    return triage_model()


def triage_watches(quiet: bool = False, only: int | None = None) -> str:
    from .jobs import link_of, resolve_links
    c = db()
    out = []
    q = "SELECT * FROM watches WHERE status='active' AND id != 1" + (" AND id=?" if only else "")
    for w in c.execute(q, (only,) if only else ()).fetchall():
        wid = w["id"]
        c.execute("UPDATE items SET status='skipped' WHERE watch_id=? AND status='new' "
                  "AND fetched_at < datetime('now','-2 days')", (wid,))
        c.commit()  # release the write lock before the brain call
        rows = c.execute("SELECT * FROM items WHERE watch_id=? AND status='new' ORDER BY id DESC LIMIT ?",
                         (wid, TRIAGE_BATCH)).fetchall()
        if not rows:
            continue
        recent = c.execute("SELECT title FROM items WHERE watch_id=? AND alerted=1 AND fetched_at > datetime('now','-3 days')",
                           (wid,)).fetchall()
        items = [{"id": r["id"], "src": r["source"], "seen": r["seen"], "title": r["title"],
                  "summary": (r["summary"] or "")[:280]} for r in rows]
        res = claude(prompt_file("watch_triage", NAME=w["name"], PURPOSE=w["purpose"] or "", RUBRIC=w["rubric"] or "",
                                 THRESHOLD=w["alert_threshold"], VOTES=_votes(c, wid),
                                 RECENT_ALERTS="\n".join(f"- {r['title']}" for r in recent) or "(none)",
                                 ITEMS=json.dumps(items, ensure_ascii=False),
                                 NOW=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")), timeout=1200,
                     agent="triager", model=_triage_model(), label="watchtriage")
        verdicts = extract_json(res["result"])
        by_id = {r["id"]: r for r in rows}
        alerts = []
        for v in verdicts:
            if v.get("id") not in by_id:
                continue
            sc = int(v.get("score", 0))
            c.execute("UPDATE items SET status='triaged', score=?, why=?, cluster=? WHERE id=?",
                      (sc, v.get("why", ""), v.get("cluster", ""), v["id"]))
            if v.get("alert") and sc >= w["alert_threshold"]:
                alerts.append(v)
        c.execute(f"UPDATE items SET status='triaged', score=COALESCE(score,0) WHERE status='new' AND id IN "
                  f"({','.join(str(i) for i in by_id)})")
        c.commit()
        alerts = sorted(alerts, key=lambda v: -int(v.get("score", 0)))[:MAX_ALERTS_PER_RUN]
        try:
            resolve_links([v["id"] for v in verdicts if v.get("id") in by_id and int(v.get("score", 0)) >= 6], limit=25)
        except Exception:
            pass
        for v in alerts:
            r = c.execute("SELECT * FROM items WHERE id=?", (v["id"],)).fetchone()
            c.execute("UPDATE items SET alerted=1 WHERE id=?", (r["id"],))
            c.commit()
            if quiet:
                continue
            send(f"{w['emoji']} <b>{esc(w['name'])} alert</b>\n\n<b>{esc(r['title'])}</b>\n"
                 f"<i>{esc(r['source'])} · score {v.get('score')}/10</i>\n\n{esc(v.get('why', ''))}\n\n"
                 f"<a href=\"{esc(link_of(r))}\">Read source</a>", kind="watch_alert", item_id=r["id"],
                 buttons=[[{"text": "👍", "callback_data": f"up:{r['id']}"},
                           {"text": "👎", "callback_data": f"down:{r['id']}"},
                           {"text": "🔎 Dig deeper", "callback_data": f"dig:{r['id']}"}]])
        info = f"watch {wid} triaged={len(rows)} alerts={len(alerts)} ({res['_seconds']}s)"
        log_run("watchtriage", True, info)
        out.append(info)
    return "; ".join(out) or "no watch items"


# ================================================================ digests (brain)
def digest_watches(only: int | None = None, force: bool = False, progress_factory=None) -> str:
    c = db()
    today = dt.datetime.now(IST)
    done = []
    q = "SELECT * FROM watches WHERE id != 1 AND status='active'" + (" AND id=?" if only else "")
    for w in c.execute(q, (only,) if only else ()).fetchall():
        due = force or w["digest"] == "daily" or (w["digest"] == "weekly" and today.weekday() == 0)
        if not due:
            continue
        try:
            triage_watches(only=w["id"])
        except Exception as e:
            log_run("watchtriage", False, f"pre-digest: {e}"[:300])
        since = w["last_digest_at"] or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        rows = c.execute("""SELECT id, title, source, url, real_url, score, why, cluster, seen, fetched_at FROM items
                            WHERE watch_id=? AND status='triaged' AND score >= 5 AND fetched_at > ?
                            ORDER BY score DESC, seen DESC LIMIT 40""", (w["id"], since)).fetchall()
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if not rows:
            send(f"{w['emoji']} <b>{esc(w['name'])}</b>: quiet since the last digest. Still watching 👁", kind="watch_digest")
        else:
            from .jobs import resolve_links
            try:
                resolve_links([r["id"] for r in rows[:20]], limit=20)
            except Exception:
                pass
            items = [{k: r[k] for k in ("id", "title", "source", "score", "why", "cluster", "seen", "fetched_at")} for r in rows]
            prog = progress_factory(f"{w['emoji']} Writing the {w['name']} digest…") if progress_factory else None
            res = claude(prompt_file("watch_digest", NAME=w["name"], EMOJI=w["emoji"], PURPOSE=w["purpose"] or "",
                                     SINCE=since[:10], ITEMS=json.dumps(items, ensure_ascii=False)),
                         timeout=1500, progress=prog, agent="digest-editor", label="watchdigest")
            if prog:
                prog.done(True)
            send(res["result"].strip(), kind="watch_digest")
        c.execute("UPDATE watches SET last_digest_at=? WHERE id=?", (stamp, w["id"]))
        c.commit()
        done.append(w["name"])
        log_run("watchdigest", True, f"{w['name']}: {len(rows)} items")
    return ", ".join(done) or "no digests due"


# ================================================================ create / update from an approved spec
def apply_spec(spec: dict, progress=None) -> str:
    """Validate every source for real, then create or update the watch. Called only after the owner taps ✅."""
    c = db()
    wid = spec.get("id")
    if wid is not None:
        wid = int(wid)
        w = c.execute("SELECT * FROM watches WHERE id=?", (wid,)).fetchone()
        if not w:
            return f"⚠️ There's no watch #{wid}."
    else:
        n = c.execute("SELECT COUNT(*) n FROM watches WHERE id != 1").fetchone()["n"]
        if n >= MAX_WATCHES:
            return f"⚠️ You already have {n} watches (max {MAX_WATCHES}). Remove one first."
    sources = spec.get("sources") or []
    lines, ok_sources = [], []
    for s in sources[:MAX_SOURCES]:
        kind = (s.get("kind") or "site").lower()
        name = (s.get("name") or s.get("query") or s.get("url") or "source")[:80]
        if progress:
            progress.step(f"checking {name}")
        try:
            if kind == "gnews":
                if not s.get("query"):
                    raise ValueError("gnews needs a query")
                r = httpx.get(gnews_url(s["query"], s.get("region") or "IN"), headers=UA, timeout=25)
                r.raise_for_status()
                ok_sources.append({"name": name, "kind": "gnews", "query": s["query"], "region": (s.get("region") or "IN").upper()})
                lines.append(f"✓ {esc(name)}: news search ({len(_feed_entries(r.content))} in the last day)")
                continue
            if kind in ("rss", "sitemap", "page"):
                try:
                    if kind == "rss":
                        r = httpx.get(_norm_url(s["url"]), headers=UA, timeout=25, follow_redirects=True)
                        r.raise_for_status()
                        if not _feed_entries(r.content):
                            raise ValueError("not a feed")
                        cnt = len(_feed_entries(r.content))
                    elif kind == "sitemap":
                        cnt = len(_sitemap_entries(_norm_url(s["url"])))
                    else:
                        r = httpx.get(_norm_url(s["url"]), headers=UA, timeout=25, follow_redirects=True)
                        r.raise_for_status()
                        cnt = len(_page_links(str(r.url), r.text))
                    ok_sources.append({"name": name, "kind": kind, "url": _norm_url(s["url"])})
                    lines.append(f"✓ {esc(name)}: {kind} ({cnt} entries)")
                    continue
                except Exception:
                    kind = "site"                                # fall back to discovery
            d = discover(s.get("url", ""))
            if "error" in d:
                lines.append(f"✗ {esc(name)}: {esc(d['error'])}")
                continue
            ok_sources.append({"name": name, "kind": d["kind"], "url": d["url"]})
            lines.append(f"✓ {esc(name)}: {d['kind']} ({d.get('count', '?')} entries; {esc(d['note'])})")
        except Exception as e:
            lines.append(f"✗ {esc(name)}: {esc(type(e).__name__)} {esc(str(e)[:80])}")
    if not ok_sources and (wid is None or sources):
        return "⚠️ None of the sources worked, so I didn't create the watch.\n" + "\n".join(lines)

    if wid is None:
        wid = c.execute("INSERT INTO watches(name, emoji, purpose, rubric, alert_threshold, digest) VALUES(?,?,?,?,?,?)",
                        (spec.get("name", "Watch")[:80], spec.get("emoji", "👁")[:4], spec.get("purpose", ""),
                         spec.get("rubric", ""), int(spec.get("alert_threshold", 8)),
                         spec.get("digest", "weekly") if spec.get("digest") in ("daily", "weekly", "none") else "weekly")).lastrowid
        verb = "Started"
    else:
        if wid != 1:     # the channel watch keeps its built-in rubric (prompts/triage.md) and settings
            c.execute("UPDATE watches SET name=COALESCE(?,name), emoji=COALESCE(?,emoji), purpose=COALESCE(?,purpose), "
                      "rubric=COALESCE(?,rubric), alert_threshold=COALESCE(?,alert_threshold), digest=COALESCE(?,digest) "
                      "WHERE id=?", (spec.get("name"), spec.get("emoji"), spec.get("purpose"), spec.get("rubric"),
                                     spec.get("alert_threshold"), spec.get("digest"), wid))
        if spec.get("replace_sources") and wid != 1:
            c.execute("DELETE FROM watch_sources WHERE watch_id=?", (wid,))
        verb = "Updated"
    existing = {(r["kind"], r["url"] or "", r["query"] or "") for r in
                c.execute("SELECT kind, url, query FROM watch_sources WHERE watch_id=?", (wid,))}
    for s in ok_sources:
        if (s["kind"], s.get("url", ""), s.get("query", "")) in existing:
            continue
        c.execute("INSERT INTO watch_sources(watch_id, name, kind, url, query, region) VALUES(?,?,?,?,?,?)",
                  (wid, s["name"], s["kind"], s.get("url"), s.get("query"), s.get("region")))
    c.commit()
    if progress:
        progress.step("first fetch")
    try:
        fetch_watches()          # baseline page/sitemap sources, pull today's news for the rest
    except Exception:
        pass
    w = c.execute("SELECT * FROM watches WHERE id=?", (wid,)).fetchone()
    n_items = c.execute("SELECT COUNT(*) n FROM items WHERE watch_id=?", (wid,)).fetchone()["n"]
    return (f"{w['emoji']} <b>{verb} watch #{wid}: {esc(w['name'])}</b>\n"
            f"Alerts at {w['alert_threshold']}+ · digest: {w['digest']} · {n_items} items so far\n\n" + "\n".join(lines))


def summary_lines() -> list[dict]:
    from .sources import SOURCES
    c = db()
    out = []
    for w in c.execute("SELECT * FROM watches ORDER BY id").fetchall():
        n_src = c.execute("SELECT COUNT(*) n FROM watch_sources WHERE watch_id=?", (w["id"],)).fetchone()["n"]
        if w["id"] == 1:
            n_src += len(SOURCES)
        bad = c.execute("SELECT COUNT(*) n FROM watch_sources WHERE watch_id=? AND ok=0", (w["id"],)).fetchone()["n"]
        n24 = c.execute("SELECT COUNT(*) n FROM items WHERE watch_id=? AND fetched_at > datetime('now','-1 day')",
                        (w["id"],)).fetchone()["n"]
        out.append({**dict(w), "sources": n_src, "failing": bad, "items_24h": n24})
    return out


def main():
    ap = argparse.ArgumentParser(prog="argus.watches")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("discover").add_argument("url")
    sub.add_parser("show").add_argument("id", type=int)
    a = ap.parse_args()
    if a.cmd == "list":
        for w in summary_lines():
            print(f"#{w['id']} {w['emoji']} {w['name']} [{w['status']}] sources={w['sources']} failing={w['failing']} "
                  f"items_24h={w['items_24h']} alerts>={w['alert_threshold']} digest={w['digest']}")
    elif a.cmd == "discover":
        print(json.dumps(discover(a.url), indent=2))
    elif a.cmd == "show":
        c = db()
        w = c.execute("SELECT * FROM watches WHERE id=?", (a.id,)).fetchone()
        if not w:
            print("no such watch"); return
        print(json.dumps(dict(w), indent=2, ensure_ascii=False))
        for s in c.execute("SELECT id, name, kind, url, query, region, ok, fails, error, last_ok, last_added "
                           "FROM watch_sources WHERE watch_id=?", (a.id,)):
            print(dict(s))


if __name__ == "__main__":
    main()
