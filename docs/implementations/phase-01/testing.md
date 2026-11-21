# Phase 01 — Saved answers survive restarts: Testing and validation

| | |
|---|---|
| Roadmap stage | Stage 1 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/hopeful-mccarthy-qftfmw` |
| Pull request | [#8](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/8) (follow-up: [#9](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/9), same branch) |
| Status | Merged |

## Methodology

All tests in [`tests/test_sessions.py`](../../../tests/test_sessions.py) run against a real
PostgreSQL database (`TEST_DATABASE_URL`).

- **Restart simulation (end-to-end):** the `make_harness` fixture builds a second bot application on
  the same database with a fake Telegram API; the old one receives no more updates. This proves state
  lives in the database, not in process memory.
- **Two processes at once:** two harnesses press Submit concurrently (`asyncio.gather`), standing
  for two bot versions during a deploy.
- **Storage layer:** `Database` methods called directly, including threaded concurrent submits and a
  deterministic lock-wait race.

## Tests added (21)

| Test | Checks |
|---|---|
| `test_answers_survive_a_restart_and_can_be_submitted` | Answers from before and after a restart end up in one submission. |
| `test_submit_button_from_before_the_restart_still_works` | An old Submit button works in the new process. |
| `test_undo_after_a_restart` | Undo removes the last saved message after a restart. |
| `test_cancel_and_discard_after_a_restart` | `/cancel` and Discard work after a restart; wording of both cases. |
| `test_resuming_through_pending_keeps_the_saved_answers` | Re-picking the same assessment keeps the draft. |
| `test_chosen_assessment_survives_a_restart` | The `/pending` choice persists. |
| `test_switching_assessments_discards_the_other_draft` | One draft per user. |
| `test_chat_history_survives_a_restart` | Chat history is passed to the AI after a restart. |
| `test_repeated_submit_creates_one_submission` | Pressing Submit twice gives one submission. |
| `test_submit_from_two_processes_at_once_creates_one_submission` | Concurrent submits from two processes give one submission. |
| `test_draft_parts_are_kept_in_order_and_per_user` | Order and per-user isolation. |
| `test_a_user_has_one_draft_at_a_time` | Saving to another assessment replaces the draft. |
| `test_draft_size_limit_saves_nothing` | Over the limit, nothing is saved. |
| `test_undo_and_discard` | Return values of undo and discard. |
| `test_choose_assessment_keeps_a_matching_draft` | `choose_assessment` keeps or drops drafts correctly. |
| `test_submit_draft_outcomes` | `submitted` / `empty` / `closed` outcomes. |
| `test_concurrent_submits_create_exactly_one_submission` | Threaded concurrent submits. |
| `test_submits_wait_for_the_users_lock_and_then_find_nothing_left` | Deterministic race on the user lock. |
| `test_concurrent_appends_all_land` | Concurrent draft appends are all saved. |
| `test_deleting_an_assessment_removes_its_drafts_and_target` | `ON DELETE CASCADE` / `SET NULL`. |
| `test_chat_history_is_trimmed_and_survives_reconnecting` | Trimming to `keep`; persistence across connections. |

## Results

At merge time (GitHub Actions `test` job, `python -m pytest -q`, PostgreSQL 16, Python 3.12):

| PR | Head commit | CI result |
|---|---|---|
| #8 | `74d9264` | `153 passed, 2 skipped in 45.27s` (success) |
| #9 | `d80d3d9` | `153 passed, 2 skipped in 79.64s (0:01:19)` (success) |

GitGuardian Security Checks also passed on both. The PR bodies report the same count from a local
run on PostgreSQL 16. Which 2 tests were skipped: **Not documented** (not in the log tail retrieved).

Re-run on 2026-11-21 against current code (branch `claude/lucid-brahmagupta-dri4yd`, commit
`ee49980`), not the result at merge time:

```
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bot_test \
  python -m pytest tests/test_sessions.py tests/test_updates.py -q
33 passed in 5.11s
```

(21 from this phase plus 12 from Phase 02.)

## Outstanding failures

None at merge time. Manual check on Railway after merging (answer two questions, redeploy, answer a
third, Submit) was recommended in PR #8; whether it was done: **Unable to verify.**
