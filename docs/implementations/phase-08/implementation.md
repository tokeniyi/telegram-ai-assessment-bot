# Phase 08 — Recovery without the admin: Implementation details

| | |
|---|---|
| Roadmap stage | Stage 8 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stoic-babbage-iym3c3` (restarted from `main` after PR #16 was merged) |
| Pull request | not opened yet |
| Status | In progress |

## Features implemented

### `/resend <id>`

- **Queueing.** `Database.requeue_failed_deliveries` moves the assessment's `failed` deliveries back
  to `pending` and resets their attempts. It is one conditional `UPDATE … RETURNING`, limited to open
  assessments (`OPEN`).
  - A second `/resend`, or one from another process, finds no failed rows. It queues nobody, so no
    copy is sent twice.
  - `blocked` rows are never queued.
- **Sending.** The handler sends the queue with `work.deliver`. Its reply first warns that users
  whose earlier send timed out may get a second copy. It then reports, for the users it queued, how
  many were sent, how many have blocked the bot, how many still failed, and how many will be retried.
- **When there is nothing to resend,** the reply says why:
  - the deadline passed (with a Results button);
  - it is still being sent ("no action needed");
  - nothing failed;
  - it is a draft, or closed (the shared argument check, `_NOT_POSSIBLE`).
- **Suggested** in the publish report ("could not be reached" now carries `/resend <id>`) and in
  `/status`. Both use `guidance.resend_suggestions`, built from
  `Database.failed_deliveries_by_assessment`: only open assessments with failed messages.

### Automatic regrading

- **Claiming.** `work.auto_regrade` runs in every `run_once`, after the existing grading recovery.
  It calls `Database.claim_auto_regrade`, which takes one `failed` submission (`FOR UPDATE SKIP
  LOCKED`) only if all of these hold:
  - the AI provider's last request got a reply (`ai_health.consecutive_failures = 0`);
  - the failure is older than `AUTO_REGRADE_DELAY` × 2^(retries so far): 5, 10, 20 minutes;
  - it has been retried fewer than `AUTO_REGRADE_ATTEMPTS` times.

  The claim sets the submission `pending` with a grading lease, exactly like `claim_for_regrade`. A
  manual `/regrade` and an automatic retry therefore exclude each other.
- **Per pass.** At most `AUTO_REGRADE_PER_PASS` (5) submissions are retried per pass, to spare
  free-tier limits. The pass stops at the first failure.
- **On success,** the student gets the result through `send_result`, and the submission is flagged
  `regraded_notice_due`.
  - `alerts.report_regraded` tells the admins: the first at once, later ones together at most every
    15 minutes (`GRADING_NOTICE_INTERVAL`).
  - The message ends "No action needed". It adds `/regrade` suggestions only if some grading still
    failed.
- **On failure,** the admins already know about this submission, so `mark_submission_failed(…,
  notify=False)` does not report it again. Only the last automatic retry reports it. That notice no
  longer says the bot retries, and offers `/regrade`. The student is not messaged again.
- **Wording.**
  - The failure notice adds "The bot retries failed grading by itself once the AI answers again"
    while retries remain.
  - The AI-back message says the bot grades them again within a few minutes.
  - `/status` shows "🔁 N of them retried automatically".
  - The student's "grading delayed" message now says the result is sent "as soon as grading is
    retried", instead of "after the administrator retries grading".

## Architecture and design decisions

- **Recovery is noticed from real requests, not probes.** Phase 05 decided not to probe the provider
  in the background. Retrying therefore waits for any request to get a reply: a student chatting, a
  new submission, `/generate` or `/regrade`. A reply that could not be used counts as an answer,
  because the provider is up.
  - **Cost:** after an outage, on a bot nobody uses, failed grading waits for the first request.
  - **Alternative rejected:** a real grading request as a probe every few minutes during an outage,
    which spends free-tier requests while the provider is down.
- **`/resend` is typed, not a button.** A send that timed out may have arrived, and resending it can
  duplicate it (Phase 03). That makes resending a decision, as for `/close`. The database still makes
  a double `/resend` harmless.
- **Exponential delay and a small limit.** An "unusable reply" failure counts as the provider being
  up. It could otherwise be retried every 30 s forever.
- **Batched success notices,** like Phase 05's failure notices. A class graded after an outage is one
  or two messages, not one per student.

## Significant code changes

| File | Change |
|---|---|
| [bot/db.py](../../../bot/db.py) | Columns `auto_regrades`, `regraded_notice_due`; `claim_auto_regrade`, `mark_regraded_notice_due`, `claim_regraded_notice`, `auto_retry_pending`, `requeue_failed_deliveries`, `delivery_statuses`, `failed_deliveries_by_assessment`; `mark_submission_failed(notify=)`; failure notices return `auto_regrades` |
| [bot/handlers/work.py](../../../bot/handlers/work.py) | `auto_regrade` in `run_once`; `grade(notify_failure=)`; `/resend` suggestion in the publish report; student "grading delayed" wording |
| [bot/handlers/admin.py](../../../bot/handlers/admin.py) | `/resend` handler and its state replies; `/status` suggests `/resend` and shows automatic retries |
| [bot/alerts.py](../../../bot/alerts.py) | `report_regraded`; failure notice and AI-back message mention automatic retries |
| [bot/commands.py](../../../bot/commands.py), [bot/guidance.py](../../../bot/guidance.py) | `resend` in the registry; `resend_suggestions` and its argument choices |
| [bot/config.py](../../../bot/config.py) | `AUTO_REGRADE_ATTEMPTS` |

## Schema / configuration changes

- `submissions.auto_regrades INTEGER NOT NULL DEFAULT 0` and `submissions.regraded_notice_due BOOLEAN
  NOT NULL DEFAULT FALSE`, added with `ADD COLUMN IF NOT EXISTS`.
- `admin_notices` gets a second kind, `auto_regraded`.
- New setting `AUTO_REGRADE_ATTEMPTS` (default 3; 0 = off).
- **Running next to the previous version (deploy overlap):** it ignores the new columns and keeps
  working.
  - It does not retry automatically.
  - Its `/regrade` takes a submission the same way, so the two cannot grade one twice.
  - A failure it records is reported as before, and can be retried automatically by the new version.
