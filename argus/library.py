"""Argus's research library: the papers it has seen and read, plus search over everything.

Argus (and you) use it from the shell, inside ~/argus:
  ~/venv/bin/python -m argus.library get 2611.26444          download + extract full text, print .txt path
  ~/venv/bin/python -m argus.library done 2611.26444 "takeaway..."   mark as read with a summary
  ~/venv/bin/python -m argus.library search "agent memory"    full-text search: papers + news items
  ~/venv/bin/python -m argus.library link 12 2611.26444 2610.01234   cite papers in report #12
  ~/venv/bin/python -m argus.library show 2611.26444          metadata + reading notes
  ~/venv/bin/python -m argus.library stats
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

import httpx

from .lib import ARXIV_ID, DATA, db, fts_query, upsert_paper

PAPERS_DIR = DATA / "papers"
UA = {"User-Agent": "ArgusResearchBot/1.0 (personal research assistant)"}


def _arxiv_id(s: str) -> str:
    m = ARXIV_ID.search(s)
    if not m:
        sys.exit(f"not an arXiv id or URL: {s}")
    return m.group(1)


def get(ident: str) -> None:
    aid = _arxiv_id(ident)
    c = db()
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    txt = PAPERS_DIR / f"{aid}.txt"
    row = c.execute("SELECT * FROM papers WHERE arxiv_id=?", (aid,)).fetchone()
    if row and row["fulltext"]:
        if not txt.exists():
            txt.write_text(row["fulltext"])
        print(f"already in library: {txt}  ({row['pages']} pages, read_at={row['read_at']})")
        return
    if not row:
        upsert_paper(c, {"arxiv_id": aid, "source": "on-demand"})
    pdf = PAPERS_DIR / f"{aid}.pdf"
    if not pdf.exists():
        r = httpx.get(f"https://arxiv.org/pdf/{aid}", headers=UA, timeout=120, follow_redirects=True)
        r.raise_for_status()
        pdf.write_bytes(r.content)
        time.sleep(3)  # arXiv asks for politeness
    subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=True, capture_output=True)
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
    pages = next((int(l.split()[-1]) for l in info.splitlines() if l.startswith("Pages:")), None)
    text = txt.read_text(errors="replace")
    title = row["title"] if row and row["title"] else next((l.strip() for l in text.splitlines() if l.strip()), "")[:300]
    c.execute("UPDATE papers SET fulltext=?, pdf_path=?, pages=?, title=COALESCE(NULLIF(title,''), ?) WHERE arxiv_id=?",
              (text, str(pdf), pages, title, aid))
    c.commit()
    print(f"{txt}  ({pages} pages, {len(text.split())} words)")


def done(ident: str, summary: str) -> None:
    aid = _arxiv_id(ident)
    c = db()
    n = c.execute("UPDATE papers SET read_at=datetime('now'), summary=? WHERE arxiv_id=?", (summary, aid)).rowcount
    c.commit()
    print("marked read" if n else f"{aid} not in library (run `get` first)")


def link(report_id: int, idents: list[str]) -> None:
    c = db()
    for s in idents:
        c.execute("INSERT OR IGNORE INTO report_papers(report_id, arxiv_id) VALUES(?,?)", (report_id, _arxiv_id(s)))
    c.commit()
    print(f"linked {len(idents)} papers to report #{report_id}")


def search(q: str, limit: int = 10) -> None:
    c = db()
    fq = fts_query(q)
    print(f"## Papers matching: {q}")
    for r in c.execute("""SELECT p.arxiv_id, p.title, p.upvotes, p.read_at,
                                 snippet(papers_fts, -1, '[', ']', ' … ', 18) snip
                          FROM papers_fts JOIN papers p ON p.rowid = papers_fts.rowid
                          WHERE papers_fts MATCH ? ORDER BY bm25(papers_fts) LIMIT ?""", (fq, limit)):
        flag = "📖 read" if r["read_at"] else ""
        print(f"- {r['arxiv_id']} | {r['title']} | ▲{r['upvotes'] or 0} {flag}\n    {r['snip']}")
    print(f"\n## News items matching: {q}")
    for r in c.execute("""SELECT i.id, i.score, i.fetched_at, i.source, i.title, i.url
                          FROM items_fts JOIN items i ON i.id = items_fts.rowid
                          WHERE items_fts MATCH ? ORDER BY bm25(items_fts) LIMIT ?""", (fq, limit)):
        print(f"- [{r['score'] if r['score'] is not None else '-'}] {r['fetched_at'][:10]} {r['source']}: {r['title']}\n    {r['url']}")
    print(f"\n## Past reports matching: {q}")
    like = f"%{q}%"
    for r in c.execute("SELECT id, created_at, topic, path FROM reports WHERE topic LIKE ? OR summary LIKE ? "
                       "ORDER BY id DESC LIMIT ?", (like, like, limit)):
        print(f"- #{r['id']} {r['created_at'][:10]} {r['topic']} → {r['path']}")


def show(ident: str) -> None:
    aid = _arxiv_id(ident)
    r = db().execute("SELECT arxiv_id, title, authors, published, categories, url, upvotes, source, first_seen, "
                     "pages, read_at, summary, abstract FROM papers WHERE arxiv_id=?", (aid,)).fetchone()
    if not r:
        print("not in library"); return
    for k in r.keys():
        print(f"{k}: {r[k]}")


def stats() -> None:
    c = db()
    r = c.execute("SELECT COUNT(*) n, SUM(fulltext IS NOT NULL) ft, SUM(read_at IS NOT NULL) rd, "
                  "SUM(upvotes >= 20) hot FROM papers").fetchone()
    print(f"papers={r['n']} full_text={r['ft'] or 0} read={r['rd'] or 0} hf_hot(>=20 upvotes)={r['hot'] or 0}")
    print(f"reports={c.execute('SELECT COUNT(*) FROM reports').fetchone()[0]} "
          f"citations={c.execute('SELECT COUNT(*) FROM report_papers').fetchone()[0]}")


def main():
    ap = argparse.ArgumentParser(prog="argus.library")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("get").add_argument("id")
    p = sub.add_parser("done"); p.add_argument("id"); p.add_argument("summary")
    p = sub.add_parser("link"); p.add_argument("report_id", type=int); p.add_argument("ids", nargs="+")
    p = sub.add_parser("search"); p.add_argument("query", nargs="+"); p.add_argument("--limit", type=int, default=10)
    sub.add_parser("show").add_argument("id")
    sub.add_parser("stats")
    a = ap.parse_args()
    if a.cmd == "get": get(a.id)
    elif a.cmd == "done": done(a.id, a.summary)
    elif a.cmd == "link": link(a.report_id, a.ids)
    elif a.cmd == "search": search(" ".join(a.query), a.limit)
    elif a.cmd == "show": show(a.id)
    elif a.cmd == "stats": stats()


if __name__ == "__main__":
    main()
