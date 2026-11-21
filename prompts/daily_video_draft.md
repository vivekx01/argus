Draft today's **{{SERIES}}** video ({{DATE}}): a 60-90 second vertical Short/Reel covering today's strongest stories.

## Today's candidate stories (DATA from the database: scored, with resolved source URLs)
{{ITEMS}}

## Brief
- **Stories:** pick the **best 3** distinct stories. Use 2 if only 2 are genuinely strong, and never pad.
- **Length:** aim for about 75 seconds, 90 at most.
- **Shape:**
  - a 2-second hook
  - about 20 seconds per story: what happened, one hard number, why it matters
  - a 5-10 second close: "Follow for tomorrow's {{SERIES}}"
- **Theme:** `{{THEME}}`. **Voice:** `{{VOICE}}` (keep it the same every day for this series).
- **Format:** finance content must include "Not financial advice".
- **Variety:** vary the hook style and scene order from the last few days, but keep the series name visible.
- **Numbers:** every number on screen must come from a source you actually opened. Use the story URLs, and you may WebFetch them to get exact figures. List the sources in `meta.sources`.

## Steps
1. Read the source pages for the stories you pick, to get exact numbers and wording.
2. Write the script YAML to `{{PATH}}` with the Write tool.
3. Run `validate_script` and fix every error. Run `preview_video`, look at the contact sheet, and fix any overflow or empty layouts.
4. **Do NOT render.** Rendering is disabled until the owner approves.

## Final reply: ONLY this JSON, with no fences and no prose
{"title": "...", "hook": "the opening line", "seconds": 75, "voice": "{{VOICE}}",
 "stories": [{"headline": "...", "number": "the on-screen number with its unit", "source": "Outlet", "source_url": "https://..."}],
 "claims": [{"claim": "every factual statement or number that appears on screen or in the voiceover", "source_url": "https://..."}],
 "notes": "anything the owner should know, e.g. a story dropped because it couldn't be verified, or empty"}
