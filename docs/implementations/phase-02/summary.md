# Phase 02 — Each Telegram update handled once: Final summary

| | |
|---|---|
| Roadmap stage | Stage 2 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/idempotent-updates` |
| Pull request | [#10](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/10) |
| Status | Merged |

## Accomplished

- `processed_updates` table and a group −2 guard (`bot/handlers/dedupe.py`) that claims each
  `(bot_id, update_id)` before any other handler runs and drops copies in any process.
- Fail-open when the claim cannot be recorded; records older than 48 hours pruned at start-up and
  every 1000 updates.
- Test harness shares one update-ID counter across restarted harnesses.
- 12 tests in `tests/test_updates.py`; CI passed (165 passed, 2 skipped).

## Changes and decisions

- **At most once:** the claim is taken before handling; an update cut short by a crash is not
  replayed, and the user sends it again. Chosen over replaying half-done work (PR #10).
- **Fails open:** a database hiccup does not silence the bot, at the cost of possibly handling a rare
  copy twice.
- **Double taps are out of scope:** different update IDs; Submit relies on `Database.submit_draft`
  (Phase 01). Any other action that must not run twice needs its own guard.
- **`bot_id` in the key:** a new bot token starts its own numbering.
- Tests that simulate a restart must use the shared update-ID counter from `make_harness`.

## Outstanding issues

- Two quick taps on "Send to all users" can still broadcast twice (`on_draft_button` check-then-set);
  listed in ROADMAP Stage 3, addressed in [Phase 03](../phase-03/summary.md).
- Grading and sending interrupted by a restart are not resumed (ROADMAP Stage 3).
- Duplicates mainly arise while two versions run during a Railway deploy; shortening that window is
  ROADMAP Stage 4.
- PR #10's statement that 5 new tests fail with the guard off: **Unable to verify** (not re-run).

## Final status

Implemented and tested; PR #10 merged 2026-11-19. No reviews or review comments on GitHub.

## Pull request

https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/10
