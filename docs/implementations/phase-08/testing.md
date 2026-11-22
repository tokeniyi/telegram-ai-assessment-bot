# Phase 08 — Recovery without the admin: Testing and validation

| | |
|---|---|
| Roadmap stage | Stage 8 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stoic-babbage-iym3c3` (restarted from `main` after PR #16 was merged) |
| Pull request | [#17](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/17) |
| Status | Merged |

## Methodology

- **Through the bot** with the fake Telegram API and AI provider.
  - Assessment messages are made to fail with a Telegram timeout (`TimedOut`) or a 403 (blocked).
  - Grading is made to fail with `AIUnavailable` (the provider is down) or with unusable replies (the
    provider is up).
  - Admins type `/resend` and `/status`. Students submit and chat.
  - Assertions check who received what, how many times, the notices admins got, and the database
    state.
- **Time passing** is simulated by ageing `failed_at` and `admin_notices.notified_at` with SQL. The
  background work is run with `work.run_once`.
- **Two processes:**
  - Two harnesses on one database: two `/resend` at once, and two background passes at once.
  - Real threads with their own connections, for the automatic-retry claim, because harnesses in one
    event loop never overlap in the database (see [issues-and-fixes.md](issues-and-fixes.md), item 3).
- **Mutation checks:** 14 safeguards removed one at a time (script kept out of the repository), each
  caught by `tests/test_recovery_followups.py`. Removed for automatic retries:
  - waiting for the AI to answer;
  - the delay;
  - the limit;
  - not taking over a `/regrade` in progress;
  - row locking;
  - not reporting repeated failures;
  - reporting success;
  - batching those reports;
  - the AI-back wording.

  Removed for `/resend`:
  - only while open;
  - not to blocked users;
  - only failed messages;
  - admins only;
  - the suggestion in the publish report.

## Tests added

All in [tests/test_recovery_followups.py](../../../tests/test_recovery_followups.py) (16 tests).

| Test | Behaviour |
|---|---|
| `test_the_publish_report_and_status_suggest_resend` | Report and `/status` suggest `/resend 1` |
| `test_resend_sends_again_only_to_those_who_failed` | One more copy to the failed user, none to others; second `/resend` finds nothing |
| `test_resend_twice_at_once_sends_one_copy` | Two processes at once: one copy each |
| `test_resend_that_fails_again_says_so` | Reply counts a repeated failure |
| `test_resend_refuses_what_it_cannot_send` | Draft, no id (real ids or "nothing to resend"), deadline passed, closed |
| `test_resend_skips_users_who_blocked_the_bot` | Blocked rows are not queued |
| `test_resend_is_for_admins_only` | Students refused, no admin command shown |
| `test_failed_grading_is_retried_once_the_ai_answers_again` | No retry (no AI call) while down; graded and sent after a reply; one admin notice |
| `test_not_retried_right_after_the_failure` | Waits the delay |
| `test_retries_are_limited_and_only_the_last_failure_is_reported` | 3 retries; only the last failure reported, with `/regrade`; student told once |
| `test_two_processes_retry_each_submission_once` | Two background passes: one result per student |
| `test_a_retry_does_not_take_over_a_manual_regrade` | A submission under `/regrade` is not claimed |
| `test_automatic_regrading_can_be_switched_off` | `AUTO_REGRADE_ATTEMPTS=0` |
| `test_the_back_message_says_the_bot_retries` | AI-back wording, `/regrade` still offered |
| `test_successful_retries_are_reported_together_after_the_first` | First at once, later ones after 15 minutes |
| `test_concurrent_automatic_retries_claim_each_submission_once` | 6 threads, 30 submissions: each claimed once |

## Results

```
python -m pytest -q
324 passed, 2 skipped in 148.93s (0:02:28)
```

Run on PostgreSQL 16 in the development container with the default `TEST_DATABASE_URL`. Before this
phase: 308 passed, 2 skipped. The 2 skipped tests need tesseract and easyocr, which are not
installed.

## Outstanding failures

None.

**Not verified:** behaviour on real Telegram clients and on Railway, and a real AI outage and
recovery.
