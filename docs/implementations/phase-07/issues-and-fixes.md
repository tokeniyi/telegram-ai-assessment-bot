# Phase 07 — Assessment features: Challenges, debugging and fixes

| | |
|---|---|
| Roadmap stage | Stage 7 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stoic-babbage-iym3c3` |
| Pull request | [#16](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/16) |
| Status | Merged |

### 1. "2d" was shown as "in 1 day 23 h"
- **Symptom:** `test_deadline_on_a_draft_is_shown_to_students_when_published` failed. The reply to
  `/deadline 1 2d` read "(in 1 day 23 h)".
- **Root cause:** `time_left` rounded the minutes down. A few milliseconds pass between parsing and
  formatting, so two days minus a moment became 1 day 23 h 59 min, shown as "1 day 23 h".
- **Investigation:** the failing assertion showed the text.
- **Fix:** round to the nearest minute ([bot/deadlines.py](../../../bot/deadlines.py), `time_left`).

### 2. No next step when `/deadline` names a closed assessment
- **Symptom:** `test_deadline_cannot_change_once_closed` expected a 📊 Results button and got none.
- **Root cause:** the shared argument check (`_assessment_from_args`) adds the results suggestion
  only for `/close` on a closed assessment.
- **Fix:** it now does the same for `/deadline` ([bot/handlers/admin.py](../../../bot/handlers/admin.py)).

### 3. Two guards that a test did not catch when removed
- **Symptom:** the mutation run (see [testing.md](testing.md)) left 2 of 16 removed guards uncaught.
- **Root cause:**
  - Cancelling queued messages when the deadline closes an assessment is also done by the existing
    `claim_delivery_reports`. The test ran the whole loop, so either path made it pass.
  - The `REMINDER_HOURS=0` check in `send_reminders` changes nothing when removed: a 0-second window
    never queues a reminder. It only saves a query.
- **Fix:** the test now checks the queue right after `work.close_due`, before the rest of the loop.
  The second mutation is equivalent to the original code and was dropped from the run. Reminders
  being off is still tested (`test_reminders_can_be_switched_off`).

### 4. Mistakes in the new tests (not bugs in the bot)
- A test expected 6 reminders. There are 5: five students and the admin, less the student who had
  submitted.
- A test parsed `reply_markup` as a JSON string. The fake Telegram API records it as a dict.

### 5. Existing tests that changed on purpose
- The draft preview has a new ⚙️ Settings button, so the assertions on its keyboard in
  `tests/test_flow.py` and `tests/test_ocr_flow.py` now include `cmd:settings:1`.
- `Database.open_assessments_for(include_submitted=…)` became `default_resubmission=…` (same meaning
  when the assessment has no option of its own). `tests/test_db.py` was updated.

### 6. Environment
- PostgreSQL was installed but not running in the development container, and the Python
  dependencies were not installed. PostgreSQL was started, `bot_test` was created and
  `requirements-dev.txt` installed before the baseline run (265 passed, 2 skipped).
