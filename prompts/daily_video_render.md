The owner has **approved** today's {{SERIES}} script. Render it now.

1. Read the approved script at `{{PATH}}`. Don't change its content. Only fix something if `validate_script` rejects it.
2. Run `render_video`, then poll `get_job` about every 20 seconds until it finishes, which usually takes 1-3 minutes.
3. Run `export_video` with a 2-3 word `folder_name` such as "{{SERIES}}", then run `get_outputs`.
4. Never call `confirm_job` and never switch to a paid voice. If the job comes back `needs_confirm`, stop and say so.

Final reply (Telegram HTML subset only: `<b>`, `<i>`, `<a href>`):
🎬 <b>{{SERIES}} ready</b>: the title
- the download link and the job page link
- the YouTube title and description from `get_outputs`, plus a suggested Instagram caption (2-3 lines plus hashtags)
- a reminder: "Turn on Instagram's AI label (AI voice)."
