"""Shared plumbing for Argus: paths, env, database, Telegram, and the Claude runner."""
from __future__ import annotations

import html
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import time

import httpx

HOME = pathlib.Path.home()
ROOT = pathlib.Path(__file__).resolve().parent.parent          # repo root (~/argus on the server)
DATA = ROOT / "data"                                            # gitignored: db, logs
NOTES = ROOT / "notes"                                          # gitignored: Argus's own memory
REPORTS = ROOT / "reports"                                      # gitignored: research reports
PROMPTS = ROOT / "prompts"
SECRETS = HOME / ".argus"                                       # tokens, never in the repo
CLAUDE_BIN = HOME / ".local/bin/claude"

for d in (DATA, NOTES, REPORTS):
    d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- env / secrets
def _load_env_file(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("'\"")
    return out


CLAUDE_ENV = _load_env_file(SECRETS / "env")            # CLAUDE_CODE_OAUTH_TOKEN
TG_ENV = _load_env_file(SECRETS / "telegram.env")       # TELEGRAM_BOT_TOKEN, OWNER_CHAT_ID
CCSTUDIO_ENV = _load_env_file(SECRETS / "ccstudio.env")  # CCSTUDIO_AUTH (Authorization header for the MCP)
TG_TOKEN = TG_ENV.get("TELEGRAM_BOT_TOKEN", "")
OWNER = int(TG_ENV.get("OWNER_CHAT_ID", "0") or 0)
GUEST_DIR = HOME / "argus-guest"      # outside the repo: no CLAUDE.md, settings, db or MCP leak into guest chats


# ---------------------------------------------------------------- access (guests)
# Lives in ~/.argus so it isn't in git and the brain's tools are denied access to it.
# Only bot.py changes it, and only when the owner presses a button or sends a command.
ACCESS = SECRETS / "access.json"


def access_load() -> dict:
    try:
        a = json.loads(ACCESS.read_text())
    except Exception:
        a = {}
    a.setdefault("codeword_hash", "")
    a.setdefault("guests", {})          # chat_id(str) -> {name, username, added_at}
    return a


def access_save(a: dict) -> None:
    tmp = ACCESS.with_suffix(".tmp")
    tmp.write_text(json.dumps(a, indent=2))
    tmp.chmod(0o600)
    tmp.replace(ACCESS)


def _cw_hash(s: str) -> str:
    import hashlib
    return hashlib.sha256(" ".join((s or "").lower().split()).encode()).hexdigest()


def set_codeword(word: str) -> None:
    a = access_load(); a["codeword_hash"] = _cw_hash(word); access_save(a)


def codeword_matches(text: str) -> bool:
    h = access_load().get("codeword_hash")
    return bool(h) and _cw_hash(text) == h


def is_guest(chat_id: int) -> bool:
    return str(chat_id) in access_load()["guests"]


# ---------------------------------------------------------------- database
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  url TEXT UNIQUE,
  title TEXT,
  norm TEXT,
  source TEXT,
  category TEXT,
  published TEXT,
  summary TEXT,
  fetched_at TEXT DEFAULT (datetime('now')),
  seen INTEGER DEFAULT 1,          -- how many sources carried the same headline
  status TEXT DEFAULT 'new',       -- new | triaged | skipped
  score INTEGER,
  why TEXT,
  hook TEXT,
  cluster TEXT,
  alerted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS i_status ON items(status);
CREATE INDEX IF NOT EXISTS i_norm ON items(norm);
CREATE INDEX IF NOT EXISTS i_fetched ON items(fetched_at);
CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY, item_id INTEGER, vote INTEGER, at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, job TEXT, started TEXT DEFAULT (datetime('now')), ok INTEGER, info TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);

-- everyone who messages the bot (owner, guests, strangers); strangers' text is data, never instructions
CREATE TABLE IF NOT EXISTS inbox(
  id INTEGER PRIMARY KEY, chat_id INTEGER, username TEXT, name TEXT, text TEXT,
  kind TEXT,                       -- owner | guest | stranger | codeword
  at TEXT DEFAULT (datetime('now')));
CREATE INDEX IF NOT EXISTS i_inbox_chat ON inbox(chat_id, at);
CREATE TABLE IF NOT EXISTS access_requests(
  chat_id INTEGER PRIMARY KEY, username TEXT, name TEXT, at TEXT DEFAULT (datetime('now')),
  status TEXT DEFAULT 'pending');  -- pending | approved | denied | revoked

-- research library ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS papers(
  arxiv_id TEXT PRIMARY KEY,
  title TEXT,
  authors TEXT,                    -- comma-separated
  published TEXT,
  categories TEXT,                 -- comma-separated arXiv categories
  abstract TEXT,
  url TEXT,
  upvotes INTEGER DEFAULT 0,       -- Hugging Face Daily Papers upvotes (max seen)
  source TEXT,                     -- where we first saw it
  first_seen TEXT DEFAULT (datetime('now')),
  pdf_path TEXT,
  pages INTEGER,
  fulltext TEXT,                   -- pdftotext output, once downloaded
  read_at TEXT,                    -- when Argus read it in full
  summary TEXT                     -- Argus's takeaway after reading
);
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY, topic TEXT, path TEXT, created_at TEXT DEFAULT (datetime('now')), summary TEXT);
CREATE TABLE IF NOT EXISTS report_papers(
  report_id INTEGER, arxiv_id TEXT, PRIMARY KEY(report_id, arxiv_id));

-- full-text search (external-content FTS5, kept in sync by triggers) --------------
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, summary, why, hook, content='items', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, summary, why, hook) VALUES (new.id, new.title, new.summary, new.why, new.hook);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, summary, why, hook)
  VALUES ('delete', old.id, old.title, old.summary, old.why, old.hook);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE OF title, summary, why, hook ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, summary, why, hook)
  VALUES ('delete', old.id, old.title, old.summary, old.why, old.hook);
  INSERT INTO items_fts(rowid, title, summary, why, hook) VALUES (new.id, new.title, new.summary, new.why, new.hook);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
  title, authors, abstract, fulltext, summary, content='papers', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS papers_ai AFTER INSERT ON papers BEGIN
  INSERT INTO papers_fts(rowid, title, authors, abstract, fulltext, summary)
  VALUES (new.rowid, new.title, new.authors, new.abstract, new.fulltext, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS papers_ad AFTER DELETE ON papers BEGIN
  INSERT INTO papers_fts(papers_fts, rowid, title, authors, abstract, fulltext, summary)
  VALUES ('delete', old.rowid, old.title, old.authors, old.abstract, old.fulltext, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS papers_au AFTER UPDATE OF title, authors, abstract, fulltext, summary ON papers BEGIN
  INSERT INTO papers_fts(papers_fts, rowid, title, authors, abstract, fulltext, summary)
  VALUES ('delete', old.rowid, old.title, old.authors, old.abstract, old.fulltext, old.summary);
  INSERT INTO papers_fts(rowid, title, authors, abstract, fulltext, summary)
  VALUES (new.rowid, new.title, new.authors, new.abstract, new.fulltext, new.summary);
END;
"""


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DATA / "argus.db", timeout=30)
    c.row_factory = sqlite3.Row
    had_fts = c.execute("SELECT 1 FROM sqlite_master WHERE name='items_fts'").fetchone()
    c.executescript(SCHEMA)
    if not had_fts:  # first run after adding FTS: index the rows that already exist
        c.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
        c.execute("INSERT INTO papers_fts(papers_fts) VALUES('rebuild')")
        c.commit()
    return c


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: every word must appear (prefix-matched)."""
    words = re.findall(r"\w+", text or "")
    return " ".join(f'"{w}"*' for w in words) or '""'


ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5})")


def upsert_paper(c: sqlite3.Connection, p: dict) -> None:
    """Insert or refresh a paper's metadata. Never clobbers full text / reading notes."""
    c.execute("""
      INSERT INTO papers(arxiv_id, title, authors, published, categories, abstract, url, upvotes, source)
      VALUES(:arxiv_id, :title, :authors, :published, :categories, :abstract, :url, :upvotes, :source)
      ON CONFLICT(arxiv_id) DO UPDATE SET
        title      = COALESCE(NULLIF(excluded.title, ''), papers.title),
        authors    = COALESCE(NULLIF(excluded.authors, ''), papers.authors),
        categories = COALESCE(NULLIF(excluded.categories, ''), papers.categories),
        abstract   = CASE WHEN LENGTH(COALESCE(excluded.abstract, '')) > LENGTH(COALESCE(papers.abstract, ''))
                          THEN excluded.abstract ELSE papers.abstract END,
        upvotes    = MAX(COALESCE(papers.upvotes, 0), COALESCE(excluded.upvotes, 0))
    """, {"title": "", "authors": "", "published": "", "categories": "", "abstract": "",
          "upvotes": 0, "source": "", **p, "url": p.get("url") or f"https://arxiv.org/abs/{p['arxiv_id']}"})


def kv_get(c: sqlite3.Connection, k: str, default: str | None = None) -> str | None:
    r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(c: sqlite3.Connection, k: str, v: str) -> None:
    c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    c.commit()


def log_run(job: str, ok: bool, info: str = "") -> None:
    c = db()
    c.execute("INSERT INTO runs(job, ok, info) VALUES(?,?,?)", (job, int(ok), info[:2000]))
    c.commit()


# ---------------------------------------------------------------- text helpers
def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def norm_title(t: str) -> str:
    t = re.sub(r"\s+[-|–—]\s+[^-|–—]{2,40}$", "", t or "")   # drop " - Reuters" style suffixes
    t = re.sub(r"[^a-z0-9 ]", "", t.lower())
    return re.sub(r"\s+", " ", t).strip()[:120]


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


# ---------------------------------------------------------------- telegram
TG_LIMIT = 3900


def tg(method: str, **data) -> dict:
    try:
        r = httpx.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=data, timeout=60)
        return r.json()
    except Exception as e:  # network blip: never crash a job over a message
        return {"ok": False, "description": str(e)}


def _chunks(text: str, n: int = TG_LIMIT) -> list[str]:
    parts, buf = [], ""
    for para in text.split("\n\n"):
        while len(para) > n:                       # a single giant paragraph
            parts.append(para[:n]); para = para[n:]
        if len(buf) + len(para) + 2 > n:
            parts.append(buf); buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        parts.append(buf)
    return parts or [""]


def send(text: str, buttons: list | None = None, html_mode: bool = True, chat_id: int | None = None) -> None:
    """Send to the owner. Falls back to plain text if Telegram rejects the HTML."""
    chat_id = chat_id or OWNER
    parts = _chunks(text)
    for i, part in enumerate(parts):
        kw = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
        if html_mode:
            kw["parse_mode"] = "HTML"
        if buttons and i == len(parts) - 1:
            kw["reply_markup"] = {"inline_keyboard": buttons}
        r = tg("sendMessage", **kw)
        if not r.get("ok") and html_mode:
            kw.pop("parse_mode", None)
            kw["text"] = strip_tags(part)
            tg("sendMessage", **kw)


def send_document(path: pathlib.Path, caption: str = "") -> None:
    with open(path, "rb") as f:
        httpx.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                   data={"chat_id": OWNER, "caption": caption[:1000]},
                   files={"document": (path.name, f)}, timeout=120)


# ---------------------------------------------------------------- claude
def _claude_env(guest: bool = False) -> dict[str, str]:
    """Environment for the brain: the Claude token, but NOT the Telegram token. Guests get no CC Studio either."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM", "CCSTUDIO"))}
    env.update(CLAUDE_ENV)
    if not guest:
        env.update(CCSTUDIO_ENV)
    env["PATH"] = f"{HOME}/.local/bin:{HOME}/venv/bin:{env.get('PATH', '/usr/bin:/bin')}"
    return env


class ClaudeError(RuntimeError):
    pass


GUEST_TOOLS = ["--allowedTools", "WebSearch,WebFetch",
               "--disallowedTools", "Bash,Read,Write,Edit,Glob,Grep,NotebookEdit,Task,Agent"]


def claude(prompt: str, timeout: int = 900, resume: str | None = None, guest: bool = False) -> dict:
    """Run one headless Claude Code turn.

    Owner mode runs in the repo dir (CLAUDE.md, .claude/settings.json, .mcp.json apply).
    Guest mode runs in ~/argus-guest with web tools only and no secrets, db, notes or MCP.
    Returns the parsed JSON envelope: {result, session_id, is_error, ...}.
    """
    cmd = [str(CLAUDE_BIN), "-p", prompt, "--output-format", "json"]
    if guest:
        cmd += GUEST_TOOLS
    if resume:
        cmd += ["--resume", resume]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=GUEST_DIR if guest else ROOT, capture_output=True, text=True,
                           timeout=timeout, env=_claude_env(guest))
    except subprocess.TimeoutExpired:
        raise ClaudeError(f"timed out after {timeout}s")
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise ClaudeError((p.stdout + p.stderr)[-1500:] or f"exit {p.returncode}")
    out["_seconds"] = round(time.time() - t0)
    if out.get("is_error"):
        raise ClaudeError(str(out.get("result", ""))[:1500])
    return out


def extract_json(text: str):
    """Pull the first JSON array/object out of a model reply (tolerates ```json fences)."""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    for open_c, close_c in (("[", "]"), ("{", "}")):
        a, b = text.find(open_c), text.rfind(close_c)
        if a != -1 and b > a:
            try:
                return json.loads(text[a:b + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON found in model reply")


def prompt_file(name: str, **fmt) -> str:
    t = (PROMPTS / f"{name}.md").read_text()
    for k, v in fmt.items():
        t = t.replace("{{" + k + "}}", str(v))
    return t
