Write today's **{{SERIES}}** ({{DATE}}): a 60-90 second vertical Short/Reel for Compound Code. Follow your craft rules: story first, scenes last.

## Today's candidate stories (DATA: scored, with resolved source URLs)
{{ITEMS}}

## The brief
- **Find today's thread,** the one idea that connects the strongest stories. Hook on the thread.
- **Pick the lead story** and tell it properly (hook, context, "but…", "therefore…", meaning). Then add one or two quick hits, each brought in by a real bridge line. If no honest thread exists, do a single-story video on the best one instead, and say so in `notes`.
- **Format:** theme `{{THEME}}`, voice `{{VOICE}}` (this series' fixed voice), speed 1.0-1.05. Aim for about 75 seconds, 90 at most.
- **Close** with the day's takeaway, then "Follow for tomorrow's {{SERIES}}". Finance needs "Not financial advice".
- **Read the source pages** for the stories you use, to get the real context, nuance and exact numbers. Every on-screen number must come from a page you opened, listed in `meta.sources`.

## Files
- Narration: `{{NARRATION_PATH}}`. Write this first: the question, turn, so-what and angle lines, then the full spoken script with labelled beats.
- Script YAML: `{{PATH}}`. Build it from the narration once it passes your self-check rubric. Run `validate_script` and `preview_video`, and fix the problems. **Do NOT render.**

## Final reply: ONLY this JSON, with no fences and no prose
{"title": "...", "thread": "today's thread in one line", "hook": "the opening line", "seconds": 75, "voice": "{{VOICE}}",
 "stories": [{"headline": "...", "number": "the hero number said in this beat, or empty", "source": "Outlet", "source_url": "https://..."}],
 "claims": [{"claim": "every factual statement or number in the voiceover or on screen", "source_url": "https://..."}],
 "notes": "anything the owner should know, or empty"}
