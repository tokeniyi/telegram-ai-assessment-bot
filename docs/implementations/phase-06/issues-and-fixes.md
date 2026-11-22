# Phase 06 — Command guidance: Challenges, debugging and fixes

| | |
|---|---|
| Roadmap stage | Stage 6 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stage-6-command-guidance` (stacked on `claude/lucid-brahmagupta-dri4yd`, PR #14) |
| Pull request | [#15](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/15) (base `main`; merge #14 first) |
| Status | Merged |

### 1. Environment: PostgreSQL had stopped again
- As in Phases 04 and 05: restarted with `pg_ctlcluster 16 main start` (stale pid file removed).

### 2. "Did you mean /regrade" built an invalid suggestion
- **Symptom:** when sample messages were rendered through the fake Telegram API, `/regrad` raised
  `ValueError: /regrade takes /regrade <id>, got ()` and the user got the generic error reply.
- **Root cause:** the unknown-command reply suggested every close match without arguments.
- **Investigation:** the registry check in `Suggestion` refused it, which is the check working as
  designed.
- **Fix:** a match that needs an assessment is shown with a real id from the database, or as its
  usage form with "nothing needs it right now". Test:
  `test_a_mistyped_command_suggests_the_closest_one_the_user_may_use`.

### 3. An example with an id that may not exist
- **Symptom:** the same rendering showed "Use it like `/close 3`" for `/close abc`, using the
  registry's sample id.
- **Fix:** the reply lists ids the command can act on now, or shows `/close <id>`.

### 4. `/start` put the user's name into HTML unescaped (existing bug)
- **Finding (while editing `/start`):** a first name containing `<` would make Telegram reject the
  welcome message. **Fix:** escaped.

### 5. Existing tests that pinned old wording
- `tests/test_visibility.py` (3 assertions) and `tests/test_recovery.py` (1): the notices now show
  `<code>/regrade 1</code>` with a button. `/regrade` on a submission that is being graded now
  answers "being graded right now … No action needed" instead of starting a run. The assertions
  were updated to the new behaviour; the behaviour they guard is unchanged.
- `tests/test_deploy.py` handover test: it counted admin messages containing "registered users",
  which `/start` now also contains (its command list comes from the registry). It now counts the
  `/users` reply itself (it starts with 👥).

### 6. Test bugs found while writing the tests
- The double-tap test counted grading calls on one process's fake AI only; the second process
  grades through its own. Both are counted now.
- The "published to nobody" test deleted the users, but pressing Publish registers the admin again;
  it now empties the queue, as `test_recovery.py` does.
- A `/pending` assertion used a student who still had open assessments; the test now closes them
  first.

### 7. Mutation checks that first proved nothing
- Two mutations replaced part of a line and left a syntax error; the script counted any non-zero
  exit as "caught". It now counts only a test failure (exit code 1), and the two were redone on
  whole lines: both caught.
- Two safeguards were not covered by any test: buttons outside private chats, and leaving the
  just-graded assessment out of "still to answer" (only reachable with `ALLOW_RESUBMISSION=true`).
  Two tests were added; both mutations are now caught.
