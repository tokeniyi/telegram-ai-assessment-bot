# Side feature — Study check-ins: Testing and validation

| | |
|---|---|
| Roadmap stage | None. This is a side feature, outside the staged roadmap ([ROADMAP.md](../../../ROADMAP.md), "Outside the stages") |
| Branch | `claude/modest-allen-wfagwm` |
| Pull request | Not opened yet |
| Status | In progress |

## Methodology

- **Pure functions:** `slot`, `day_start`, `gap_days` and `decide` are tested with fixed times, in
  UTC and Africa/Lagos.
- **Through the bot**, with the fake Telegram API and AI provider (`tests/conftest.py`):
  - Students type `/start` and `/checkins`, and press the `chk:` buttons.
  - Admins type `/status`.
  - The background work runs with `work.run_once` or `work.send_checkins`.
  - Time passing is simulated by moving `study_checkins.next_at`, `users.last_active_at`,
    `last_sent_at` and `reminders.sent_at` with SQL.
  - The fake AI answers the check-in prompt with text containing `<b>`, so escaping is checked.
- **Failures:**
  - the AI is down (`AIUnavailable`), or Stage 5 counts it as down (`ai_health`);
  - Telegram answers 403 (blocked);
  - the process dies mid-send (`Crash` from the fake Telegram API).
- **Two processes:**
  - two harnesses on one database (a deploy);
  - a real two-thread race on `claim_checkin` (see [issues-and-fixes.md](issues-and-fixes.md),
    item 4).
- **Mutation checks:** each safeguard was removed, the tests were run, and it was put back.
  - `FOR UPDATE OF c SKIP LOCKED` removed from `claim_checkin`: the thread race test fails
    (1 failed, 20 passed).
  - `decide` not moving `next_at` before sending: 8 tests fail, including
    `test_a_check_in_cut_off_by_a_crash_is_not_sent_twice`.

## Tests added

All in [tests/test_checkins.py](../../../tests/test_checkins.py). The file collects 21 tests, because
`test_decide` has 5 cases.

| Test | Behaviour checked |
|---|---|
| `test_slot_is_the_hour_in_the_bots_time_zone` | The hour today, tomorrow when passed, N days ahead, in `TIMEZONE`; local midnight |
| `test_the_gap_grows_when_check_ins_are_ignored` | ×1, ×1, ×2, ×4, ×4 |
| `test_decide` (5 cases) | blocked, missed, recent activity, reminded today, send; and the time of the next one |
| `test_decide_counts_unanswered_check_ins_and_resets_on_a_reply` | Unanswered count and gap; a reply resets both |
| `test_settings` | `CHECKIN_DAYS`/`CHECKIN_HOUR` defaults, values, and refusal of hour 24 |
| `test_students_are_offered_check_ins_once_and_nothing_is_sent_without_a_yes` | Admins not asked; one offer across two processes; nothing sent while undecided; No removes the buttons |
| `test_saying_yes_schedules_the_first_check_in_for_tomorrow` | First one tomorrow at 17:00; the reply suggests `/checkins off` |
| `test_the_checkins_command` | Status, on, pause (refused when off), invalid choice (INPUT with the three choices), paused status, stop |
| `test_switched_off_bot_wide` | `CHECKIN_DAYS=0`: no offer, `/checkins` says off, nothing sent even if due |
| `test_a_due_check_in_is_personal_and_sent_once_by_two_processes` | Sent once by two processes; escaped; buttons; the AI's context has the open assessment, lost question and chat question, but not answers or reference answers; saved in chat history; a reply reaches the chat AI with it |
| `test_no_check_in_to_a_student_who_was_just_active_or_reminded_today` | Put off after recent activity and after a reminder today; sent otherwise, without a pending button when nothing is open |
| `test_unanswered_check_ins_come_less_often_until_the_student_replies` | Gaps of 3, 6, 12 days; back to 3 after a reply |
| `test_a_built_in_message_when_the_ai_is_down` | Built-in text on `AIUnavailable`; no AI call at all while `ai_health` counts it as down |
| `test_a_student_who_blocked_the_bot_is_not_messaged_again` | 403 marks them blocked; never retried; shown in `/status` |
| `test_a_check_in_cut_off_by_a_crash_is_not_sent_twice` | A crash after the claim: the next process does not resend it |
| `test_status_shows_check_ins` | The `/status` section |
| `test_two_processes_claiming_at_the_same_moment_send_one_check_in` | Real concurrent claims: one "send", one "nothing due" |

One existing test was changed: `tests/test_ocr_flow.py::test_explain_caption_wins_over_an_open_assessment`
(see [issues-and-fixes.md](issues-and-fixes.md), item 1).

## Results

PostgreSQL 16 was started in the development container, and the `bot_test` database was created
with the CI credentials. Before the change, the suite had 324 passed and 2 skipped.

```
python -m pytest -q
345 passed, 2 skipped in 119.87s (0:01:59)
```

The 2 skipped tests were skipped before this change too.

## Outstanding failures

None.

**Unable to verify:**

- the check-in text written by a real AI model;
- delivery on real Telegram clients;
- behaviour on Railway during a real deploy.
