# Side feature — Study check-ins: Phase overview

| | |
|---|---|
| Roadmap stage | None. This is a side feature, outside the staged roadmap ([ROADMAP.md](../../../ROADMAP.md), "Outside the stages") |
| Branch | `claude/modest-allen-wfagwm` |
| Pull request | [#19](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/19) |
| Status | Merged |

## Objective

So far the bot speaks first only about assessments: when one is published, before its deadline and
when a result is ready. Between assessments, a student hears nothing. This feature lets students
choose to get a short message every few days. The bot's AI writes it. It encourages them to review
and invites them to reply if they are stuck.

The user asked for it on 2026-11-22, outside the roadmap ("aside from the roadmap but I still want
it documented"). They agreed to these choices:

1. Opt-in, with an easy way out.
2. Not too often, at a sensible time.
3. Personal from the first version.
4. A built-in message when the AI is down.
5. Visible to admins in `/status`.

## Scope

In:

- **Opt-in.** Students are offered check-ins once, with their first `/start`. `/checkins on|off|pause`
  is added to the command registry. Buttons on the offer, the status and each check-in do the same.
- **Schedule.**
  - Every `CHECKIN_DAYS` (3) days at `CHECKIN_HOUR` (17), in `TIMEZONE`.
  - Put off when the student just used the bot, or got a deadline reminder that day.
  - Sent less often after check-ins go unanswered.
- **Content.** The AI writes each check-in from facts in the database: assessments still to answer,
  questions the student lost points on, and what they recently asked.
- **Built-in message** when the AI provider is down or its reply is unusable.
- **Replies.** Each check-in is saved in the chat history, so a reply is ordinary chat with context.
- **`/status`** shows who receives check-ins, how many were sent, failed or stopped because the
  student blocked the bot.

Out:

- Study-session memory. It does not exist yet. The design leaves room for it as one more fact
  source (`bot/checkins.py`, `SOURCES`).
- A check-in hour or interval chosen by each student. There is one bot-wide setting.
- Offering check-ins to users who registered before this feature, unless they send `/start` again
  or use `/checkins` (see [summary.md](summary.md), outstanding issues).
- Offering check-ins to admins. They can still use `/checkins`.

## Expected outcome

- A student who says yes gets one AI-written check-in at the set hour every few days. A student who
  does not say yes gets none.
- Each due check-in is sent at most once, even with two bot processes on one database.
- An AI outage still produces a check-in, using the built-in text.
- A student who blocked the bot is not messaged again.

## Builds on

- **Stage 7 (phase 07): reminders before a deadline.**
  - The "claim, mark as sent, then send" pattern (at most once) is reused.
  - The `reminders` table is read, to skip a day on which a deadline reminder was sent.
- **Stage 6 (phase 06):** every command is in the registry, and suggestions are made with
  `commands.Suggestion`. `/checkins` is in the registry with an example. `tests/test_guidance.py`
  checks it against the handlers.
- **Stage 5 (phase 05):** `ai_health` follows the provider. Check-ins do not call it while it counts
  as down (`AI_DOWN_AFTER` failures in a row). Phase 05 decided against background probing, and
  this feature does not probe either.
- **Stage 3 (phase 03) and CLAUDE.md:** anything that must happen once is decided by the database.
  The due row is taken with `FOR UPDATE SKIP LOCKED` and moved to its next time in the same
  transaction.
- **Chat (PR #3):** the chat history in `user_sessions` already gives the AI earlier messages. The
  check-in is added to it as an assistant turn.
- **Phase 08, outstanding issues:** none of them are addressed here. Its decision on retries without
  probing is respected.
