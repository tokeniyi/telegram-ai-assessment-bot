# Phase 08 — Recovery without the admin: Final summary

| | |
|---|---|
| Roadmap stage | Stage 8 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stoic-babbage-iym3c3` (restarted from `main` after PR #16 was merged) |
| Pull request | [#17](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/17) |
| Status | Merged |

## Accomplished

- `/resend <id>` sends an open assessment again to the users whose message failed.
  - It sends each one copy, even with a double `/resend` or two processes.
  - It is suggested by the publish report and `/status`.
- Failed grading is retried by the bot itself once the AI provider answers again.
  - Up to `AUTO_REGRADE_ATTEMPTS` (3) times, 5/10/20 minutes after the failure.
  - Students get their results, and admins one batched "retried automatically" message.
  - `/regrade` is still offered.
- 16 new tests; full suite 324 passed, 2 skipped. 14 of 14 removed safeguards are caught by a test.
- Roadmap: this work became Stage 8, and Scale and limits became Stage 9. Stage 7 is marked merged
  (separate commit).

## Changes and decisions

- **The bot retries failed grading only when the last AI request got a reply**
  (`ai_health.consecutive_failures = 0`). There is still no background probing (Phase 05).
- **Automatic retries and `/regrade` share one lease** (`status` failed → pending). A new way to
  grade a submission must claim it the same way.
- **`mark_submission_failed(notify=False)`** is for a failure the admins already know about. New
  retry paths should use it, so that they do not repeat notices.
- **A second batched admin notice kind, `auto_regraded`,** follows the Phase 05 pattern (an
  `admin_notices` row and a per-row due flag).
- **`/resend` is typed, never a button.** Resending can duplicate a message that timed out. Only the
  admin decides that.
- **Stage numbering:** Stage 8 is this phase. Scale and limits is Stage 9.

## Outstanding issues

- **After an outage, a bot with no traffic does not notice the provider is back.** Failed grading
  waits for the first AI request that gets a reply (a chat message, a submission, `/generate` or
  `/regrade`). A token-free probe (the provider check behind `/status`) could be considered, which
  would revisit the Phase 05 decision.
- **`/resend` cannot reach users who blocked the bot** (by design), nor users who started the bot
  after the assessment was published. They still find it in `/pending`.
- **`/results` does not say that failed grading is retried automatically;** `/status` and the
  notices do.
- **Not verified on real Telegram clients, on Railway, or with a real AI outage.**

## Final status

Implemented and tested; PR #17 merged into `main`.

## Pull request

[#17](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/17)
