# Phase 04 — Safer deploys: Final summary

| | |
|---|---|
| Roadmap stage | Stage 4 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | PR_LINK |
| Status | In review |

## Accomplished

- `GET /health` on `PORT`: 200 only while updates are being fetched and the database answers.
- Clean shutdown on SIGTERM: stop fetching (confirming fetched updates to Telegram), refuse new
  webhook deliveries, finish the updates in hand and the background item in hand within
  `SHUTDOWN_GRACE_SECONDS`, then exit 0.
- Optional webhook (`WEBHOOK_URL`, `WEBHOOK_SECRET`), served by the same small HTTP server.
- Dockerfile on Python 3.12, as CI.
- 19 new tests, including a real `main.py` process stopped with SIGTERM and an old/new handover;
  full suite 219 passed, 2 skipped.
- Phase 03 documentation and the roadmap marked as merged.

## Changes and decisions

- **`bot/runtime.py` owns the lifecycle** instead of `run_polling`. New start-up or shutdown steps
  go there, in order; `post_init`/`post_stop`/`post_shutdown` are still called.
- **`Services.stopping`** is the shutdown signal for slow work: loops check it before taking an item.
- **Railway settings live in the dashboard**, not in `railway.json` (deprecated, ignored from
  2026-12-01). They are listed in the README's "Deploying (Railway)" section.
- **Polling stays the default**; the webhook needs a public domain and is opt-in.

## Outstanding issues

- **Required manual step:** set the healthcheck path `/health` and
  `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=60` on the Railway service. Without draining time, Railway
  kills the old version at once (its default is 0) and the clean shutdown never runs; work in
  progress is then recovered by Stage 3 leases, as before.
- **The roadmap's "done when" is not verified on Railway.** No real redeploy was done from the
  development environment. With polling, a few seconds of overlap remain (both versions poll until
  the old one gets SIGTERM), so `already handled` log lines can still appear; the webhook removes
  them. Recommended check after merging: redeploy while a grading is running and look at the logs.
- An OCR thread already running is not interrupted at shutdown; exit can take longer than the
  grace period until Railway's SIGKILL.
- No limit on concurrent HTTP connections to `/health` (it is only reachable through Railway's
  proxy, and publicly only when the service has a domain).
- Whether Railway's healthcheck calls the app on `PORT`: **Unable to verify** from the documentation
  read (docs.railway.com was not reachable), though it is how the bot is set up.

## Final status

Implemented and tested; PR open for review.

## Pull request

PR_LINK
