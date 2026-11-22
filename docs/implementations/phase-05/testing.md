# Phase 05 — Visibility for the admin: Testing and validation

| | |
|---|---|
| Roadmap stage | Stage 5 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | [#14](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/14) |
| Status | Merged |

## Methodology

- **Through the bot.** `/status`, the alerts and the log events are driven with the fake Telegram
  API and fake AI provider (`tests/conftest.py`): admins and students send commands, tap Submit,
  and the AI fails in the two ways that matter. `AIUnavailable` stands for an outage; a plain
  `AIError` stands for a bad reply.
- **Several processes.** `/status` is asked of a second harness on the same database: it must
  report the first process's failed sends, failed and in-progress grading. Two harnesses failing
  at the same time must send one alert between them.
- **Races.** Eight connections record failures (then successes) at the same moment, and six claim
  the grading notice at the same moment. Exactly one is told to send each message, and no failure
  is reported twice.
- **Real SDKs.** `check()` and `complete()` of the Anthropic and OpenAI SDK providers run
  against a local HTTP server standing in for the provider (200, 401, 404, 503, closed port).
- **Real process.** `python main.py` with `LOG_FORMAT=json`: every stdout line must be a JSON
  object, and nothing of the bot's own logging goes to stderr.
- **Mutation checks.** 21 safeguards were removed one at a time (script kept out of the
  repository), and for each the targeted tests were run:
  - **20 caught:** the alert not repeated, the "back" message only after an alert, the threshold,
    the atomic failure count (replaced by read-then-write), the notice interval, notices held
    during an outage, regraded failures not reported, the "back" message reporting held failures,
    the background flush, both observer calls, connection errors as `AIUnavailable`, no retry in
    `check()`, monitoring errors swallowed, the `/status` timeout, `/status` admin-only,
    positional-only `event()`, the reserved-key rename, logs on stdout, the grading error stored.
  - **1 missed, then explained:** the notice claim's atomic update. It has a second guard, the
    `admin_notices` lock; removing both fails the test, removing either alone does not (see
    [issues-and-fixes.md](issues-and-fixes.md), item 7).

## Tests added (`tests/test_visibility.py`, 18 tests, 19 cases)

| Area | Tests |
|---|---|
| `/status` | `test_status_is_for_admins_and_says_when_nothing_needs_attention`, `test_status_shows_grading_and_sending_problems_from_every_process`, `test_status_reports_an_ai_provider_that_does_not_answer` |
| AI down / back | `test_admins_are_told_once_when_the_ai_is_down_and_once_when_it_is_back`, `test_a_bad_reply_is_not_an_outage`, `test_two_processes_failing_at_once_alert_once`, `test_concurrent_ai_outcomes_decide_one_alert_each`, `test_monitoring_failures_never_break_the_work` |
| Grading notices | `test_grading_failures_are_reported_at_once_then_batched`, `test_a_failure_fixed_by_regrade_before_the_summary_is_not_reported`, `test_during_an_outage_grading_failures_wait_for_the_back_message`, `test_concurrent_notice_claims_report_each_failure_once` |
| Provider check | `test_the_provider_check_lists_models_and_reports_failures[openai]`, `[anthropic]` |
| Logs | `test_sending_and_grading_write_events` (also checks no answer text is in an event), `test_failures_are_logged_as_events`, `test_json_lines_carry_the_event_fields`, `test_main_py_logs_json_lines_to_stdout`, `test_log_format_is_checked` |

The existing tests that check the admin is told about a failed or abandoned grading
(`test_flow.py`, `test_recovery.py`) pass unchanged: one failure keeps its pre-Stage 5 wording.

## Results

```
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bot_test python -m pytest -q
238 passed, 2 skipped in 98.42s
```

Before this phase: 219 passed, 2 skipped (same command, on this branch reset to `main`). Run
locally on Python 3.13 with PostgreSQL 16; CI runs Python 3.12 (result recorded on the PR).

## Not tested

- The live check against the real Kilo gateway and other hosted providers: **Not done**; no
  network access to them here. Whether Kilo's gateway serves `GET /models`: **Unable to verify**.
  If it does not, `/status` shows "reachable (it has no model list)".
- How Railway displays the JSON lines: **Not done** (no Railway access from here).

## Outstanding failures

None.
