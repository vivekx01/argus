# Argus v2: a companion that learns

**Decided 2026-11-22 with the owner.** v1 was a news bot: a headline feed, too many messages, everything framed as content, no memory of the owner, no initiative.

v2 is a **mentor, personal assistant and friend in one**. It knows the owner, keeps learning about what they care about, talks with them about anything, and brings them interesting things on its own.

## The loop
**talk → learn → share → talk**

1. **Talk.** In conversation Argus learns what the owner is into, what they know, what they think and what they're unsure about.
2. **Learn.** On its own schedule it reads properly about those interests, plus some of its own curiosity. It writes what it learns into its notes.
3. **Share.** When it finds something genuinely interesting for this owner, it brings it up, as a friend would.
4. **Talk again.** Conversations draw on its notes and on past discussions, so they build over time.

## Owner decisions
| Question | Decision |
|---|---|
| Personality | **A curious, opinionated friend.** It has its own views, disagrees, asks questions back and is a bit playful. Always honest and sourced, and never sycophantic. |
| Outreach | **Chatty:** up to about 4-5 unprompted messages a day (shares, questions, follow-ups). |
| New interests | **Auto-adopt and tell.** "I'll start reading up on psychology. Say stop if not." |
| Channel (Compound Code) | **Only when asked.** It's a companion first. Content angles come only on request, or as one optional line in the morning message. |

## Parts
| Part | What it is | Stored in |
|---|---|---|
| **Owner profile** | Interests and depth, what they already know, their opinions, open questions, how they like to talk. Updated after chats. | `notes/owner.md` plus a db table of interests |
| **Interests** | Every subject the owner cares about (AI, finance, software engineering, psychology, …). Each has why they care, their level, the angle that hooks them, sources and how actively to follow it. This replaces "watches". | db `interests` (watches migrate in) |
| **Reading sessions** | A few bounded sessions a day. Each one picks an interest (weighted by the owner's engagement, about 20% Argus's own curiosity) and reads deeply: essays, papers, discussions, data. It then writes notes and queues anything worth sharing. | job `explore`, db `readings` |
| **Notes** | Linked topic notes: key ideas, sources, Argus's own view, open questions, and what the owner thinks. Read before every conversation on that topic. | `notes/topics/*.md` plus an FTS index |
| **Conversation memory** | A summary of each chat session (topics, what was concluded, what the owner said they'd do or wondered about), so a new session continues the relationship. | db `conversations` |
| **Share queue** | Candidate messages (a find, a question, a follow-up), each scored for interest. The best are sent within the outreach budget. | db `share_queue` |
| **News** | Fetch and triage stay. AI and finance become interests, and big-story alerts stay for genuinely huge news. | existing |

## Outreach rules (chatty, never spammy)
"Spammy" in v1 meant long lists that repeated themselves, not the number of messages. In v2:
- **Short:** every message is 1-4 sentences, like a text from a friend. Longer things are offered ("want the long version?"), not dumped.
- **Budget:** at most about 5 unprompted messages a day, including the morning one. On a quiet day, send fewer.
- **Quiet hours:** 22:30-07:30 IST. Nothing goes out then except a truly huge alert.
- **Fresh:** never repeat what the owner has already been told.
- **Varied:** finds, questions ("you said X last week, and here's a study that disagrees"), follow-ups on past chats, and connections across interests.
- **Self-correcting:** if the owner ignores a kind of message, send less of it. Replies and 👍 mean more of it.

## Morning message
One message: the news that actually changed (the reworked brief), plus one thing Argus learned or is thinking about. Optionally one content line.

## Keep, cut, change
- **Keep:** fetch and triage (Haiku), alerts at 9+, `/research` PDFs, the paper library, the fact-checker, guests, Telegram progress messages and the server setup.
- **Cut:** separate watch digests and the weekly report (both fold into the morning message and conversation), and the hook or reel framing on every item.
- **Change:**
  - Chat persona becomes the opinionated friend.
  - Sessions continue with memory instead of resetting blind.
  - The dashboard becomes "Argus's mind": profile, interests, notes, reading log and share history.

## Usage budget (Claude Pro, shared with chat)
- Triage stays on Haiku.
- Reading sessions get a daily cap, about 3 sessions with bounded turns, and are skipped if the owner chatted heavily that day.
- The owner's conversations always take priority over exploring.

## Build phases
1. **Know the owner:** the profile, conversation memory across sessions, the friend persona, and auto-adopted interests.
2. **Learn:** the interests table (migrating watches), reading sessions and topic notes.
3. **Share:** the share queue, outreach budget and quiet hours, the morning message rework and the "mind" dashboard.
4. **New senses:** market data history and transcripts (earnings calls, podcasts).
