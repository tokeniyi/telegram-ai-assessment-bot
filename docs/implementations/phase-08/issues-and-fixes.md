# Phase 08 — Recovery without the admin: Challenges, debugging and fixes

| | |
|---|---|
| Roadmap stage | Stage 8 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stoic-babbage-iym3c3` (restarted from `main` after PR #16 was merged) |
| Pull request | [#17](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/17) |
| Status | Merged |

No bug in the bot's code was found while building this phase. The problems below were in the new
tests or in what they could observe.

### 1. A retry test that never recorded the provider as back
- **Symptom:** `test_retries_are_limited_and_only_the_last_failure_is_reported` saw no retry at all.
- **Root cause:** the test made grading fail with `AIUnavailable`, which leaves
  `consecutive_failures` above 0. The bot then correctly waits for a reply. Queuing unusable replies
  does not help, because nothing requested them.
- **Fix:** the test records a success (`record_ai_success`) before the retries, as a student's chat
  message would.

### 2. The last failure notice is batched
- **Symptom:** the same test expected a new "⚠️ Grading failed" notice after the third automatic retry
  failed, and got none.
- **Root cause:** correct behaviour. The previous notice was less than 15 minutes old, so the new one
  waits for a later pass (Phase 05 batching).
- **Fix:** the test helper `time_passes` now also ages `admin_notices`.

### 3. Two safeguards no test caught when removed
- **Symptom:** the first mutation run caught 12 of 14.
  - Removing `FOR UPDATE SKIP LOCKED` from `claim_auto_regrade` went unnoticed.
  - So did removing the 15-minute batching of "retried automatically" notices.
- **Root cause:**
  - The two-process test runs both harnesses in one event loop, and database calls are synchronous.
    The two claims therefore never overlapped.
  - No test had two successful retries within 15 minutes of each other.
- **Fix:** two new tests.
  - `test_concurrent_automatic_retries_claim_each_submission_once` runs real threads with their own
    connections, as Phase 05's notice test does.
  - `test_successful_retries_are_reported_together_after_the_first` covers the batching.
  - Afterwards all 14 are caught. The locking mutation failed the new test in 5 of 5 runs, and the
    real code passed it in 6 of 6.

### 4. Environment
- PostgreSQL had stopped since the previous phase (new container). It was started again before the
  baseline run (308 passed, 2 skipped).
