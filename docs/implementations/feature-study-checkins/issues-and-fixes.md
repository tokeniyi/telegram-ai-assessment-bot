# Side feature — Study check-ins: Challenges, debugging and fixes

| | |
|---|---|
| Roadmap stage | None. This is a side feature, outside the staged roadmap ([ROADMAP.md](../../../ROADMAP.md), "Outside the stages") |
| Branch | `claude/modest-allen-wfagwm` |
| Pull request | [#19](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/19) |
| Status | Merged |

No bug in the bot's code was found while building this feature. The problems below were in tests
and in what they could observe.

### 1. An existing test assumed `/start` has no buttons
- **Symptom:** after the offer was added, `tests/test_ocr_flow.py::test_explain_caption_wins_over_an_open_assessment`
  failed: `assert ['chk:on', 'chk:off'] == []`.
- **Root cause:** the test checked "not stored as an answer" with `last_keyboard(STUDENT_ID) == []`.
  `last_keyboard` returns the keyboard of the last message that has one. That is now the check-in
  offer on the welcome message.
- **Investigation:** this was the only failure in the full suite with the feature in. The explain
  reply itself still had no keyboard.
- **Fix:** the assertion now checks what it means: no draft buttons (`ans:`) and no saved draft
  parts.

### 2. A queued AI reply consumed by the wrong call
- **Symptom:** in `test_a_due_check_in_is_personal_and_sent_once_by_two_processes`, the AI context
  showed "4/4 points" instead of a lost question. The log showed "Unusable AI reply … unknown intent".
- **Root cause:** the test queued the partly-wrong grade before the student typed `1. B`. The
  chat classification of that message took the queued reply.
- **Fix:** the grade is queued after `1. B`, just before Submit.

### 3. "Study check-ins" also appears in the command list
- **Symptom:** "not offered twice" failed, because the second `/start` still contained
  "Study check-ins".
- **Root cause:** that text is also the `/checkins` summary in the command list of every welcome
  and `/help` message.
- **Fix:** the tests look for the offer's own sentence ("Would you like me to message you …").

### 4. The async harnesses cannot catch a missing row lock
- **Symptom:** none at first. But two harnesses run with `asyncio.gather` never overlap in the
  database, because every database call is synchronous inside one event loop. Phase 08 found the
  same thing (its item 3).
- **Investigation:** `FOR UPDATE OF c SKIP LOCKED` was removed from `claim_checkin`. The
  harness-based test still passed.
- **Fix:** a thread-based test was added
  (`test_two_processes_claiming_at_the_same_moment_send_one_check_in`). It uses two `Database`
  pools, a barrier, and a `decide` that sleeps while it holds the row. Without the lock it fails;
  with it, it passes.
