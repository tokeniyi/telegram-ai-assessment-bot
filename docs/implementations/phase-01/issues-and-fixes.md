# Phase 01 — Saved answers survive restarts: Challenges, debugging and fixes

| | |
|---|---|
| Roadmap stage | Stage 1 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/hopeful-mccarthy-qftfmw` |
| Pull request | [#8](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/8) (follow-up: [#9](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/9), same branch) |
| Status | Merged |

No bugs found during development are recorded in the commits or PRs. The PRs have no review
comments, reviews or discussion comments on GitHub. Debugging during development: **Not documented.**

The recorded problems are:

### Merge conflict with PR #7
- **Symptom:** a conflict when merging `main` (with PR #7's message wording) into the branch.
- **Root cause:** both changes touched `add_to_draft` in `bot/handlers/student.py`: PR #8 replaced
  the in-memory draft with `svc.db.add_draft_part`; PR #7 reworded the "too long" message.
- **Investigation:** **Not documented.**
- **Fix:** merge commit `74d9264` keeps the database call with PR #7's "size limit" wording, and
  updates two assertions in `tests/test_sessions.py` to PR #7's wording (`/cancel` with nothing
  saved, and the Discard button).

### `/cancel` and Discard said different things
- **Symptom:** `/cancel` replied "Your typed answers were discarded." while the Discard button
  replied "🗑 Your unsent answers have been discarded." (PR #9).
- **Root cause:** two messages for the same action, worded separately.
- **Investigation:** **Not documented.**
- **Fix:** PR #9 / `d80d3d9` makes `/cancel` use the Discard wording and updates the one assertion
  that expected the old text. `docs/BOT_MESSAGE_AUDIT.md` was left unchanged on purpose: it records
  the messages as they were before PR #7.

## Documentation inconsistencies noticed while writing this

- `docs/DOCUMENTATION.md` titles this change "PR #7: persistent user sessions"; on GitHub it is
  PR #8 (PR #7 is the bot message wording change).
- PR #8's body says durable jobs are "planned for Stage 4"; ROADMAP.md (written later) places them
  in Stage 3. The commit message says "a later stage".
