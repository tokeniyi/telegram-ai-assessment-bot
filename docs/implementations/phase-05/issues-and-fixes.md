# Phase 05 — Visibility for the admin: Challenges, debugging and fixes

| | |
|---|---|
| Roadmap stage | Stage 5 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/lucid-brahmagupta-dri4yd` |
| Pull request | [#14](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/14) |
| Status | Merged |

### 1. Environment: PostgreSQL had stopped again
- **Symptom:** `pg_isready` reported "no response" at the start of the session, as in Phase 04
  (item 7).
- **Fix:** `pg_ctlcluster 16 main start` (it removed a stale pid file). Not a code issue.

### 2. Logs on stderr are shown as errors on Railway
- **Finding:** Railway's documentation (read through search results) says plain lines on stderr
  are shown as errors. `logging.basicConfig` writes to stderr, so every line the bot wrote, INFO
  included, would have been marked as an error.
- **Fix:** `setup_logging` writes to stdout. Guarded by `test_main_py_logs_json_lines_to_stdout`.
  This was not observed on a real Railway deploy (**Unable to verify** from here).

### 3. Logging broke an AI call when a provider had no `model` attribute
- **Symptom:** `tests/test_chat.py::test_converse_rejects_unknown_intents_and_empty_chat` failed
  with `AttributeError: 'P' object has no attribute 'model'` on the first full run.
- **Root cause:** `_complete` read `self.provider.model` for the log event. That test's stub
  provider has only `complete`.
- **Fix:** `getattr(self.provider, "model", None)`. Logging must never fail the work it describes.

### 4. A field called `level` collided with `event()`'s own parameter
- **Symptom:** `TypeError: event() got multiple values for argument 'level'` in
  `test_json_lines_carry_the_event_fields`.
- **Fix:** `logger`, `name` and `level` are positional-only (`/`), so any keyword is a field. The
  JSON formatter renames fields that clash with `time`, `level`, `logger` or `message`.

### 5. Test bugs found while writing the tests
- `test_sending_and_grading_write_events` expected one `delivery.sent`, but the admin had also
  started the bot and was sent the assessment too. The expectation now lists both users.
- The first draft of `tests/test_visibility.py` had three mistakes, fixed before its first run:
  a leftover no-op line, a `del` of a class method on an instance, and a convoluted assertion on
  request kinds.

### 6. A safeguard that could never be the only one
- **Finding (while planning the mutation checks):** clearing `failure_notice_due` in
  `save_grade` and `claim_for_regrade` duplicated the `status = 'failed'` filter of the notice
  query. Removing either one alone could not change any behaviour.
- **Fix:** the clearing was removed; the filter is the guard, and it is mutation-tested.

### 7. A mutation slipped through: the concurrent notice claim
- **Symptom:** replacing the atomic `UPDATE … RETURNING` with a SELECT followed by an UPDATE did
  not fail `test_concurrent_notice_claims_report_each_failure_once`.
- **Investigation:** removing the `FOR UPDATE` on the `admin_notices` row as well still did not
  fail. In the test, the row did not exist yet, so every thread's `INSERT … ON CONFLICT DO NOTHING`
  waited for the first thread's uncommitted insert, which serialised them. In production the row
  exists after the first notice.
- **Fix:** the test creates the row first. Now removing both guards fails the test; removing either
  one alone does not, because the claim has two independent guards.
