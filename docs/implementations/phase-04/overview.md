# Phase 04 — Safer deploys: Phase overview

| | |
|---|---|
| Roadmap stage | Stage 4 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | see [summary.md](summary.md) |
| Status | In review |

## Objective

During a Railway deploy the new version starts while the old one still runs. Until now the old
one was killed without warning, and both polled Telegram at the same time. This phase makes the
handover orderly: the new version reports when it is ready, the old one stops taking updates and
finishes its work, and, optionally, a webhook removes the double fetching altogether.

## Scope

In scope (the three items of Stage 4 in the roadmap):

- **Health check:** `GET /health` on `PORT`, 200 only while updates are being fetched and the
  database answers.
- **Clean shutdown:** on SIGTERM, stop fetching, finish the updates and the background item in
  hand, within `SHUTDOWN_GRACE_SECONDS`, then exit.
- **Optional webhook:** `WEBHOOK_URL` switches from polling to a webhook with a secret token.

Also: the documentation status updates for Phase 03 (merged as PR #12), rolled into this branch
at the user's request.

Out of scope: Railway service settings themselves (they are made in the Railway dashboard and are
documented, not changed, by this repository); admin visibility (Stage 5); rate limits (Stage 7).

## Expected outcome

From the roadmap: *a redeploy during normal use produces no `already handled` log lines and no
interrupted work.* See [summary.md](summary.md) for what this phase can and cannot guarantee
without the Railway settings.

## Builds on

- Phase 02: copies of an update are dropped in any process. With polling, the overlap of two
  versions still produces such copies; this phase shortens that overlap, and the webhook removes it.
- Phase 03 outstanding issues: "a process that stops between Telegram accepting a message and the
  row being marked `sent` leaves a row that is sent again … Stage 4's clean shutdown narrows this".
  The background work now finishes the item in hand on shutdown instead of being cancelled.
- Phase 03 decision: leases make interrupted work recoverable, so the shutdown may give up after a
  grace period without losing anything.

## Facts about Railway this phase relies on

Read from Railway's documentation via web search on 2026-11-21 (docs.railway.com itself was not
reachable from the development environment, so the pages were not read in full):

- A healthcheck is called while a deployment starts; Railway waits for any 2xx answer (300 s by
  default) before making it active, and does not call it afterwards.
- `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` is the time between SIGTERM and SIGKILL for the old
  deployment; **the default is 0**.
- `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS` is how long the old deployment keeps running after the new
  one is active.
- Config as code (`railway.json`/`railway.toml`) is deprecated and stops being read on
  2026-12-01, so no such file is added; the settings are documented for the dashboard instead.
- That the healthcheck calls the app on the `PORT` Railway injects: **Unable to verify** from the
  pages read; it matches the bot listening on `PORT`.
