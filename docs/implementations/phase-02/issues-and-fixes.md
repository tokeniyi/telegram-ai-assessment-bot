# Phase 02 — Each Telegram update handled once: Challenges, debugging and fixes

| | |
|---|---|
| Roadmap stage | Stage 2 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/idempotent-updates` |
| Pull request | [#10](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/10) |
| Status | Merged |

The PR has one commit and no reviews, review comments or discussion comments on GitHub. Debugging
during development beyond the item below: **Not documented.**

### Restart tests would reuse update IDs
- **Symptom:** described in PR #10: without a change to the harness, "the existing restart tests
  would reuse IDs and be dropped as copies". Whether this was observed as failing tests or
  anticipated: **Unable to verify.**
- **Root cause:** each `Harness` kept its own `_update_id` counter starting at 1. A second harness
  standing for a restarted bot on the same database reused IDs already claimed in
  `processed_updates`. Real Telegram numbers a bot's updates in one sequence across processes.
- **Investigation:** **Not documented.**
- **Fix:** `tests/conftest.py` (`d94777b`): `make_harness` creates one `itertools.count(1)` and
  passes it to every harness it builds.
