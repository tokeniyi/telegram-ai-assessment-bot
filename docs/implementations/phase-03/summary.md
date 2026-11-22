# Phase 03 — Work that survives a restart: Final summary

| | |
|---|---|
| Roadmap stage | Stage 3 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | [#12](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/12) |
| Status | Merged |

## Accomplished

- "Send to all users" publishes once, however many taps or bot processes. The status change and
  the send queue (`deliveries`) are one transaction.
- Sending resumes after a restart: leftovers are sent on start and every 30 s, each message by
  one process at a time (leases), with retries for errors that may pass.
- Grading resumes after a restart: an abandoned submission is graded again automatically, at
  most 3 runs, then left `failed` for `/regrade`. The student receives the result.
- Discard, Regenerate, `/close` and `/regrade` no longer race with publishing or grading.
- Existing databases are upgraded on start; work already done by the previous version is not redone.
- 35 new tests; full suite 200 passed, 2 skipped. Phase documentation for Stages 1 and 2
  reconstructed, and the documentation conventions added to `CLAUDE.md`.

## Changes and decisions

- **Exactly-once decisions live in PostgreSQL**: conditional `UPDATE … RETURNING`, primary keys and
  leases (`FOR UPDATE SKIP LOCKED`). New slow work should reuse `bot/handlers/work.py` rather than
  run inside a handler.
- **Assessments: no repeat when in doubt.** A timed-out or partly sent message is marked `failed`,
  not retried. **Results: repeat rather than lose.** An unconfirmed result is sent once more.
- **Leases are long** (5 min per message; grading `max(600 s, 4 × AI_TIMEOUT_SECONDS)`), so work in
  progress during a deploy is never duplicated. The cost is that abandoned work waits up to one
  lease before it is resumed.
- **Users who start the bot after publishing** are not queued; they find the assessment in
  `/pending`, as before.

## Outstanding issues

- **One remaining duplicate window:** a process that stops between Telegram accepting a message and
  the row being marked `sent` leaves a row that is sent again after its lease. Stage 4's clean
  shutdown (finish the current send before exiting) narrows this further.
- **Resuming waits for the lease.** After a crash, a half-sent assessment continues after up to
  5 minutes, and an abandoned grading after up to 10.
- **The previous version, during the deploy that ships this change,** does not know leases: its
  `/regrade` can grade a submission the new version is also grading (one grade is saved; the
  student could get two result messages). Avoid `/regrade` while the deploy is in progress.
- **Failed sends are only visible in the log and the publisher's report.** An admin view of failed
  deliveries and stuck grading belongs to Stage 5 (`/status`).
- **No pacing for large classes** beyond 50 ms between messages (Stage 8).
- Running these tests against a real Telegram bot and AI provider on Railway: **Not done** in this
  phase; recommended as a manual check after merging (publish to two accounts, redeploy mid-send).

## Final status

Implemented and tested; PR #12 merged on 2026-11-21. CI on its last commit: `test` and GitGuardian passed.

## Pull request

[#12](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/12)
