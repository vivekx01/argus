"""Daily videos: AI Daily and Finance Daily, drafted every evening, rendered only after the owner taps ✅.

Pipeline per series (specialist agents):
  1. pick today's solid channel stories (score >= 7, last 24h), skip the day if fewer than 2 distinct stories
  2. video-producer drafts a 60-90 s script (YAML), validates and previews it; rendering is disabled
  3. fact-checker independently verifies every on-screen claim
  4. video-producer fixes or drops anything not confirmed, then re-validates
  5. the owner gets the draft + fact-check summary with ✅ Render / ✋ Skip
  6. on ✅, video-producer renders and exports (job: dailyrender)
"""
from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

from .lib import DATA, claude, db, esc, extract_json, kv_get, kv_set, log_run, prompt_file, send

IST = ZoneInfo("Asia/Kolkata")
VIDEOS = DATA / "videos"
NO_RENDER = ["mcp__ccstudio__render_video", "mcp__ccstudio__export_video", "mcp__ccstudio__confirm_job"]
MIN_SCORE, MIN_STORIES = 7, 2

SERIES = {
    "ai": {"name": "AI Daily", "emoji": "🤖", "theme": "tech", "cats": ("ai", "research", "ai_finance"),
           "voice": "af_bella"},
    "finance": {"name": "Finance Daily", "emoji": "💹", "theme": "finance", "cats": ("finance", "india", "ai_finance"),
                "voice": "af_heart"},
}


def _key(date: str, series: str) -> str:
    return f"daily:{date}:{series}"


def draft(series: str, progress=None) -> str:
    from .jobs import resolve_links
    s = SERIES[series]
    c = db()
    date = dt.datetime.now(IST).strftime("%Y-%m-%d")
    rows = c.execute(f"""SELECT id, title, source, category, score, why, hook, cluster, seen FROM items
                         WHERE watch_id=1 AND status='triaged' AND score >= ? AND fetched_at > datetime('now','-24 hours')
                         AND category IN ({','.join('?' * len(s['cats']))}) ORDER BY score DESC, seen DESC LIMIT 15""",
                     (MIN_SCORE, *s["cats"])).fetchall()
    clusters = {r["cluster"] or r["id"] for r in rows}
    if len(clusters) < MIN_STORIES:
        send(f"{s['emoji']} <b>No {s['name']} today</b>: only {len(clusters)} solid stor{'y' if len(clusters) == 1 else 'ies'} "
             f"(score {MIN_SCORE}+) in the last 24h. Skipping rather than padding.", kind="daily")
        log_run("daily", True, f"{series}: skipped, {len(clusters)} solid stories")
        return "skipped"
    try:
        resolve_links([r["id"] for r in rows], limit=15)
    except Exception:
        pass
    items = [dict(r) | {"url": (c.execute("SELECT COALESCE(NULLIF(real_url,''), url) FROM items WHERE id=?",
                                          (r["id"],)).fetchone()[0])} for r in rows]
    VIDEOS.mkdir(parents=True, exist_ok=True)
    path = VIDEOS / f"{date}-{series}.yaml"
    narration = VIDEOS / f"{date}-{series}-narration.md"
    if progress:
        progress.step("🎬 producer: drafting the script")
    out = claude(prompt_file("daily_video_draft", SERIES=s["name"], THEME=s["theme"], VOICE=s["voice"], DATE=date,
                             PATH=str(path), NARRATION_PATH=str(narration), ITEMS=json.dumps(items, ensure_ascii=False)),
                 timeout=2400, agent="video-producer", disallow=NO_RENDER, label="daily-draft", progress=progress)
    plan = extract_json(out["result"])
    if not path.exists():
        raise RuntimeError("producer did not save the script")

    # story editor: independent review of the narration; up to 2 revision rounds until it "ships"
    review = {}
    for rnd in range(3):
        if progress:
            progress.step(f"📝 story editor: reviewing the narration (round {rnd + 1})")
        ed = claude(f"Review the narration in {narration} for the {s['name']} video. Follow your editor instructions.",
                    timeout=900, agent="story-editor", label="story-edit", progress=progress)
        review = extract_json(ed["result"])
        if review.get("verdict") == "ship" or rnd == 2:
            break
        if progress:
            progress.step(f"✍️ producer: rewriting ({review.get('biggest_problem', '')[:60]})")
        out = claude("The independent story editor sent your narration back:\n"
                     f"{json.dumps(review, ensure_ascii=False)}\n\nRevise {narration} to fix these, keeping the story "
                     f"true to the sources. Then rebuild {path} from it, re-run validate_script and preview_video, and "
                     "reply with the same JSON as before. Do NOT render.",
                     timeout=1800, resume=out.get("session_id"), agent="video-producer", disallow=NO_RENDER,
                     label="daily-revise", progress=progress)
        plan = extract_json(out["result"])

    if progress:
        progress.step("✅ fact-checker: verifying every on-screen claim")
    fc = claude(prompt_file("fact_check", CLAIMS=json.dumps(plan.get("claims", []), ensure_ascii=False)),
                timeout=1800, agent="fact-checker", label="factcheck", progress=progress)
    verdicts = extract_json(fc["result"])
    flagged = [v for v in verdicts if v.get("verdict") != "confirmed"]
    if flagged:
        if progress:
            progress.step(f"🛠 producer: fixing {len(flagged)} flagged claim(s)")
        out = claude(f"The independent fact-checker flagged these claims in {path}:\n"
                     f"{json.dumps(flagged, ensure_ascii=False)}\n\nFor each one, use the corrected value if one is "
                     "given with a source, or remove the claim. Never keep an unconfirmed number on screen. Update "
                     "the YAML file, re-run validate_script and preview_video, and reply with the same JSON as before "
                     "(only confirmed or corrected claims in `claims`).",
                     timeout=1800, resume=out.get("session_id"), agent="video-producer", disallow=NO_RENDER,
                     label="daily-fix", progress=progress)
        plan = extract_json(out["result"])
        # the fix pass can introduce new wording or sources: check the final claims once more
        if progress:
            progress.step("🔁 fact-checker: re-checking the fixed script")
        fc = claude(prompt_file("fact_check", CLAIMS=json.dumps(plan.get("claims", []), ensure_ascii=False)),
                    timeout=1800, agent="fact-checker", label="factcheck-recheck", progress=progress)
        first_pass = len(flagged)
        verdicts = extract_json(fc["result"])
        flagged = [v for v in verdicts if v.get("verdict") != "confirmed"]
    else:
        first_pass = 0

    kv_set(c, _key(date, series), json.dumps({"path": str(path), "plan": plan, "verdicts": verdicts, "review": review,
                                              "status": "awaiting", "series": series}))
    ok = sum(v.get("verdict") == "confirmed" for v in verdicts)
    def _num(x):
        n = str(x.get("number") or "").strip()
        return "" if not n or n.lower().startswith(("none", "n/a", "no ")) else f": {esc(n)}"
    stories = "\n".join(f"• <b>{esc(x.get('headline', ''))}</b>{_num(x)} · "
                        f"<a href=\"{esc(x.get('source_url', '#'))}\">{esc(x.get('source', 'source'))}</a>"
                        for x in plan.get("stories", []))
    send(f"{s['emoji']} <b>{s['name']} draft</b>: {esc(plan.get('title', ''))}\n"
         f"<i>~{plan.get('seconds', '?')}s · voice {esc(plan.get('voice', s['voice']))} · validated and previewed</i>\n\n"
         f"{stories}\n\n🎣 <b>Hook:</b> {esc(plan.get('hook', ''))}\n"
         f"📝 <b>Story editor:</b> {review.get('overall', '?')}/10 ({esc(review.get('verdict', '?'))})\n"
         f"✅ <b>Fact-check (final script):</b> {ok}/{len(verdicts)} confirmed"
         f"{f' · {first_pass} fixed or dropped earlier' if first_pass else ''}\n"
         f"{_warning(review, flagged)}"
         f"{('📝 ' + esc(plan.get('notes', ''))) if plan.get('notes') else ''}\n\n"
         f"{_narration_block(narration)}"
         f"<i>Want changes? Tell me in chat before rendering.</i>",
         kind="daily",
         buttons=[[{"text": "✅ Render", "callback_data": f"daily:{date}:{series}:yes"},
                   {"text": "✋ Skip", "callback_data": f"daily:{date}:{series}:no"}]])
    log_run("daily", True, f"{series}: drafted '{plan.get('title', '')[:60]}', {ok}/{len(verdicts)} confirmed, "
                           f"{len(flagged)} flagged ({out['_seconds']}s)")
    return "drafted"


def _warning(review: dict, flagged: list) -> str:
    """Say plainly when a draft did not pass its gates, so a render is a conscious choice."""
    out = []
    if review.get("verdict") != "ship":
        out.append(f"story editor didn't sign off ({esc(str(review.get('biggest_problem', ''))[:160])})")
    if flagged:
        out.append(f"{len(flagged)} claim(s) still not confirmed: "
                   + "; ".join(esc(str(v.get('claim', ''))[:70]) for v in flagged[:3]))
    return f"⚠️ <b>Not ready:</b> {' · '.join(out)}\n" if out else ""


def _narration_block(narration) -> str:
    """The spoken script, collapsed, so the owner can read the story before approving a render."""
    try:
        text = narration.read_text(errors="replace")
    except OSError:
        return ""
    body = text.split("##", 1)[-1] if "##" in text else text
    return f"<blockquote expandable>🎙 {esc(body.strip()[:2500])}</blockquote>\n"


def render(date: str, series: str, progress=None) -> str:
    c = db()
    meta = json.loads(kv_get(c, _key(date, series), "{}") or "{}")
    if not meta or meta.get("status") != "awaiting":
        send(f"That {SERIES[series]['name']} draft isn't waiting for a render (status: {meta.get('status', 'none')}).")
        return "nothing to render"
    meta["status"] = "rendering"
    kv_set(c, _key(date, series), json.dumps(meta))
    out = claude(prompt_file("daily_video_render", PATH=meta["path"], SERIES=SERIES[series]["name"]),
                 timeout=3000, agent="video-producer", label="daily-render", progress=progress)
    rendered = "mcp__ccstudio__render_video" in out.get("_tools", [])
    meta["status"] = "rendered" if rendered else "failed"
    kv_set(c, _key(date, series), json.dumps(meta))
    send(out["result"].strip(), kind="video")
    log_run("video", rendered, f"{SERIES[series]['name']} {date}{'' if rendered else ' (no render happened)'} "
                               f"({out['_seconds']}s)")
    return meta["status"]


def skip(date: str, series: str) -> None:
    c = db()
    meta = json.loads(kv_get(c, _key(date, series), "{}") or "{}")
    if meta:
        meta["status"] = "skipped"
        kv_set(c, _key(date, series), json.dumps(meta))
