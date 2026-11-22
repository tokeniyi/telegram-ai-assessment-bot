# Phase 03 — Work that survives a restart: Phase overview

| | |
|---|---|
| Roadmap stage | Stage 3 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | [#12](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/12) |
| Status | Merged |

## Objective

Slow work ran inside the Telegram handler that started it, so a restart or redeploy in the middle
lost it and nothing picked it up again. This phase moves sending and grading onto rows in
PostgreSQL that any bot process can finish, and makes publishing happen exactly once.

## Scope

In scope (the three problems listed for Stage 3 in the roadmap):

| Problem | Fix in this phase |
|---|---|
| A restart while an assessment is being sent leaves the remaining students without it. | Publishing writes one `deliveries` row per student in the same transaction as the status change. Rows are claimed with a lease, sent, and marked. Leftovers are sent on start and every 30 s. |
| A restart mid-grading leaves the submission `pending` until an admin runs `/regrade`. | Submissions are leased to the process grading them. An abandoned one is graded again automatically (at most 3 runs), then its result is sent. |
| Two quick taps on "Send to all users" send the assessment twice. | `publish_assessment` changes the status with `UPDATE … WHERE status = 'draft' RETURNING`; only one tap gets through. |

Also in scope, because they are the same check-then-act race: Discard and Regenerate racing with
Send, and `/close` racing with sending.

Out of scope: health checks and clean shutdown (Stage 4), admin status/alerts (Stage 5), sending
speed for large classes (Stage 8).

## Expected outcome

From the roadmap: *a restart during sending or grading needs no admin action, and no student
receives an assessment twice.* The one remaining window for a duplicate is documented in
[summary.md](summary.md).

## Builds on

- Phase 01: `Database.submit_draft` creates at most one submission per submit; this phase adds a
  grading lease to the row it inserts. Phase 01 listed "grading and sending still run only in
  memory" as outstanding.
- Phase 02: the update guard drops copies of one update but not two different taps; Phase 02's
  summary lists the "Send to all users" double tap as outstanding. It is fixed here.
