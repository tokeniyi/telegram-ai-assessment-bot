# Phase 01 — Saved answers survive restarts: Implementation details

| | |
|---|---|
| Roadmap stage | Stage 1 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/hopeful-mccarthy-qftfmw` |
| Pull request | [#8](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/8) (follow-up: [#9](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/9), same branch) |
| Status | Merged |

Commits: `646850b` (implementation), `74d9264` (merge of `main` with PR #7), `d80d3d9` (PR #9).

## Features implemented

### Persistent draft answers
Each saved answer message is one row in `draft_answers`. `add_draft_part`, `draft_parts`,
`undo_draft_part` and `discard_draft` in `bot/db.py` replace the `context.user_data["draft"]` dict
in `bot/handlers/student.py`. A user has at most one draft at a time: saving to another assessment
deletes the rows for the old one. The draft size limit (`MAX_DRAFT_CHARS`) is checked in the same
transaction; if exceeded, nothing is saved.

### Persistent target assessment and chat history
`user_sessions.target_assessment_id` stores the assessment picked with `/pending`
(`get_target`, `choose_assessment`). `user_sessions.chat_history` stores the chat history as JSON,
trimmed to `2 * CHAT_HISTORY_TURNS` entries (`chat_history`, `append_chat_history`, used by
`bot/handlers/chat.py`).

### Exactly-once submit
`Database.submit_draft` deletes the draft with `DELETE … RETURNING`, checks that the assessment is
still `published` (and, without resubmission, that no submission exists), and inserts the
submission, all in one transaction. It returns `submitted`, `empty` or `closed`.

### Behaviour change
Picking the same assessment again in `/pending` keeps its saved answers (previously it wiped them).
Picking a different one still discards the draft (PR #8 body, commit `646850b`).

### `/cancel` wording (PR #9)
`/cancel` said "Your typed answers were discarded." while the Discard button said
"🗑 Your unsent answers have been discarded."; `/cancel` now uses the Discard wording.

## Architecture and design decisions

- **Per-user row lock.** Every method that changes a user's draft or history first upserts that
  user's `user_sessions` row (`_lock_session`), which serialises writes for the user across all bot
  processes sharing the database.
- **Claim by delete.** The code comment in `submit_draft` notes that deleting the draft is what
  makes submit exactly-once even without the lock: a competing transaction blocks on the rows and
  then finds them gone.
- **In-memory flags kept.** The `grading` / `reading` / `explaining` flags and the `asyncio.Lock` in
  `chat.py` remain in memory; the lock still orders a quick series of messages within one process.
- Alternatives considered: **Not documented.**

## Significant code changes

PR #8 (6 files, +572 / −57, per GitHub and `git show --stat 646850b`):

| File | Change |
|---|---|
| `bot/db.py` | New tables and index; session, draft and chat-history methods; `submit_draft`. |
| `bot/handlers/student.py` | Draft, target, undo, discard, `/cancel` and submit use the database. |
| `bot/handlers/chat.py` | History read/written through the database; `draft_parts` takes `user_id`. |
| `tests/test_sessions.py` | New, 21 tests. |
| `docs/DOCUMENTATION.md` | New section on persistent sessions. |
| `README.md` | Caveat updated: drafts survive restarts; interrupted grading needs `/regrade`. |

Merge commit `74d9264` resolved a conflict with PR #7 in `student.py` `add_to_draft` and updated two
assertions in `tests/test_sessions.py` to PR #7's wording.

PR #9 (`d80d3d9`): one line in `bot/handlers/student.py` and one assertion in `tests/test_sessions.py`.

## Schema / configuration changes

- `user_sessions (user_id BIGINT PK, target_assessment_id BIGINT → assessments ON DELETE SET NULL,
  chat_history TEXT DEFAULT '[]', updated_at TEXT)`.
- `draft_answers (id identity PK, user_id BIGINT, assessment_id BIGINT → assessments ON DELETE
  CASCADE, text TEXT, created_at TEXT)`, index `idx_draft_answers_user (user_id, id)`.
- Created with `CREATE TABLE IF NOT EXISTS`; existing tables and data are not altered. No new
  settings.
- Compatibility with the old version running at the same time: the old version ignores the new
  tables. PR #8 notes that students mid-answer during the first deploy lose their in-memory draft
  once, because it was never in the database.
