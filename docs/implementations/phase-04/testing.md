# Phase 04 — Safer deploys: Testing and validation

| | |
|---|---|
| Roadmap stage | Stage 4 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | [#13](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/13) |
| Status | In review |

## Methodology

- **Production code path.** The tests run `bot.runtime.BotRunner`, the code `main.py` runs, with
  python-telegram-bot's real HTTP client, against a fake Bot API served over real HTTP
  (`tests/fake_bot_api.py`). Like Telegram, the fake answers a `getUpdates` cut off by another one
  with 409 Conflict, so two versions polling at once behave as they do on Telegram.
- **Real process and signals.** Two tests start `python main.py` as a subprocess and send SIGTERM:
  once after `/health` answers 200, once during start-up.
- **Shutdown under load.** Slow AI calls and a fake API that holds `sendMessage` back keep work in
  progress while the stop arrives, to check what finishes, what is refused and what is left for the
  next process (via a fresh database connection, since the stopped bot has closed its own).
- **Handover.** An old and a new runner on one database and one API: the new one starts while the
  old one is grading, then the old one is stopped.
- **Mutation checks.** Each safeguard was removed in turn, and every one made at least one test
  fail: poller stopped first, background loop finishing its item, `deliver` honouring the stop,
  webhook refusing during shutdown, secret check, grace cap, "stopping" health, webhook refusal
  after a stop, ping timeout, HEAD without body, malformed update answered 200, start-up returning
  early, `WEBHOOK_URL` path check, cancelling handlers after the grace period, early SIGTERM
  handler. Removing the grace cap is detected as a test run that exceeds its time limit rather
  than as an assertion. Three first attempts were too weak to tell (described in
  [issues-and-fixes.md](issues-and-fixes.md)) and were redone or the test strengthened.

## Tests added (`tests/test_deploy.py`, 19 tests)

| Area | Tests |
|---|---|
| Health check | `test_health_is_only_ok_while_fetching_updates_and_the_database_answers`, `test_a_running_bot_reports_healthy_and_answers_users`, `test_health_fails_when_the_database_is_down`, `test_a_hanging_database_makes_health_fail_fast`, `test_head_has_no_body_and_too_many_headers_are_refused` |
| Clean shutdown | `test_stop_confirms_fetched_updates_so_the_next_process_is_not_sent_them`, `test_grading_in_progress_finishes_before_the_process_exits`, `test_shutdown_does_not_wait_past_the_grace_period`, `test_sending_stops_after_the_message_in_hand_and_the_rest_is_left_queued`, `test_background_sending_finishes_the_message_in_hand`, `test_a_stop_during_start_up_does_not_start_fetching` |
| Handover | `test_a_deploy_handover_loses_nothing_and_handles_nothing_twice` |
| Webhook | `test_webhook_mode_receives_updates_and_refuses_strangers`, `test_a_webhook_update_arriving_across_sigterm_is_refused_not_lost`, `test_a_malformed_webhook_update_is_dropped_not_retried_forever`, `test_webhook_settings_are_checked`, `test_a_webhook_url_with_a_path_is_refused` |
| Real process | `test_main_py_reports_healthy_and_exits_cleanly_on_sigterm`, `test_main_py_exits_on_sigterm_even_while_starting` |

`tests/test_recovery.py`: two assertions changed from "the background task was cancelled" to "it
finished", because `work.stop` now lets it finish.

## Results

```
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bot_test python -m pytest -q
219 passed, 2 skipped in 90.38s
```

Before this phase: 200 passed, 2 skipped. Run locally on Python 3.13 with PostgreSQL 16; CI runs
Python 3.12 (result recorded on the PR).

## Not tested

- A real Railway deploy: health check, draining and overlap settings, and the webhook through
  Railway's proxy. **Not done**; needs the Railway settings in the README and a manual redeploy.
- The Docker image was not built here (no Docker in the development environment); the Python
  3.12 base image is the one CI's dependencies are installed on.
- PID 1 behaviour itself (only reproducible inside a container); the early handler is tested
  on a normal process.

## Outstanding failures

None.
