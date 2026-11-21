# Phase 03 — Work that survives a restart: Implementation details

| | |
|---|---|
| Roadmap stage | Stage 3 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | see [summary.md](summary.md) |
| Status | In review |

## Features implemented

### Publishing once, with a send queue
`Database.publish_assessment` runs in one transaction:
`UPDATE assessments SET status = 'published' … WHERE id = %s AND status = 'draft' RETURNING id`,
then one `deliveries` row per active user (`ON CONFLICT DO NOTHING`). A second tap, or a copy in a
second bot process, gets `None` and is told the draft was already published. The publish handler
(`on_draft_button` in `bot/handlers/admin.py`) then sends from the queue.

### Sending that survives a restart
`work.deliver` (`bot/handlers/work.py`) repeatedly calls `Database.claim_delivery`, which takes the
next pending row with `FOR UPDATE SKIP LOCKED` and leases it for 5 minutes (`claimed_until`). After
sending, the row is marked `sent`; a user who blocked the bot is marked `blocked` (and inactive);
other Telegram errors are retried after 60 s, up to 3 attempts, then `failed`. A timeout, or a
failure after the first of several messages, is not retried (Telegram may already have delivered
it) and the row is marked `failed`. A row whose lease ran out (its process stopped) is claimed again.

When no row of an assessment is pending, `Database.claim_delivery_reports` sets
`delivery_reported_at` and returns the counts to exactly one caller, which sends the publisher the
"sent to N users" report. If sending finishes in a later process, that process sends the report.
Pending rows of an assessment that is no longer published (closed by the previous version during
a deploy) are marked `cancelled` there, so the report is never blocked.

### Grading that survives a restart
`submit_draft` now inserts the submission already leased to the submitter (`grading_attempts = 1`,
`grading_claimed_until = now() + lease`). The lease is `max(600 s, 4 × AI_TIMEOUT_SECONDS)`, longer
than one grading call including SDK retries, so live grading is not duplicated.
`Database.claim_stale_grading` takes a `pending` submission whose lease ran out, renews the lease
and increments `grading_attempts`; after 3 attempts it marks it `failed` instead and the admins are
told to `/regrade`. `save_grade` only updates a submission that is not graded yet, so if two
processes ever grade the same one, only the first saves and sends a result.

`save_grade` sets `result_due`; a successful send clears it and sets `result_sent_at`. A result still
due 5 minutes after grading (process stopped, or Telegram failed) is sent once more by
`claim_unsent_result`.

### Background loop
`work.run_forever` runs `deliver`, `report_deliveries` and `recover_grading` on start (from
`_post_init` in `bot/app.py`) and every 30 s. Each step is guarded, so one failure is logged and
retried on the next round. `_post_stop` cancels the loop, and `_post_shutdown` does too, for a
start-up that failed after `post_init`.

### Other check-then-act races closed
- Discard: `discard_draft_assessment` deletes only `WHERE status = 'draft'`.
- Regenerate: `replace_questions` swaps questions only while the assessment is a draft; if it was
  published during generation, the new questions are dropped and the admin is told.
- `/close`: `close_assessment` closes only a published assessment and cancels its pending
  deliveries, so students are not sent an assessment they can no longer answer.
- `/regrade`: claims each submission with `claim_for_regrade` just before grading it, skipping one
  that another process is grading right now.

## Architecture and design decisions

- **Outbox in PostgreSQL, not a job queue library.** The bot already depends on PostgreSQL and
  every process shares it; `FOR UPDATE SKIP LOCKED` plus leases gives one owner per message without
  a new service. python-telegram-bot's `JobQueue` would need the APScheduler extra and is
  process-local.
- **Leases, not "on start, redo everything".** During a Railway deploy the old version may still
  be sending or grading when the new one starts; a lease tells abandoned work from work in progress.
- **One row claimed at a time.** A batch claim could expire while a long Telegram `RetryAfter` wait
  is in progress; one row per claim keeps each lease short and honest.
- **Results: at least once, assessments: at most once in the normal path.** Losing a result is
  worse than a rare duplicate, so results are marked after sending and resent once if unmarked.
- **Old rows are left alone.** Only grades saved by this version have `result_due`, so results the
  old version sent (before the upgrade, or during the deploy overlap) are never resent. Published assessments without `deliveries` rows are never reported. A legacy
  `pending` submission without a lease counts as abandoned only once `created_at` is older than
  one lease, so one the old version is still grading during the deploy is not duplicated.
- **Schema applied under an advisory lock** (`pg_advisory_xact_lock`), because two versions start
  together during a deploy and concurrent `ALTER TABLE` / `CREATE TABLE IF NOT EXISTS` can fail.

## Significant code changes

| File | Change |
|---|---|
| `bot/db.py` | `deliveries` table; new columns; advisory lock; `publish_assessment`, `close_assessment`, `discard_draft_assessment`, `claim_delivery`, `finish_delivery`, `retry_delivery_later`, `delivery_counts`, `claim_delivery_reports`, `claim_stale_grading`, `claim_for_regrade`, `mark_report_sent`, `mark_result_sent`, `claim_unsent_result`; `submit_draft` leases the submission; `save_grade` returns whether it saved; `replace_questions` only for drafts. |
| `bot/handlers/work.py` (new) | Sending, reporting, grading, result sending and the background loop. |
| `bot/handlers/admin.py` | Publish/Discard/Regenerate use the guarded methods; `_broadcast` removed; `/close` and `/regrade` use claims. |
| `bot/handlers/student.py` | Submit passes the grading lease; grading moved to `work.grade`; `stored_result_blocks` takes the services object. |
| `bot/app.py` | Starts the background loop in `_post_init`, stops it in `_post_stop`. |
| `tests/conftest.py` | `FakeTelegram.fail` hook for Bot API errors, and `Crash` to stand for the process dying. |
| `tests/test_recovery.py` (new) | Stage 3 tests (see [testing.md](testing.md)). |

## Schema and configuration changes

Added on start, in place, to existing databases:

- Table `deliveries (assessment_id, user_id, status, attempts, claimed_until, last_error, sent_at)`,
  primary key `(assessment_id, user_id)`.
- `assessments.published_by`, `assessments.delivery_reported_at`.
- `submissions.grading_attempts` (default 0), `submissions.grading_claimed_until`,
  `submissions.result_sent_at`, `submissions.result_due` (default false).

All additions are nullable or defaulted, so the previous version keeps working against the
upgraded schema while both run during a deploy. No new settings; the tuning constants are at the
top of `bot/handlers/work.py`.
