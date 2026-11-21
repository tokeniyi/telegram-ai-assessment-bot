# Phase 02 — Each Telegram update handled once: Implementation details

| | |
|---|---|
| Roadmap stage | Stage 2 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/idempotent-updates` |
| Pull request | [#10](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/10) |
| Status | Merged |

Commit: `d94777b` (single commit); merged in `f282bd4`.

## Features implemented

### Update claim guard
`bot/handlers/dedupe.py` registers a `TypeHandler(Update, claim_update)` in group −2, registered
first in `bot/handlers/__init__.py`. `claim_update` calls `Database.claim_update(bot_id, update_id)`,
an `INSERT … ON CONFLICT DO NOTHING RETURNING update_id`. If no row is returned, the update was
already claimed and the handler raises `ApplicationHandlerStop`, so no other handler runs.

### Fail open
If the claim raises (e.g. database unreachable), a warning is logged
("Could not record update %s; handling it anyway") and the update is handled normally.

### Pruning
`Database.prune_processed_updates(older_than_hours)` deletes records older than
`RETENTION_HOURS = 48`. It runs at start-up (`_post_init` in `bot/app.py`) and every
`PRUNE_EVERY = 1000` claims (counted in `bot_data["update_claims"]`). Pruning errors are logged and
ignored.

## Architecture and design decisions

- **At most once, not exactly once.** The claim is taken before handling. If a crash cuts handling
  short, the resent copy is skipped and the user sends the message again. PR #10: "This was chosen
  over replaying half-done work, such as a second draft line or a second broadcast."
- **Fail open** rather than dropping every update while PostgreSQL is unavailable (code comment in
  `claim_update`).
- **48-hour retention:** Telegram keeps unconfirmed updates for at most 24 hours (PR #10 body).
- **`bot_id` in the key:** a new bot token starts its own update numbering.
- **Guard in the database, not in memory:** so a copy delivered to a second process during a deploy
  is also dropped.
- Other alternatives considered: **Not documented.**

## Significant code changes

8 files, +292 / −7 (GitHub and `git show --stat d94777b`):

| File | Change |
|---|---|
| `bot/handlers/dedupe.py` | New: `claim_update`, `prune`, `register`. |
| `bot/db.py` | `processed_updates` table and index; `claim_update`, `prune_processed_updates`. |
| `bot/handlers/__init__.py` | Registers the dedupe handler before all others. |
| `bot/app.py` | Prunes old records at start-up. |
| `tests/conftest.py` | Shared update-ID counter across harnesses; `Harness.deliver` and `last_update` to resend an update verbatim. |
| `tests/test_updates.py` | New, 12 tests. |
| `docs/DOCUMENTATION.md` | New "Stage 2" section. |
| `README.md` | One sentence on update de-duplication. |

## Schema / configuration changes

- `processed_updates (bot_id BIGINT, update_id BIGINT, received_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (bot_id, update_id))`, index `idx_processed_updates_received (received_at)`.
- Created with `CREATE TABLE IF NOT EXISTS`; existing data is not touched. No new settings
  (retention and prune interval are module constants).
- Compatibility with the previous version running at the same time: the old version does not write
  claims, so during the first deploy of this change a copy handled by the old process is not
  recognised by the new one. This follows from the design; it is not stated in the PR.
