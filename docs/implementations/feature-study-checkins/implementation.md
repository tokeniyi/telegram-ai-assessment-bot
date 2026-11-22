# Side feature — Study check-ins: Implementation details

| | |
|---|---|
| Roadmap stage | None. This is a side feature, outside the staged roadmap ([ROADMAP.md](../../../ROADMAP.md), "Outside the stages") |
| Branch | `claude/modest-allen-wfagwm` |
| Pull request | [#19](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/19) |
| Status | Merged |

## Features implemented

### Opting in

- The first `/start` of a non-admin adds a "📅 Study check-ins" question to the welcome message. It
  has the buttons **Yes, check in on me** (`chk:on`) and **No thanks** (`chk:off`).
  - "First" is decided by the database: `Database.offer_checkins` inserts the row with
    `ON CONFLICT DO NOTHING`.
  - Pressing either button removes both buttons.
- `/checkins` with no argument shows the current state, with buttons to change it:
  - on, with the time of the next one;
  - paused, until when;
  - off.
- `/checkins on` (also `yes`, `start`, `resume`) schedules the first check-in for tomorrow at
  `CHECKIN_HOUR`.
- `/checkins off` (also `no`, `stop`) turns them off.
- `/checkins pause` stops them for 7 days. They then start again by themselves.
  - It is refused (STATE) when check-ins are off.
  - Any other argument gets an INPUT reply listing the three choices.
- With `CHECKIN_DAYS=0`:
  - nothing is offered or sent;
  - `/checkins` says the feature is switched off;
  - students' choices are kept for when it is switched on again.

### When a check-in is sent

The pure function `checkins.decide` ([bot/checkins.py](../../../bot/checkins.py)) decides what to do
with a due row:

| Case | Action |
|---|---|
| The user blocked the bot (`users.is_active` false) | `blocked`: no more check-ins |
| More than 3 h past the hour (no bot was running) | `missed`: put off to the next check-in hour |
| The student used the bot in the last 12 h | `recent`: put off to tomorrow |
| A deadline reminder was sent to them since local midnight | `reminded`: put off to tomorrow |
| Otherwise | `send`, and schedule the next one |

- The gap to the next check-in is `CHECKIN_DAYS` (×1). It grows when check-ins go unanswered
  (`gap_days`):
  - after the 2nd unanswered in a row, ×2;
  - from the 3rd, ×4.
- "Answered" means any update from the student after the last check-in (`users.last_active_at`). It
  resets the count.
- 12 h rather than 24 h of "recent" activity: with `CHECKIN_DAYS=1`, a student who answered
  yesterday's check-in at 17:05 would otherwise skip every other day.

### What the AI is told

- `checkins.SOURCES` is a tuple of functions. Each returns lines of facts about the student:
  - `open_assessments`: up to 3 they can still answer, with time left and saved answer messages;
  - `recent_results`: the latest graded result, and up to 3 questions in it that lost points.
    Reference answers are never included;
  - `recent_questions`: the last 3 things they wrote in chat. Messages saved as answers are left out.
- Each fact is cut to 200 characters, and all of them to 2,000.
- A source that raises is logged and skipped.
- Study-session memory will be one more function in `SOURCES`.
- `AssessmentAI.checkin` ([bot/ai/service.py](../../../bot/ai/service.py), `CHECKIN_SYSTEM`) asks
  for:
  - one to three sentences, under 60 words, plain text;
  - personal from the context, inventing nothing;
  - no assessment answers and no commands;
  - JSON `{"message": ...}`.

  A reply longer than 600 characters counts as unusable, and the call is retried once
  (`_json_call`).

### Sending

- `work.send_checkins` runs in the background loop, every 30 s, after automatic regrading.
  - It takes due rows one at a time with `Database.claim_checkin` and sends at most 10 per pass
    (`CHECKINS_PER_PASS`).
- The message is the AI's text (HTML-escaped), then an italic line: reply here for help, and
  `/checkins` to change them.
- Buttons:
  - **📝 Open assessments** (`cmd:pending`), only if the student has some to answer
    (`guidance.student_next_steps`);
  - **⏸ Pause a week** (`chk:pause`).
- If `ai_health.consecutive_failures >= AI_DOWN_AFTER`, the AI is not called. If the call fails, the
  built-in text is used ("Hi <name>! A quick check-in: …").
- Outcomes:
  - Sent: the text is added to the chat history as an `Assistant` turn.
  - Forbidden (the student blocked the bot): the user is marked inactive and check-ins `blocked`.
  - Other Telegram errors: the attempt is recorded as `failed`, with no retry (not worth a
    duplicate).
- Log events: `checkin.sent` (with `source` ai/fallback), `checkin.failed`, `checkin.blocked`, and
  `checkin.skipped` (debug, with the reason).

### `/status`

A "Study check-ins" section shows:

- how many receive them, how many are paused, how many said no, and how many have not answered the
  offer;
- how many were sent in the last 7 days, and how many of those used the built-in text;
- failures, with the latest error;
- how many were stopped because the student blocked the bot.

## Architecture and design decisions

- **At most once, like Stage 7 reminders.**
  - `claim_checkin` locks one due row (`FOR UPDATE OF c SKIP LOCKED`) and calls `decide`.
  - In the same transaction it moves `next_at` (and, for `send`, writes the `checkin_log` row).
  - The message is written and sent after that.
  - A process stopping in between loses that one check-in rather than sending it twice. A repeat
    check-in is worse than a missing one.
- **`decide` is passed into the database method** so that the decision runs inside the transaction
  holding the lock, and stays a pure, unit-tested function. The alternative was the scheduling rules
  in SQL, with time-zone arithmetic. It was rejected as harder to read and test.
- **Opt-in rather than opt-out** (agreed with the user). Unrequested messages lead to blocks, and a
  blocked bot also stops reaching that student with assessments.
- **Offer in the welcome message, not a second message.** `/start` still sends exactly one message.
  The buttons are on it.
- **No AI call while Stage 5 counts the provider as down.** A pass of 10 check-ins does not wait for
  10 timeouts. No probing is added (the Phase 05 decision).
- **No retries of failed sends.** A failed check-in waits for the next scheduled one.

## Significant code changes

| File | Change |
|---|---|
| [bot/checkins.py](../../../bot/checkins.py) | New: `slot`, `day_start`, `gap_days`, `decide`, fact `SOURCES`, `compose`, built-in text, message HTML |
| [bot/handlers/checkins.py](../../../bot/handlers/checkins.py) | New: `/checkins`, the `chk:` buttons, the offer text |
| [bot/handlers/work.py](../../../bot/handlers/work.py) | `send_checkins` and `checkin_markup`; added to `run_once` |
| [bot/handlers/common.py](../../../bot/handlers/common.py) | `/start` offers check-ins once to students |
| [bot/handlers/admin.py](../../../bot/handlers/admin.py) | `checkin_lines` in `/status` |
| [bot/ai/service.py](../../../bot/ai/service.py) | `CHECKIN_SYSTEM`, `AssessmentAI.checkin` |
| [bot/db.py](../../../bot/db.py) | Schema below; `offer_checkins`, `get_checkins`, `set_checkins`, `claim_checkin`, `finish_checkin`, `prune_checkin_log`, `checkin_overview`; `upsert_user` sets `last_active_at` |
| [bot/commands.py](../../../bot/commands.py) | `/checkins [choice]`, `feature="checkin_days"` |
| [bot/config.py](../../../bot/config.py) | `CHECKIN_DAYS`, `CHECKIN_HOUR` |
| [tests/conftest.py](../../../tests/conftest.py) | The fake AI answers the check-in prompt |
| [tests/test_ocr_flow.py](../../../tests/test_ocr_flow.py) | One assertion made specific (see [issues-and-fixes.md](issues-and-fixes.md), item 1) |
| README, `.env.example`, [docs/DOCUMENTATION.md](../../DOCUMENTATION.md) | Command and settings |

## Schema and configuration changes

- `users.last_active_at TIMESTAMPTZ`: set on every update by `upsert_user`. NULL for an update
  handled by an older version, which counts as "not recently active".
- `study_checkins`:
  - one row per user who was offered check-ins or chose them;
  - `status` is offered, on, off or blocked;
  - also `next_at`, `paused_until`, `ignored` and `last_sent_at`;
  - a partial index on `next_at` where `status = 'on'`.
- `checkin_log`: one row per check-in taken for sending, with `outcome` (sent, failed or blocked)
  and `source` (ai or fallback). Pruned after 90 days.
- Settings:
  - `CHECKIN_DAYS`: default 3, 0 switches the feature off;
  - `CHECKIN_HOUR`: 0–23, default 17, in `TIMEZONE`.
- **Running beside an older version during a deploy.** Everything is added with `IF NOT EXISTS`.
  The older version ignores the new table and column. It sends no check-ins and does not offer them.
  It answers `/checkins` with "I don't know", and its `chk:` buttons get no answer until it stops.
