# Phase 04 — Safer deploys: Challenges, debugging and fixes

| | |
|---|---|
| Roadmap stage | Stage 4 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | [#13](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/13) |
| Status | Merged |

Items 1 to 7 came up during development; items 8 to 13 came from a review of the first complete
version by a separate review agent, which compared `BotRunner` with python-telegram-bot's own run
loop and confirmed findings 8, 9 and 12 by running code.

### 1. Railway's documentation could not be read directly
- **Symptom:** every request to `docs.railway.com` failed (`ENOTFOUND`): the host is not allowed
  by the development environment's network policy.
- **Fix:** the facts were taken from web-search results quoting the documentation (see
  [overview.md](overview.md)). One of them changed the plan: config as code is deprecated and stops
  being read on 2026-12-01, so no `railway.json` was added. Whether the healthcheck uses `PORT` is
  marked **Unable to verify**.

### 2. `run_polling` does not fit a deploy
- **Finding (from reading PTB 22.8):** `run_polling` stops the poller, then waits for handlers
  without a limit, and has no point between "polling started" and "ready" for a health check;
  `run_webhook` cannot serve `/health`. Hence `bot/runtime.py`.

### 3. Background work was cut off on stop
- **Root cause:** Stage 3's `work.stop` cancelled the loop, possibly between Telegram accepting a
  message and the row being marked `sent`, the duplicate window Phase 03 left open.
- **Fix:** the loops check `Services.stopping` between items; `work.stop` waits before cancelling.

### 4. A mutation slipped through: cancelling the loop at once passed every test
- **Symptom:** reverting item 3 did not fail any test. The sending test went through the Send
  button, whose handler `Application.stop` waits for, not through the background loop.
- **Fix:** `test_background_sending_finishes_the_message_in_hand` publishes before the bot starts,
  so the background loop is the sender. It fails with the mutation.

### 5. Failing deploy tests hung instead of failing
- **Symptom:** with some mutations, the test run never finished.
- **Root cause:** a failed assertion left a slow AI call blocked; shutdown then waited the full
  50 s grace while the fixture's teardown waited for the bot.
- **Fix:** the test fixture uses a 5 s grace and fails after 15 s. (Test-only.)

### 6. Test bugs found while writing the tests
- The fake API recorded `getUpdates` only when the long poll returned; it now records it on arrival.
- Tests read the database after the bot had closed its pool; they now open a fresh connection.
- A test expected no `getUpdates` after the poller stopped, but python-telegram-bot makes one more
  call to confirm the fetched updates (the behaviour wanted); the test now expects exactly that call.
- A test called `work.deliver` from outside the bot, so nothing waited for it and its send failed
  when the HTTP client closed. No such caller exists in the bot; the test now uses the Send button.

### 7. Environment: PostgreSQL had stopped
- The idle development container's PostgreSQL stopped between sessions (every test errored on
  connect). Restarted with `pg_ctlcluster 16 main start`. Not a code issue.

### 8. A webhook update could get 200 and then be lost (review, confirmed)
- **Symptom:** headers received, then SIGTERM, then the body: the request was answered 200 while
  the update queue was already closing, so the update was neither handled nor resent by Telegram.
- **Fix:** `BotRunner._put_update` refuses once a stop is requested and the server answers 503,
  so Telegram resends it. Test: `test_a_webhook_update_arriving_across_sigterm_is_refused_not_lost`.

### 9. A hanging database made `/health` answer 400 after 10 s (review, confirmed)
- **Root cause:** the request timeout was reported as a bad request, and the ping ran on the
  default thread pool shared with OCR, where a hung query keeps a thread.
- **Fix:** the ping runs on its own single thread (one at a time), with a 3 s wait and a
  `statement_timeout`; a slow ping is reported as 503 `database unreachable`. Test:
  `test_a_hanging_database_makes_health_fail_fast`.

### 10. SIGTERM during start-up (review)
- **Root cause:** `start()` carried on after a stop request (python-telegram-bot checks for it after
  `post_init`); in webhook mode the server could then accept updates during shutdown. Separately,
  in a container Python is PID 1, which ignores SIGTERM until a handler is installed.
- **Fix:** `start()` returns early once a stop is requested; `main.py` installs a SIGTERM handler
  first. Tests: `test_a_stop_during_start_up_does_not_start_fetching`,
  `test_main_py_exits_on_sigterm_even_while_starting`.

### 11. The grace period did not stop handlers still running (review)
- **Fix:** after the grace period, handler tasks still running are cancelled before the HTTP
  client and the database pool close. Test: `test_shutdown_does_not_wait_past_the_grace_period`
  now also checks that no database-pool errors are logged.
- **Not fixed:** an OCR thread already running is not interrupted; Python joins it at exit, so
  exit can take longer than `SHUTDOWN_GRACE_SECONDS`. Railway's SIGKILL bounds it.

### 12. HTTP details (review; HEAD confirmed)
- HEAD answered with a body: fixed. More than 100 header lines: now 400.
- An update python-telegram-bot cannot read answered 500, so Telegram would resend it forever:
  now 200 with `{"ok": false}` and an error in the log.
- `WEBHOOK_URL` with a path registered `/app/telegram` while the bot serves `/telegram`: such a URL
  is now refused at start-up.
- Not changed: no limit on concurrent connections (the server only answers Railway's proxy and
  health checks; documented).
Tests: `test_head_has_no_body_and_too_many_headers_are_refused`,
`test_a_malformed_webhook_update_is_dropped_not_retried_forever`,
`test_a_webhook_url_with_a_path_is_refused`.

### 13. Tests less realistic than they claimed (review)
- The fake API served two concurrent pollers, whereas Telegram answers 409 Conflict. The fake now
  does the same, and the handover test asserts that conflicts happened.
- The Dockerfile used Python 3.11 while CI tests 3.12 (and `asyncio` shutdown behaviour differs
  between them). The Dockerfile now uses `python:3.12-slim-bookworm`.
