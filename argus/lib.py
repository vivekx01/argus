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
-- everything Argus sends to Telegram, so chat turns know what alerts/briefs/reports the owner has seen
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, chat_id INTEGER, kind TEXT, item_id INTEGER, text TEXT,
  tg_message_id INTEGER, at TEXT DEFAULT (datetime('now')));
CREATE INDEX IF NOT EXISTS i_outbox_at ON outbox(at);
CREATE INDEX IF NOT EXISTS i_outbox_msg ON outbox(tg_message_id);
-- abuse flags raised by guest-mode Argus; they only notify the owner, they never revoke anything
CREATE TABLE IF NOT EXISTS guest_flags(
  id INTEGER PRIMARY KEY, chat_id INTEGER, reason TEXT, at TEXT DEFAULT (datetime('now')));
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
    cols = {r[1] for r in c.execute("PRAGMA table_info(items)")}
    if "real_url" not in cols:   # publisher URL behind a Google News redirect, resolved lazily
        c.execute("ALTER TABLE items ADD COLUMN real_url TEXT")
        c.commit()
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


def send(text: str, buttons: list | None = None, html_mode: bool = True, chat_id: int | None = None,
         kind: str = "message", item_id: int | None = None) -> int | None:
    """Send to the owner (or a chat). Falls back to plain text if Telegram rejects the HTML.

    Everything sent is recorded in `outbox`, so later chat turns know what the owner has seen.
    Returns the Telegram message id of the last part.
    """
    chat_id = chat_id or OWNER
    parts = _chunks(text)
    mid = None
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
            r = tg("sendMessage", **kw)
        mid = (r.get("result") or {}).get("message_id", mid)
    record_outbox(chat_id, kind, text, mid, item_id)
    return mid


def record_outbox(chat_id: int, kind: str, text: str, tg_message_id: int | None = None,
                  item_id: int | None = None) -> None:
    try:
        c = db()
        c.execute("INSERT INTO outbox(chat_id, kind, item_id, text, tg_message_id) VALUES(?,?,?,?,?)",
                  (chat_id, kind, item_id, strip_tags(text)[:4000], tg_message_id))
        c.commit()
    except Exception:
        pass


def send_document(path: pathlib.Path, caption: str = "", chat_id: int | None = None, kind: str = "document") -> None:
    with open(path, "rb") as f:
        r = httpx.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                       data={"chat_id": chat_id or OWNER, "caption": caption[:1000]},
                       files={"document": (path.name, f)}, timeout=180)
    try:
        mid = r.json().get("result", {}).get("message_id")
    except Exception:
        mid = None
    record_outbox(chat_id or OWNER, kind, f"[file {path.name}] {caption}", mid)


# ---------------------------------------------------------------- live progress in Telegram
def _short(s: str, n: int = 70) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe_tool(name: str, inp: dict) -> str:
    """Human label for a tool call, shown in the live status message."""
    from urllib.parse import urlparse
    inp = inp or {}
    if name == "WebSearch":
        return f"🔎 searching: {_short(inp.get('query'), 60)}"
    if name == "WebFetch":
        return f"📄 reading {urlparse(inp.get('url', '')).netloc or 'a page'}"
    if name == "Read":
        return f"📖 reading {pathlib.Path(inp.get('file_path', '')).name}"
    if name in ("Write", "Edit"):
        return f"✍️ writing {pathlib.Path(inp.get('file_path', '')).name}"
    if name == "Bash":
        cmd = inp.get("command", "")
        if "argus.library get" in cmd:
            m = ARXIV_ID.search(cmd)
            return f"📚 fetching paper {m.group(1) if m else ''} into the library"
        if "argus.library search" in cmd:
            return "🗂 checking what I already know"
        if "argus.library done" in cmd:
            return "🧠 saving paper takeaway"
        if "argus.library link" in cmd:
            return "🔗 citing papers in the report"
        if "sqlite3" in cmd:
            return "🗃 querying my database"
        if "pdftotext" in cmd:
            return "📑 extracting PDF text"
        if "curl" in cmd:
            return "⬇️ downloading"
        return f"⚙️ {_short(inp.get('description') or cmd, 60)}"
    if name.startswith("mcp__ccstudio__"):
        tool = name.split("__")[-1]
        return {"validate_script": "✅ validating the script", "preview_video": "🖼 previewing the layout",
                "render_video": "🎞 rendering the video", "get_job": "⏳ checking render progress",
                "export_video": "📦 exporting files", "get_outputs": "📋 collecting title & description",
                "list_voices": "🎙 picking a voice"}.get(tool, f"🎬 studio: {tool}")
    if name == "ToolSearch":
        return "🧰 loading tools"
    return f"⚙️ {name}"


class Progress:
    """One Telegram status message, edited in place while a long task runs.

    It appears only once the task has run a few seconds (quick replies stay quiet),
    edits at most every few seconds, keeps the typing indicator alive, and ends with ✅/⚠️.
    """

    def __init__(self, title: str, chat_id: int | None = None, show_after: float = 6.0):
        import threading
        self.title, self.chat_id, self.show_after = title, chat_id or OWNER, show_after
        self.lines: list[str] = []
        self.current = ""
        self.mid = None
        self.t0 = time.time()
        self._last_edit = 0.0
        self._dirty = True
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._tick, daemon=True)
        self._thread.start()

    def _render(self, final: str | None = None) -> str:
        secs = int(time.time() - self.t0)
        head = final or f"{self.title} ({secs // 60}m {secs % 60:02d}s)"
        body = [f"✓ {esc(l)}" for l in self.lines[-7:]]
        if self.current and not final:
            body.append(f"▸ <b>{esc(self.current)}</b>")
        return head + ("\n" + "\n".join(body) if body else "")

    def _flush(self, force: bool = False, final: str | None = None) -> None:
        if time.time() - self.t0 < self.show_after and not final:
            return
        if not force and time.time() - self._last_edit < 4:
            return
        text = self._render(final)
        if self.mid is None:
            if final and time.time() - self.t0 < self.show_after:
                return                          # finished quickly: no status message at all
            r = tg("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML", disable_notification=True)
            self.mid = (r.get("result") or {}).get("message_id")
        else:
            tg("editMessageText", chat_id=self.chat_id, message_id=self.mid, text=text, parse_mode="HTML")
        self._last_edit = time.time()
        self._dirty = False

    def _tick(self) -> None:
        n = 0
        while not self._stop.wait(2):
            n += 1
            if n % 2 == 0:
                tg("sendChatAction", chat_id=self.chat_id, action="typing")
            with self._lock:
                if self._dirty or n % 7 == 0:   # refresh the clock every ~15s even when idle
                    self._flush()

    def step(self, label: str) -> None:
        with self._lock:
            if self.current and self.current != label:
                self.lines.append(self.current)
            self.current = label
            self._dirty = True

    def note(self, text: str) -> None:
        self.step(f"💭 {_short(text, 90)}")

    def done(self, ok: bool = True, msg: str = "") -> None:
        self._stop.set()
        with self._lock:
            if self.current:
                self.lines.append(self.current); self.current = ""
            secs = int(time.time() - self.t0)
            final = (f"✅ Done in {secs // 60}m {secs % 60:02d}s" if ok else f"⚠️ Stopped after {secs // 60}m {secs % 60:02d}s") \
                + (f": {esc(msg)}" if msg else "")
            self._flush(force=True, final=final)


# ---------------------------------------------------------------- reports as phone-friendly PDFs
REPORT_CSS = """
@page { size: 120mm 210mm; margin: 9mm 8mm 11mm; }
body { font: 10.5pt/1.5 -apple-system, "Segoe UI", Roboto, "Noto Sans", sans-serif; color: #15181f; }
h1 { font-size: 17pt; line-height: 1.2; margin: 0 0 4mm; color: #0b1640; }
h2 { font-size: 12.5pt; margin: 6mm 0 2mm; padding-bottom: 1mm; border-bottom: 1px solid #dfe3ec; color: #1a2a66; }
h3 { font-size: 11pt; margin: 4mm 0 1mm; }
p, li { orphans: 2; widows: 2; } ul, ol { padding-left: 5mm; } li { margin: 1mm 0; }
a { color: #2848d6; text-decoration: none; word-break: break-all; }
code { font-size: 9pt; background: #f1f3f8; padding: 0 2px; border-radius: 2px; }
pre { font-size: 8.5pt; background: #f1f3f8; padding: 2mm; white-space: pre-wrap; }
table { border-collapse: collapse; width: 100%; font-size: 9pt; margin: 2mm 0; }
th, td { border: 1px solid #dfe3ec; padding: 1mm 1.5mm; text-align: left; vertical-align: top; }
th { background: #f1f3f8; }
blockquote { margin: 2mm 0; padding: 1mm 3mm; border-left: 3px solid #7c9cff; color: #444; }
.brand { font-size: 8pt; color: #7a8299; margin-bottom: 3mm; letter-spacing: .3px; }
"""


def markdown_to_pdf(md_path: pathlib.Path, title: str = "") -> pathlib.Path:
    """Render a Markdown report to a phone-sized PDF using the local headless Chromium."""
    import markdown as md
    from playwright.sync_api import sync_playwright
    src = md_path.read_text(errors="replace")
    # Python-Markdown needs 4-space nesting; models usually write 2-space nested bullets
    src = re.sub(r"(?m)^((?:  )+)(?=(?:[-*+]|\d+\.)\s)", lambda m: "    " * (len(m.group(1)) // 2), src)
    body = md.markdown(src, extensions=["tables", "fenced_code", "sane_lists"])
    page_html = (f"<!doctype html><html><head><meta charset='utf-8'><title>{esc(title)}</title>"
                 f"<style>{REPORT_CSS}</style></head><body><div class='brand'>👁 ARGUS RESEARCH · "
                 f"{time.strftime('%d %b %Y')}</div>{body}</body></html>")
    pdf = md_path.with_suffix(".pdf")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.set_content(page_html, wait_until="load")
        pg.pdf(path=str(pdf), prefer_css_page_size=True, print_background=True,
               display_header_footer=True, header_template="<span></span>",
               footer_template="<div style='font-size:7pt;color:#999;width:100%;text-align:center'>"
                               "<span class='pageNumber'></span> / <span class='totalPages'></span></div>")
        b.close()
    return pdf


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


def claude(prompt: str, timeout: int = 900, resume: str | None = None, guest: bool = False,
           progress: "Progress | None" = None, disallow: list[str] | None = None) -> dict:
    """Run one headless Claude Code turn, streaming events so progress can be shown live.

    Owner mode runs in the repo dir (CLAUDE.md, .claude/settings.json, .mcp.json apply).
    Guest mode runs in ~/argus-guest with web tools only and no secrets, db, notes or MCP.
    `disallow` removes extra tools for this turn (e.g. rendering from chat).
    Returns the final envelope: {result, session_id, is_error, ...} plus _seconds and _tools.
    """
    import tempfile
    cmd = [str(CLAUDE_BIN), "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if guest:
        cmd += GUEST_TOOLS
    if disallow:
        cmd += ["--disallowedTools", ",".join(disallow)]
    if resume:
        cmd += ["--resume", resume]
    t0 = time.time()
    final: dict | None = None
    tools_used: list[str] = []
    pending_text = ""
    with tempfile.TemporaryFile(mode="w+") as errf:
        p = subprocess.Popen(cmd, cwd=GUEST_DIR if guest else ROOT, stdout=subprocess.PIPE, stderr=errf,
                             text=True, env=_claude_env(guest), bufsize=1)
        import threading
        killer = threading.Timer(timeout, p.kill)
        killer.start()
        try:
            for line in p.stdout:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "assistant":
                    for b in (ev.get("message") or {}).get("content") or []:
                        if b.get("type") == "text" and b.get("text", "").strip():
                            pending_text = b["text"]            # may be the final answer; shown only if work follows
                        elif b.get("type") == "tool_use":
                            tools_used.append(b.get("name", ""))
                            if progress:
                                if pending_text:
                                    progress.note(pending_text)
                                progress.step(describe_tool(b.get("name", ""), b.get("input") or {}))
                            pending_text = ""
                elif ev.get("type") == "result":
                    final = ev
            p.wait()
        finally:
            killer.cancel()
        if final is None:
            errf.seek(0)
            if time.time() - t0 >= timeout - 1:
                raise ClaudeError(f"timed out after {timeout}s")
            raise ClaudeError(errf.read()[-1500:] or f"exit {p.returncode}")
    final["_seconds"] = round(time.time() - t0)
    final["_tools"] = tools_used
    if final.get("is_error"):
        raise ClaudeError(str(final.get("result", ""))[:1500])
    return final


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
