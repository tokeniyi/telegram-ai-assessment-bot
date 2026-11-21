# Phase 04 — Safer deploys: Implementation details

| | |
|---|---|
| Roadmap stage | Stage 4 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | see [summary.md](summary.md) |
| Status | In review |

## Features implemented

### Own run loop (`bot/runtime.py`)
`main.py` no longer calls python-telegram-bot's `run_polling`. `BotRunner` runs the same steps in
an order that suits a deploy:

1. `initialize` (retried 5 times on network errors), `post_init` (commands, Stage 3 background work).
2. The HTTP server starts on `PORT`; `/health` answers 503 `starting`.
3. `Application.start`, then polling starts (which also removes an old webhook) or the webhook is
   registered with `setWebhook(url + "/telegram", secret_token=…)`.
4. `/health` answers 200 `ok`.

On SIGTERM or SIGINT (`request_stop`):

1. `/health` answers 503 `stopping`; the webhook answers 503 to new deliveries, so Telegram
   retries them, by then against the new version.
2. Polling stops. python-telegram-bot then calls `getUpdates` once more with the latest offset,
   which tells Telegram the fetched updates were received, so they are not sent to the next version.
3. `Services.stopping` is set: sending and grading take no new item.
4. `Application.stop` waits for the updates already received to be handled.
5. The background loop finishes the item in hand (`work.stop`).
6. The server closes, the application shuts down, the database pool closes.

Steps 4 and 5 share one budget, `SHUTDOWN_GRACE_SECONDS` (default 50). Past it, handlers still
running are cancelled before the database and HTTP client close, and the process exits; work cut
off is recovered by the next version through the Stage 3 leases. A second signal is ignored
(Railway's SIGKILL is the hard stop). A stop requested during start-up ends start-up early, before
anything is fetched; `main.py` installs a SIGTERM handler first thing, because in a container
Python is PID 1 and would otherwise ignore SIGTERM until the run loop's handler exists.

### Health check and webhook server (`bot/web.py`)
A minimal HTTP/1.1 server on `asyncio.start_server`, no new dependency:

- `GET`/`HEAD /health`: 200 `ok` when updates are being fetched and `Database.ping` succeeds; 503
  with `starting`, `stopping` or `database unreachable` otherwise. The ping runs on its own single
  thread (one at a time, however often `/health` is called), and connection wait, query
  (`statement_timeout`) and the health check's own wait are each limited to 3 s.
- `POST /telegram` (webhook mode only): checks `X-Telegram-Bot-Api-Secret-Token` with a
  constant-time comparison (403 if wrong), rejects bodies that are not an update (400), puts the
  update on the application's queue and answers 200 at once. Once shutdown has begun it answers
  503, so Telegram resends the update (to the new version). An update python-telegram-bot cannot
  read is logged and answered 200, so Telegram does not resend it forever.
- Anything else: 404. A request must arrive within 10 s, with at most 100 header lines and a
  body of at most 1 MB.

### Background work stops between items (`bot/handlers/work.py`)
Stage 3's loop was cancelled on stop, possibly between Telegram accepting a message and the row
being marked sent. Now `deliver`, `recover_grading` and the loop check `Services.stopping` before
taking the next item, the 30 s sleep wakes on it, and `work.stop` waits for the task (up to the
remaining grace) before cancelling it.

### Settings (`bot/config.py`, `.env.example`)
`PORT`, `WEBHOOK_URL` (must be `https://` with no path, needs `PORT`), `WEBHOOK_SECRET` (derived from the
token with SHA-256 when empty; checked against Telegram's allowed characters),
`SHUTDOWN_GRACE_SECONDS` (50), `TELEGRAM_API_URL` (a local Bot API server; also how the tests
point the bot at a fake API).

## Architecture and design decisions

- **Own lifecycle instead of `run_polling`/`run_webhook`.** `run_webhook` serves only the webhook
  (tornado, an extra dependency) and cannot add `/health`; `run_polling` has no hook between
  "polling started" and "ready", and waits without limit for handlers on stop.
- **Healthy only once updates are fetched.** Railway switches as soon as `/health` answers 2xx; an
  earlier "ok" would let Railway stop the old version before the new one takes over.
- **Health checks the database.** A version that cannot reach PostgreSQL would lose every
  update; failing the check keeps the old version running instead.
- **Webhook kept on shutdown.** Deleting it would make Telegram stop delivering to the new
  version, which registered the same URL.
- **Webhook optional, polling the default.** A webhook needs a public domain on the service; the
  bot keeps working without one.
- **No `railway.json`.** Config as code stops being read on 2026-12-01 (see
  [overview.md](overview.md)); the three Railway settings are documented in the README.

## Significant code changes

| File | Change |
|---|---|
| `bot/runtime.py` (new) | `BotRunner`: start-up, polling or webhook, signal handling, ordered shutdown. |
| `bot/web.py` (new) | `/health` and the webhook receiver. |
| `main.py` | Runs `bot.runtime.run` instead of `run_polling`. |
| `bot/app.py` | Uses `TELEGRAM_API_URL` as the Bot API base URL when set. |
| `bot/config.py` | The new settings and their checks. |
| `bot/db.py` | `Database.ping`. |
| `bot/utils.py` | `Services.stopping`. |
| `bot/handlers/work.py` | Loops check `stopping`; `stop` waits before cancelling. |
| `tests/fake_bot_api.py` (new) | A fake Bot API over real HTTP (polling, webhook registration, messages). |
| `tests/test_deploy.py` (new) | Stage 4 tests (see [testing.md](testing.md)). |
| `README.md`, `.env.example` | Railway settings and the new variables. |
| `Dockerfile` | `python:3.12-slim-bookworm` (was 3.11), the version CI tests on. |

## Schema and configuration changes

No schema change. New optional variables only; with none of them set, the bot polls as before
and serves no HTTP. On Railway, `PORT` is set by the platform, so `/health` is served there.
