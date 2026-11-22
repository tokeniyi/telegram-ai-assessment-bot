# Side feature — Study check-ins: Final summary

| | |
|---|---|
| Roadmap stage | None. This is a side feature, outside the staged roadmap ([ROADMAP.md](../../../ROADMAP.md), "Outside the stages") |
| Branch | `claude/modest-allen-wfagwm` |
| Pull request | [#19](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/19) (the feature), [#20](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/20) (the one-time announcement) |
| Status | #19 merged; #20 in review |

## Accomplished

- **Opt-in check-ins.** Students are offered them once, with their first `/start`.
  `/checkins on|off|pause` and buttons change the choice.
- **Schedule.**
  - Every `CHECKIN_DAYS` (3) days at `CHECKIN_HOUR` (17:00, `TIMEZONE`).
  - Put off after recent activity (12 h) or a deadline reminder that day.
  - Less often after 2 or 3 unanswered in a row.
- **Written by the AI** from the student's open assessments, the questions they lost points on, and
  what they recently asked. A built-in message is used when the AI is down.
- **Sent at most once** per due time, with two processes too. A student who blocked the bot is not
  messaged again.
- **Replies go to the normal chat.** The check-in is in the chat history.
- **`/status`** has a "Study check-ins" section.
- **One-time announcement (#20).** Students who registered before the feature are asked the same
  question once, spread over passes (25 per 30 s).
- **Tests.** 21 new tests; the full suite has 345 passed, 2 skipped. Both safeguards removed in the
  mutation checks were caught.

## Changes and decisions

- **Check-ins are opt-in.** Nothing is sent to a student whose row is not `on`. Keep it that way:
  the user chose opt-in.
- **Due rows are claimed like Stage 7 reminders** (`Database.claim_checkin`).
  - The row is locked, `decide` runs, and `next_at` moves, all in one transaction, before the AI
    call and the send.
  - Any new kind of message the bot starts by itself should follow this pattern.
- **`checkins.SOURCES` is where study-session memory plugs in.**
  - A source is a function `(services, user_id) -> list[str]` of facts.
  - Reference answers must not be passed in.
- **Every non-admin user is asked once, ever.** A user with no `study_checkins` row has not been
  asked; the background announcement asks them. A later feature that wants a user never to be asked
  must give them a row.
- **`users.last_active_at`** is now set on every update. Later features can use it for "recently
  active".
- **The AI is not called while `ai_health.consecutive_failures >= AI_DOWN_AFTER`.** There is still
  no background probing (the Phase 05 decision).
- **The check-in is added to the chat history as an `Assistant` turn without a `Student` turn.** Code
  that reads the history in pairs must allow for this. `checkins.recent_questions` already does.

## Outstanding issues

- **Throughput.**
  - At most 10 check-ins per 30 s pass, so about 20 per minute.
  - All are due at the same hour. Ones not reached within 3 h count as "missed" and move to the
    next day.
  - Above roughly 3,600 opted-in students due at once, some would slip a day. The Stage 9 (Scale
    and limits) work could spread them out.
- **A check-in cut off by a crash** between the claim and the send is lost, by design (at most
  once). It is logged in `checkin_log` with no `source`.
- **A student who blocked the bot and comes back** must turn check-ins on again with `/checkins on`.
- **One bot-wide hour and interval.** There is no per-student choice. The built-in message is in
  English only.
- **During a deploy, the older version** answers `/checkins` with "I don't know" and leaves `chk:`
  buttons unanswered until it stops.
- **Unable to verify:**
  - the quality of check-ins from a real model;
  - real Telegram clients;
  - Railway.

## Final status

The feature: implemented and tested; PR #19 merged into `main`. The announcement: implemented and
tested; PR #20 open for review.

## Pull request

[#19](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/19) (the feature) · [#20](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/20) (the announcement)
