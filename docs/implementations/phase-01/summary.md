# Phase 01 — Saved answers survive restarts: Final summary

| | |
|---|---|
| Roadmap stage | Stage 1 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/hopeful-mccarthy-qftfmw` |
| Pull request | [#8](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/8) (follow-up: [#9](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/9), same branch) |
| Status | Merged |

## Accomplished

- Unsubmitted answers, the `/pending` choice and chat history stored in PostgreSQL
  (`user_sessions`, `draft_answers`).
- Buttons from a previous process keep working after a restart.
- Exactly-once submit via `Database.submit_draft`.
- 21 tests in `tests/test_sessions.py`; CI passed (153 passed, 2 skipped).
- PR #9: `/cancel` wording matches the Discard button.

## Changes and decisions

- **Per-user lock on `user_sessions`:** all draft/history writes for a user are serialised across
  processes. Later phases that change per-user state should take the same lock.
- **Claim-by-delete submit:** `DELETE … RETURNING` makes double taps and concurrent submits produce
  one submission. Phase 02 relies on this for double taps, which its update guard does not merge.
- **Slow-work flags stay in memory:** not correctness guards; a restart mid-grading leaves the
  submission `pending` until `/regrade`.
- **Same assessment in `/pending` keeps the draft** (behaviour change).

## Outstanding issues

- Grading and sending still run only in memory; a restart mid-way is not resumed (ROADMAP Stage 3,
  addressed in [Phase 03](../phase-03/summary.md)).
- Students mid-answer during the first deploy of this change lost their in-memory draft once
  (PR #8 body).
- `docs/DOCUMENTATION.md` labels this change "PR #7"; it is PR #8.
- Post-merge manual check on Railway: **Unable to verify.**

## Final status

Implemented and tested; PR #8 merged 2026-11-17 and PR #9 merged 2026-11-18. No reviews or review
comments on GitHub.

## Pull request

- https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/8
- https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/9
