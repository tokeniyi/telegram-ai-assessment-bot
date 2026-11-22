# Phase 07 — Assessment features: Implementation details

| | |
|---|---|
| Roadmap stage | Stage 7 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stoic-babbage-iym3c3` |
| Pull request | [#16](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/16) |
| Status | In review |

## Features implemented

### Deadlines

- **Typing a deadline.** `/deadline <id> <when>` accepts several forms
  ([bot/deadlines.py](../../../bot/deadlines.py)):
  - from now: `2d`, `36h`, `90m`, `1w`, `1d 12h`, `in 3 days`;
  - a date and time in `TIMEZONE`: `2026-11-30 17:00`;
  - a day alone, which means 23:59: `2026-11-30`, `tomorrow`;
  - `today 18:00`, `tomorrow 9:30`;
  - `off`, to remove it.

  A deadline must be at least 5 minutes and at most a year away. `/deadline <id>` alone shows the
  current deadline with ready-made examples for that assessment.
- **Answers stop at the deadline itself.** The condition `Database.OPEN` (published, and no deadline
  or one still ahead) is used by `open_assessments_for`, `submit_draft`, `claim_delivery`, reminders
  and `set_deadline`. A student who taps Submit one second late is told the deadline passed. Their
  unsubmitted answers are discarded, as `/close` already did.
- **Closing.** `work.close_due` runs first in every `run_once`. One `UPDATE … WHERE deadline <= now()
  RETURNING` closes each passed assessment, sets `closed_at`, cancels its queued messages and skips
  its pending reminders. It hands each closed assessment to exactly one process, which tells the
  publisher how many of the users it was sent to submitted, with a 📊 Results button.
- **Publishing a draft whose deadline has passed** is refused by the handler (with `/deadline`
  examples) and by `publish_assessment` itself.
- **Shown** to students in the assessment message (with "You can submit once." or the resubmission
  note), in `/pending` button labels, and in the chat model's context. Admins see it in the draft
  preview, `/settings` and `/assessments`.

### Reminders

- `queue_due_reminders` marks an assessment `reminders_queued_at` and inserts one `reminders` row per
  student who was sent it, is active and has not submitted. Both happen in one transaction, so it
  is done once, by one process.
- An assessment is queued only if it was published before the reminder time (`deadline −
  REMINDER_HOURS`). Students sent it closer to the deadline have just seen the deadline in the
  assessment message.
- `claim_reminder` first marks as `skipped` the reminders of students who have submitted since, or
  of assessments no longer open. It then takes one row with `FOR UPDATE SKIP LOCKED` and marks it
  `sent` before it is sent.
- The message has **Submit / Undo / Discard** if the student has saved answers, otherwise
  **📝 Answer now** (`show:<id>`). A blocked bot marks the user inactive.
- `set_deadline` deletes the assessment's reminder rows and clears `reminders_queued_at`. Students who
  still have not submitted are then reminded before the new deadline.

### Per-assessment options

- `assessments.show_answers` and `assessments.allow_resubmission` are nullable. NULL follows the
  bot-wide setting, which is what every existing assessment does.
- `Settings.shows_answers(assessment)` and `Settings.allows_resubmission(assessment)` decide in Python.
  They are used by result messages (`work.grade`, `stored_result_blocks`), the chat context and the
  assessment message.
- In SQL the same rule is `COALESCE(a.allow_resubmission, <default>)`, used by
  `open_assessments_for` and `submit_draft`.
- `/settings <id>` shows both options and the deadline. Each option button carries the value to set
  (`opt:<id>:show_answers:0`), not a toggle, so a double tap does not undo the first tap.
- Resubmission cannot be changed once the assessment is closed. The answers option can, because it
  applies to results sent from then on.

## Architecture and design decisions

- **Check the deadline when answers arrive, close in the background.** The 30-second loop alone would
  accept answers for up to 30 s after the deadline. A separate scheduler was not needed.
- **Reminders at most once.** A process stopping between claiming and sending loses that one
  reminder. Leasing them like `deliveries` would instead risk repeating it. A reminder matters less
  than an assessment message, so the simpler rule was chosen (as Phase 03 did for messages that may
  have arrived).
- **One reminder time per bot** (`REMINDER_HOURS`) rather than per assessment. This keeps
  `/settings` small; it can be added as another nullable column.
- **Options as NULL = default** rather than copying the defaults into each new assessment. Old rows
  need no migration, and an older version running during a deploy ignores the columns.
- **`/deadline` is not a button command** (it changes state; Phase 06 rule). `/settings` is: it only
  shows state, and its own buttons set explicit values.
- **Time zone:** deadlines are stored as `TIMESTAMPTZ`. They are typed and shown in `TIMEZONE` (a
  zoneinfo name, checked at start-up). `tzdata` was added to `requirements.txt` so that zone names
  resolve even where the OS has no zone database.

## Significant code changes

| File | Change |
|---|---|
| [bot/deadlines.py](../../../bot/deadlines.py) | New: `parse`, `format_moment`, `time_left`, `describe`, `DeadlineError` |
| [bot/db.py](../../../bot/db.py) | `OPEN`; schema below; `set_deadline`, `set_option`, `close_due_assessments`, `queue_due_reminders`, `claim_reminder`, `finish_reminder`, `reminder_counts`; deadline and per-assessment options in `publish_assessment`, `claim_delivery`, `open_assessments_for` (parameter renamed `default_resubmission`), `submit_draft`; `closed_at` set by `/close` |
| [bot/handlers/work.py](../../../bot/handlers/work.py) | `close_due`, `send_reminders`, `reminder_message`, `closed_notice`; both added to `run_once`; per-assessment answers option |
| [bot/handlers/admin.py](../../../bot/handlers/admin.py) | `/deadline`, `/settings`, `opt:` buttons, ⚙️ button and options line in the preview, deadline in `/assessments`, publish refused after the deadline |
| [bot/handlers/student.py](../../../bot/handlers/student.py) | Deadline and resubmission note in the assessment message, deadline in `/pending`, "deadline passed" reply |
| [bot/commands.py](../../../bot/commands.py) | `deadline` and `settings` in the registry |
| [bot/config.py](../../../bot/config.py) | `TIMEZONE`, `REMINDER_HOURS`, `Settings.tz`, `shows_answers`, `allows_resubmission` |
| [bot/guidance.py](../../../bot/guidance.py), [bot/handlers/chat.py](../../../bot/handlers/chat.py), [bot/handlers/common.py](../../../bot/handlers/common.py) | Choices for the new commands, deadline and options in the chat context, admin help step |

## Schema / configuration changes

- `assessments`: `deadline TIMESTAMPTZ`, `closed_at TIMESTAMPTZ`, `reminders_queued_at TIMESTAMPTZ`,
  `show_answers BOOLEAN`, `allow_resubmission BOOLEAN` (all nullable, added with `ADD COLUMN IF NOT
  EXISTS`), and a partial index on `deadline` for open assessments.
- New table `reminders (assessment_id, user_id)` with status `pending | sent | skipped | blocked | failed`.
- Settings: `TIMEZONE` (default `UTC`) and `REMINDER_HOURS` (default 24; 0 = no reminders).
  `SHOW_CORRECT_ANSWERS` and `ALLOW_RESUBMISSION` become defaults.
- **Running next to the previous version (deploy overlap):** the previous version knows nothing of
  deadlines or options.
  - Until it stops, it may accept an answer after a deadline. It may also send a queued assessment
    message after a deadline, until this version closes the assessment (within 30 s).
  - It applies the bot-wide options rather than an assessment's own.
  - No data is lost or corrupted either way; the new columns are only read by this version.
