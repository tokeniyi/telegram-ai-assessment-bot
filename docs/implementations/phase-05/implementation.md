# Phase 05 — Visibility for the admin: Implementation details

| | |
|---|---|
| Roadmap stage | Stage 5 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | not opened yet |
| Status | In progress |

## Features implemented

### `/status` (`bot/handlers/admin.py`)
Admins only. It answers "🩺 Checking…" and then edits that message into a report with three
sections. Everything except the live check comes from `Database.status_overview`, so the report
covers every bot process:

- **AI provider:** the configured provider and model; **Now:** the result of `provider.check()`,
  bounded to 12 s; **Recent requests:** how many failed in a row since when and with what error, the
  last success, and whether the admins were alerted (from `ai_health`).
- **Grading:** being graded now (pending, lease live), interrupted (pending, lease expired, so the
  background loop grades it again), failed (per assessment, as `/regrade` commands) with the
  latest error, and results saved but not delivered yet.
- **Sending assessments:** messages still queued and messages that could not be sent, per
  assessment, with the latest error; users who have blocked the bot.

A footer gives the answering process's uptime and whether it polls or uses the webhook.

### Live AI check (`bot/ai/providers.py`)
`check()` on both providers lists the provider's models (`GET /v1/models`), with a 10 s timeout and
no SDK retries. This shows whether the provider is reachable and accepts the key, without
spending tokens. A 404 is reported as "reachable (it has no model list)", because some
OpenAI-compatible servers have no model list; any other error status is reported as
`AIUnavailable`. A provider without `check` is shown as "not checked".

### `AIUnavailable` and the AI observer (`bot/ai`)
`AIUnavailable(AIError)` is raised when a request could not be made: connection failure or
timeout, error status, rate limit, or the key or model refused. A reply that arrived but was
unusable (malformed JSON, a refusal, cut off) stays a plain `AIError`. Callers that catch
`AIError` are unchanged.

`AssessmentAI._complete` wraps every text request (generate, explain, chat, grade). It logs it,
then reports to an optional observer: `ai_succeeded()` when a reply arrived, `ai_unavailable()` on
`AIUnavailable`. `bot/app.py` installs `alerts.AIMonitor` as the observer.

### Alerts (`bot/alerts.py`)
- **AI down:** `Database.record_ai_failure` counts consecutive failures on the single `ai_health`
  row. The failure that makes `AI_DOWN_AFTER` (3) in a row, if the admins have not been told yet,
  sets `down_notified_at`. Only that caller gets the row back and sends 🚨.
- **AI back:** `Database.record_ai_success` locks the row, resets it, and returns the previous
  state only if `down_notified_at` was set. That caller sends ✅, listing the submissions waiting
  for `/regrade`.
- **Grading failed:** `mark_submission_failed` (and the "interrupted 3 times" path) now store
  `grading_error` and `failed_at`, and set `failure_notice_due`.
  `Database.claim_grading_failure_notice` hands each due failure to one caller, and returns nothing
  if the last notice is under 15 minutes old or the admins know the AI is down.
  `report_grading_failures` is called after every grading failure (in the Submit handler and the
  recovery loop) and on every background run, which sends what was held back. One failure keeps
  the wording used before Stage 5; several are summarised per assessment.
- `notify_admins` moved here from `bot/handlers/work.py`. Monitoring errors are logged and
  swallowed, so a database problem in monitoring cannot fail an AI call or a grading run.

### Structured logs (`bot/logs.py`)
`event(log, name, level, **fields)` writes `name key=value …` and attaches the fields to the log
record. `JsonFormatter` (`LOG_FORMAT=json`) writes one object per line: `time`, `level`
(`debug|info|warn|error`), `logger`, `message`, `event`, the fields, and `exception`. A field
that clashes with a standard key is renamed `field_<name>`. `setup_logging` writes to stdout and
keeps `httpx` at WARNING, because its URLs contain the bot token.

| Area | Events |
|---|---|
| Sending | `delivery.sent` (DEBUG), `delivery.blocked`, `delivery.failed` (with `retry`, `reason`), `delivery.finished` (counts per assessment) |
| Grading | `grading.started`, `grading.finished` (score, seconds), `grading.failed`, `grading.superseded`, `grading.resumed`, `grading.gave_up` |
| Results | `result.sent`, `result.blocked`, `result.failed` (`resent=True` for the recovery path) |
| OCR | `ocr.finished` (pages by source, engines, confidence, characters, seconds), `ocr.failed`, `ocr.download_failed`; the per-page report moved to DEBUG |
| AI | `ai.request` (DEBUG, kind, model, seconds), `ai.unavailable` |
| Alerts | `alert.ai_down`, `alert.ai_back`, `alert.grading_failed` |

## Architecture and design decisions

- **Outages are detected from real requests.** The bot does not poll the provider in the
  background. That would cost requests on a rate-limited free tier (Stage 7's concern), and a
  model list answering does not prove completions work. The cost: recovery is noticed only when
  the next real request succeeds.
- **"Down" means unreachable, not wrong.** Only `AIUnavailable` counts. Bad replies come from the
  model, not an outage; they reach the admins as grading failures. A reply counts as a success
  even if it was unusable.
- **OCR vision and correction calls are not observed.** They call the provider directly, and
  vision often goes to different (free, unreliable) models. Counting them would raise false
  alerts about the main model.
- **Batching instead of one message per failure.** Before, every failed grading sent every admin a
  message, so an outage during a class sent dozens. Now the first failure is reported at once,
  later ones at most every 15 minutes, and during an outage only the 🚨 and ✅ messages go out.
- **Database rows decide who sends each alert** (CLAUDE.md rule), with a row lock. The failure
  notice is claimed with `UPDATE … RETURNING` under the `admin_notices` lock, two independent
  guards (see [testing.md](testing.md)).
- **`failure_notice_due` instead of comparing timestamps.** `now()` is the transaction start time,
  so a failure committed just after a notice read could carry an earlier time and be skipped.
  A flag per submission cannot be missed or reported twice.
- **Logs to stdout.** Per Railway's documentation (via search results), plain lines on stderr are
  shown as errors.

## Significant code changes

| File | Change |
|---|---|
| `bot/alerts.py` (new) | `AIMonitor`, `report_grading_failures`, `notify_admins`, message wording. |
| `bot/logs.py` (new) | `event`, `JsonFormatter`, `setup_logging`. |
| `bot/ai/providers.py` | `AIUnavailable`; `check()` on both providers. |
| `bot/ai/service.py` | `_complete`: timing, events, observer; request `kind`. |
| `bot/db.py` | Schema below; `record_ai_success/failure`, `ai_health`, `claim_grading_failure_notice`, `failed_grading_by_assessment`, `status_overview`; `mark_submission_failed` takes the error. |
| `bot/handlers/admin.py` | `/status`. |
| `bot/handlers/work.py` | Events; failures go through `report_grading_failures`; the background loop flushes notices. |
| `bot/handlers/student.py` | Submit's grading failure uses `report_grading_failures`. |
| `bot/handlers/reading.py` | OCR events with timing. |
| `bot/app.py`, `bot/handlers/common.py` | Observer wiring; `/status` in the admin menu and help. |
| `bot/config.py`, `main.py`, `.env.example` | `LOG_FORMAT`; logging set up by `setup_logging`. |
| `bot/utils.py` | `Services.started_at`. |
| `tests/conftest.py` | The fake provider has `check()`. |
| `tests/test_visibility.py` (new) | Stage 5 tests (see [testing.md](testing.md)). |
| `README.md` | `/status`, the Monitoring section, layout. |

## Schema and configuration changes

Added in place on start, as in Stage 3 (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`):

- `submissions`: `grading_error TEXT`, `failed_at TIMESTAMPTZ`,
  `failure_notice_due BOOLEAN NOT NULL DEFAULT FALSE`; partial index on failed submissions.
- `ai_health`: one row (`id = 1`): consecutive failures, since when, last error, last success,
  `down_notified_at`.
- `admin_notices`: `kind`, `notified_at` (one row, `grading_failed`).

**Backwards compatible.** During the deploy, the previous version ignores the new columns and
tables. A grading failure it handles is notified by itself, one message per failure as before;
it never sets `failure_notice_due`, so the new version does not report it again.

**New setting:** `LOG_FORMAT` (`text` default, or `json`). With the default, log lines look as before
except that sending, grading and OCR lines are now events, and they go to stdout instead of
stderr.
