# Phase 03 — Work that survives a restart: Challenges, debugging and fixes

| | |
|---|---|
| Roadmap stage | Stage 3 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | [#12](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/12) |
| Status | In review |

Problems are listed in the order they were found. Items 3 to 9 came from a review of the first
complete version, done by a separate review agent that read the diff and confirmed suspicions
with scripts against PostgreSQL; items 1 and 2 came up during development.

### 1. Results could be lost if the process stopped while sending them
- **Symptom:** found while writing the restart tests. The first version marked a result as sent
  *before* sending it, so a crash during the send lost the result for good.
- **Root cause:** at-most-once was applied to results, where a lost message is worse than a repeat.
- **Fix:** `send_result` marks `result_sent_at` only after a successful send; a graded result still
  unmarked after 5 minutes is sent once by `claim_unsent_result`. Test:
  `test_a_result_lost_by_a_restart_is_sent_later_once`.

### 2. Restart tests counted the admin's draft preview as a delivery
- **Symptom:** 4 new tests failed with "admin received the assessment 2 times".
- **Root cause:** a test bug. The admin's draft preview contains the same question text as the
  published message, and the counter matched both.
- **Fix:** the counter ignores messages containing "Draft #". No bot code changed.

### 3. An assessment closed by the previous version never finished (review, confirmed)
- **Symptom:** during a deploy, the old version's `/close` only sets the status. Its pending
  `deliveries` rows were never claimed (the claim requires `published`) and never finished, so the
  publisher's report never arrived.
- **Fix:** `claim_delivery_reports` first marks pending rows of non-published assessments
  `cancelled`. Test: `test_an_assessment_closed_by_the_old_version_is_still_reported`.

### 4. A long `/regrade` outlived its own leases (review, confirmed)
- **Symptom:** `/regrade` claimed every submission up front with one lease each, then graded them
  one by one. After an AI outage a batch can take longer than the lease. The background work then
  took the later ones as abandoned: it graded them twice, or marked them `failed` and told the
  student grading was unavailable just before `/regrade` sent their result.
- **Fix:** each submission is claimed just before it is graded. Test:
  `test_regrade_claims_each_submission_only_when_it_grades_it`.

### 5. A result sent by the previous version could be sent again (review, confirmed with SQL)
- **Symptom:** the old version's `/regrade` saves a grade and sends it without setting
  `result_sent_at`. The unsent-result recovery (which then keyed on `grading_attempts > 0`) would
  send it a second time.
- **Fix:** new column `result_due`, set only by this version's `save_grade` and cleared when the
  result is sent; recovery only considers `result_due` rows. Test:
  `test_a_result_sent_by_the_old_version_is_not_sent_again`.

### 6. Retrying a failed send could duplicate an assessment (review, reasoning)
- **Symptom:** every Telegram error was retried, including a timeout (Telegram may already have
  delivered the message) and a failure on the second of several messages (the first would be sent
  again).
- **Fix:** `_send_once` sends the parts itself; a `TimedOut`, or any failure after the first part,
  marks the row `failed` without retrying. Tests: `test_a_timed_out_send_is_not_repeated`,
  `test_a_partly_sent_long_assessment_is_not_repeated`.

### 7. Publishing to nobody gave the admin no report (review, reading)
- **Root cause:** the report needs at least one `deliveries` row. In practice the admin who taps
  Send is an active user, so this needs an empty queue to happen; it is guarded anyway.
- **Fix:** an empty queue sends the "sent to 0 users" report at once. Test:
  `test_publishing_to_nobody_still_reports`.

### 8. The background loop could outlive the database (review, checked against PTB 22.8)
- **Root cause:** python-telegram-bot runs `post_stop` only if the application started; if start-up
  fails after `post_init`, the loop kept running while `post_shutdown` closed the pool.
- **Fix:** `_post_shutdown` also stops the loop. Test:
  `test_shutdown_stops_the_background_work_even_without_stop`.

### 9. Minor findings (review)
- A message being sent when `/close` lands was counted `cancelled` although it was sent; `finish_delivery`
  now also updates `cancelled` rows. Test: `test_a_message_being_sent_when_the_assessment_closes_counts_as_sent`.
- `retry_delivery_later` logged "will retry" for a row that /close had cancelled; it now says so.
- `/regrade` said "the users have their results" even when sending failed; it now counts
  delivered results.
- Not changed: under READ COMMITTED, `claim_delivery` can return nothing for a moment while another
  process holds rows, so the publish handler may stop early; the background loop sends the rest
  within 30 s.
