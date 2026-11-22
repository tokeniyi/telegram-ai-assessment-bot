# Phase 08 — Recovery without the admin: Phase overview

| | |
|---|---|
| Roadmap stage | Stage 8 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stoic-babbage-iym3c3` (restarted from `main` after PR #16 was merged) |
| Pull request | not opened yet |
| Status | In progress |

## Objective

Two failures still waited for an admin to act:

- **Unsent assessments:** an assessment message that could not be sent stayed unsent. `/status`
  showed it but could not fix it.
- **Failed grading:** grading that failed stayed failed until someone ran `/regrade`, even after the
  AI provider was back.

This phase gives admins `/resend`, and makes the bot retry failed grading by itself.

Requested by the user on 2026-11-22 ("start the resend and auto-regrade follow-ups"). It was added
to the roadmap as Stage 8; Scale and limits became Stage 9 (references in the Phase 03–05 docs
updated).

## Scope

In:

- `/resend <id>` for an open assessment.
  - It queues the failed messages again, once, and sends them.
  - It is suggested in the publish report and in `/status`.
- Automatic retry of failed grading by the background work.
  - It runs only while the AI provider answers, after a delay that doubles, with a limit per
    submission.
  - Students get their results. Admins get a batched "retried automatically" message.
  - The failure notice, the AI-back message and `/status` say that the bot retries.
- `AUTO_REGRADE_ATTEMPTS` (default 3; 0 turns it off).

Out:

- Probing the AI provider in the background to notice it is back (Phase 05 decision, kept).
- Resending automatically.
- Resending to users who blocked the bot.
- Anything from Stage 9.

## Expected outcome

- An admin can reach the students an assessment did not reach, without duplicates from double taps
  or two processes.
- A grading failure caused by an outage is graded and sent without anyone acting, once the provider
  answers again.
- Both hold with two bot processes on one database.

## Builds on

- **Phase 05, outstanding issues:** "Failed grading is not retried automatically when the AI is back"
  and "No command to resend failed assessment messages". Both are addressed here.
- **Phase 05 decisions:**
  - Kept: no background probing of the AI provider. The bot uses `ai_health` and retries only when
    the last AI request got a reply.
  - Reused: batched admin notices with an `admin_notices` row and a per-row "due" flag.
- **Phase 06, outstanding issues:** "No `/resend`" and "Failed grading is not retried automatically".
- **Phase 06 decisions:**
  - Every new command goes in the registry.
  - Suggestions use real ids.
  - Commands that change state are typed, not buttons: `/resend` is typed only.
- **Phase 03:**
  - `claim_for_regrade`'s lease is reused, so an automatic retry and a manual `/regrade` never grade
    one submission at once.
  - "No repeat when in doubt" for assessment messages is why resending stays the admin's decision.
- **Phase 07:** `Database.OPEN` decides what can be resent. Nothing is resent after a deadline.
