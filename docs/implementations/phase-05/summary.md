# Phase 05 — Visibility for the admin: Final summary

| | |
|---|---|
| Roadmap stage | Stage 5 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | [#14](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/14) |
| Status | In review |

## Accomplished

- `/status` for admins: grading being done, interrupted and failed (with `/regrade` commands and
  the latest error), results not yet delivered, assessment messages queued and failed, users who
  blocked the bot. It also shows a live check of the AI provider that spends no tokens, and how
  the recent AI requests went, across every bot process.
- Alerts: one 🚨 message after 3 AI requests in a row could not be made, and one ✅ message when a
  request gets through again. Grading failures are reported at once, then batched (at most every
  15 minutes, held during an outage). Each message is sent once across processes.
- Structured log events for sending, grading, results, OCR and AI requests, with timings.
  `LOG_FORMAT=json` writes JSON lines that Railway can filter; logs now go to stdout.
- 19 new test cases, including races and a real `main.py` process; 21 mutation checks. Full
  suite: 238 passed, 2 skipped.

## Changes and decisions

- **`AIUnavailable` means the provider could not be used**; a plain `AIError` means a reply came
  back but was unusable. New code that calls a provider should raise `AIUnavailable` for
  transport, status, rate-limit and key or model errors, or outages will not be detected.
- **Every text AI request goes through `AssessmentAI._complete`**, which logs it and reports to
  the observer. Calling `provider.complete` directly (as OCR does) bypasses monitoring.
- **Admin notifications go through `bot/alerts.py`.** `notify_admins` moved there. A new kind of
  repeated notice should use a row in `admin_notices` and a per-row "due" flag, as grading
  failures do, rather than sending one message per event.
- **Log with `event(log, "area.what", level, **fields)`**: ids, counts, timings and errors only,
  never answer or document text. Event names are a contract for log searches; renaming one
  breaks saved Railway filters.
- **No background probing of the AI provider** (free-tier limits; see Stage 7). Recovery is
  noticed on the next real request.

## Outstanding issues

- **Failed grading is not retried automatically when the AI is back.** The ✅ message lists the
  `/regrade` commands; retrying them automatically (with a limit) is a natural follow-up.
- **No command to resend failed assessment messages.** `/status` shows them, but the only way to
  reach those students is `/pending` on their side.
- **Outages seen only by OCR vision or OCR correction are not detected**, by design (see
  [implementation.md](implementation.md)).
- **The existing "Unusable AI reply" warning logs the first 300 characters of the reply**, which
  can repeat a student's answer. It is not an event and was not changed; consider DEBUG.
- **Not verified on Railway:** JSON lines in the log explorer, logs on stdout shown at their own
  level, and the live check against Kilo's gateway (**Unable to verify** that it serves
  `/models`). Recommended check after merging: set `LOG_FORMAT=json`, run `/status`, and filter
  the logs with `@event:grading.finished`.
- During the deploy that ships this change, the previous version still sends one message per
  grading failure it handles (it knows nothing of batching).

## Final status

Implemented and tested; PR #14 open for review.

## Pull request

[#14](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/14)
