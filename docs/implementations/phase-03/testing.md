# Phase 03 — Work that survives a restart: Testing and validation

| | |
|---|---|
| Roadmap stage | Stage 3 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | [#12](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/12) |
| Status | Merged |

## Methodology

- **Restart simulation.** Two harnesses (`make_harness`) share one PostgreSQL schema and stand for
  the old and the new process. The old one is "killed" mid-work by `Crash`, a `BaseException`
  raised from the fake Telegram API (`FakeTelegram.fail`) or the fake AI provider, which no handler
  catches. The new one runs `work.run_once`, as the bot does on start. Leases are expired with SQL
  to stand for time passing.
- **Telegram failures.** `FakeTelegram.fail` returns Bot API errors (403 blocked, 400, 500) or
  raises `TimedOut`.
- **Concurrency.** Double taps run with `asyncio.gather` in one process and across two processes;
  database claims run from 6 to 8 threads at once.
- **Upgrade.** A test drops the new table and columns to recreate the Stage 2 schema, inserts rows
  as the old version would, and starts the new version on it.
- **Mutation checks.** Each safeguard was removed in turn (status guard on publish, delivery and
  grading leases, marking deliveries sent, retry limit, unsent-result recovery, single `save_grade`,
  legacy filter, and each review fix). For every one, at least one test failed. Two notes: removing
  the publish status guard is caught by the database tests but not the end-to-end ones, because the
  `deliveries` primary key alone still prevents a double send; removing the delivery lease made a
  test loop forever instead of failing.
- **Real start-up.** A script ran the real lifecycle (`initialize`, `post_init`, `start`, `stop`,
  `post_stop`, `shutdown`) on a database holding a half-sent assessment and an abandoned
  submission. With no admin action, the 3 unsent messages were sent, the submission was graded and
  its result sent, the admin got one report, and the loop stopped cleanly.

## Tests added (`tests/test_recovery.py`, 35 tests)

| Area | Tests |
|---|---|
| Double tap | `test_two_simultaneous_publish_taps_send_once`, `test_publish_taps_in_two_processes_send_once`, `test_concurrent_publishes_have_one_winner`, `test_publishing_queues_every_active_user_once` |
| Other races | `test_discard_after_publish_…`, `test_regenerate_that_finishes_after_publish_…`, `test_closing_during_sending_stops_the_rest`, `test_a_message_being_sent_when_the_assessment_closes_counts_as_sent` |
| Sending after a restart | `test_a_restart_mid_send_is_finished_by_the_next_process`, `test_two_processes_sending_the_same_assessment_split_the_work`, `test_concurrent_claims_never_hand_out_the_same_delivery`, `test_the_report_is_claimed_once_…`, `test_users_who_join_after_publishing_…` |
| Send failures | `test_a_user_who_blocked_the_bot_…`, `test_a_failed_send_is_retried_later`, `test_a_send_that_keeps_failing_gives_up_…`, `test_a_timed_out_send_is_not_repeated`, `test_a_partly_sent_long_assessment_is_not_repeated`, `test_publishing_to_nobody_still_reports` |
| Grading after a restart | `test_grading_interrupted_by_a_restart_is_finished_automatically`, `test_grading_that_keeps_being_interrupted_…`, `test_recovered_grading_that_fails_…`, `test_a_result_lost_by_a_restart_…`, `test_regrade_leaves_…`, `test_regrade_claims_each_submission_…`, `test_a_normal_submission_is_graded_once_…`, `test_stale_grading_is_claimed_once_with_a_limit`, `test_a_grade_is_saved_once` |
| Deploy overlap / upgrade | `test_upgrading_a_database_from_before_stage_3`, `test_an_assessment_closed_by_the_old_version_…`, `test_a_result_sent_by_the_old_version_…`, `test_schema_can_be_applied_by_two_processes_at_once`, `test_a_closed_assessment_is_not_claimed` |
| Background loop | `test_background_work_survives_an_error_and_stops_cleanly`, `test_shutdown_stops_the_background_work_even_without_stop` |

`tests/conftest.py` gained the `FakeTelegram.fail` hook and `Crash`. No existing test was changed.

## Results

```
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bot_test python -m pytest -q
200 passed, 2 skipped in 72.99s
```

Before this phase, the same command gave 165 passed, 2 skipped. The 2 skipped tests were already
skipped before this phase. Python 3.13 and PostgreSQL 16 locally; CI uses Python 3.12 (result
recorded on the PR).

## Outstanding failures

None.
