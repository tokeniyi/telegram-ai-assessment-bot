# Phase 07 — Assessment features: Testing and validation

| | |
|---|---|
| Roadmap stage | Stage 7 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stoic-babbage-iym3c3` |
| Pull request | [#16](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/16) |
| Status | Merged |

## Methodology

- **Unit tests** of `bot/deadlines.py`:
  - every accepted form;
  - `TIMEZONE` (a time typed in Africa/Lagos, and "today" being the local day);
  - every refusal (past, too soon, over a year, unreadable);
  - formatting.

  Config tests check `TIMEZONE` and `REMINDER_HOURS`.
- **Through the bot** with the fake Telegram API and AI provider. Admins type `/deadline` and
  `/settings` and press the buttons; students answer, submit and press reminder buttons. Time passing
  is simulated by moving `deadline` (and `published_at`) with SQL. The background work is run with
  `work.run_once`, as the bot does every 30 s.
- **Two processes:** two harnesses on one database run the background work at the same time. The
  deadline notice is sent once, and each student gets one reminder.
- **Races in the database:**
  - a student submits between queueing and sending: skipped;
  - a deadline passes before the background work closes the assessment: answers already refused,
    and the deadline can no longer be changed;
  - a draft whose deadline passed is published: `publish_assessment` refuses even without the
    handler's check.
- **Mutation checks:** 15 safeguards were removed one at a time (script kept out of the
  repository), and each made `tests/test_assessment_features.py` fail. They were:
  - `OPEN` ignoring the deadline;
  - publish ignoring it, in the database and in the handler;
  - closing leaving messages queued;
  - reminders for students who submitted, at queueing and at sending;
  - a reminder right after a late publish;
  - a moved deadline keeping old reminders;
  - a blocked student staying active;
  - option buttons open to students;
  - the answers option ignored;
  - the resubmission option ignored when listing and when submitting;
  - resubmission changeable when closed;
  - wrong counts in the closing notice.

  One more mutation (the `REMINDER_HOURS=0` shortcut) is equivalent to the original; see
  [issues-and-fixes.md](issues-and-fixes.md), item 3.

## Tests added

All in [tests/test_assessment_features.py](../../../tests/test_assessment_features.py) (43 tests, counting each parameter set).

| Test | Behaviour |
|---|---|
| `test_parse_deadline` (12 cases), `test_parse_deadline_in_the_bots_time_zone`, `test_parse_deadline_refuses` (8), `test_format_deadline` | Reading and showing times |
| `test_settings_timezone_and_reminder_hours` | Defaults, values, invalid `TIMEZONE` refused at start-up |
| `test_deadline_on_a_draft_is_shown_to_students_when_published` | Set on a draft; shown in the assessment message, `/pending`, `/assessments` |
| `test_deadline_without_a_time_shows_it_with_real_examples` | `/deadline 1` and `/deadline` give examples with real ids |
| `test_deadline_that_cannot_be_read_is_an_input_problem` | ✏️ reply, examples, nothing saved |
| `test_deadline_cannot_change_once_closed` | Closed, or passed but not yet closed: ℹ️ with a Results button |
| `test_deadline_can_be_removed` | `off` |
| `test_a_draft_whose_deadline_passed_is_not_published` | Handler and database refuse |
| `test_answers_stop_at_the_deadline_before_the_assessment_is_closed` | Submit one second late is refused; draft discarded |
| `test_the_deadline_closes_the_assessment_and_tells_the_publisher_once` | Two processes: one notice, right counts |
| `test_messages_not_sent_by_the_deadline_are_cancelled` | Queued messages cancelled by closing |
| `test_students_who_have_not_submitted_are_reminded_once` | Two processes; right recipients; Submit vs Answer now buttons |
| `test_a_student_who_submits_before_the_reminder_is_sent_is_skipped` | Late submission skips the reminder |
| `test_no_reminder_when_published_close_to_the_deadline` | Published inside the window: none |
| `test_moving_the_deadline_reminds_again_before_the_new_one` | Reminded again only when the new deadline is close |
| `test_reminders_can_be_switched_off` | `REMINDER_HOURS=0` |
| `test_a_student_who_blocked_the_bot_is_not_reminded_again` | Blocked: marked inactive |
| `test_settings_shows_the_options_and_their_buttons` | Preview line, ⚙️ button, `/settings` view |
| `test_hiding_reference_answers_for_one_assessment` | Hidden for one assessment, shown for another; double tap |
| `test_resubmission_allowed_for_one_assessment_only` | Allowed for one, not the other; switched off again |
| `test_option_buttons_are_for_admins_only` | Students cannot change options |
| `test_resubmission_cannot_be_changed_once_closed` | No button, and a forged one is refused |

## Results

```
python -m pytest -q
308 passed, 2 skipped in 135.41s (0:02:15)
```

Run on PostgreSQL 16 in the development container with
`TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bot_test` (the default). Before this
phase: 265 passed, 2 skipped. The 2 skipped tests are the same as before: tesseract and easyocr
are not installed in the container.

## Outstanding failures

None.

**Not verified:** behaviour on real Telegram clients and on Railway, including a real deploy overlap
with the previous version.
