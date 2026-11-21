# Phase 02 — Each Telegram update handled once: Testing and validation

| | |
|---|---|
| Roadmap stage | Stage 2 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/idempotent-updates` |
| Pull request | [#10](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/10) |
| Status | Merged |

## Methodology

Tests in [`tests/test_updates.py`](../../../tests/test_updates.py) run against a real PostgreSQL
database with the fake Telegram API and fake AI from `tests/conftest.py`.

- **Resent update (end-to-end):** `Harness.deliver(h.last_update)` hands the bot the exact same
  update again, as Telegram does after an unconfirmed fetch.
- **Two processes:** two harnesses on one database; the second receives the first one's update, both
  sequentially and in a concurrent race.
- **Fail-open and pruning:** `monkeypatch` makes the claim fail, or lowers the prune interval.
- **Storage layer:** `Database.claim_update` and `prune_processed_updates` directly, including
  threaded concurrent claims.

## Tests added (12)

| Test | Checks |
|---|---|
| `test_a_resent_answer_is_saved_once` | A resent answer gets no second reply and no second draft line. |
| `test_a_resent_publish_button_sends_the_assessment_once` | A resent publish tap does not broadcast twice. |
| `test_a_resent_upload_is_not_turned_into_a_second_draft` | A resent upload makes no second AI call or assessment. |
| `test_two_processes_receiving_the_same_update_handle_it_once` | A copy delivered to another process is dropped. |
| `test_two_processes_racing_for_one_update` | Concurrent delivery to two processes is handled once. |
| `test_new_updates_after_a_restart_are_handled` | New updates after a restart are not dropped. |
| `test_updates_are_still_handled_when_they_cannot_be_recorded` | Fail-open when the claim raises. |
| `test_old_records_are_pruned_every_so_often` | Pruning runs every `PRUNE_EVERY` claims. |
| `test_an_update_can_be_claimed_once_per_bot` | One claim per `(bot_id, update_id)`; other bots independent. |
| `test_concurrent_claims_have_one_winner` | Threaded concurrent claims: exactly one succeeds. |
| `test_pruning_forgets_only_old_records` | Only records older than the cut-off are deleted. |
| `test_schema_with_update_records_can_be_created_twice` | The schema is idempotent. |

`tests/conftest.py` was also changed (shared update-ID counter) so existing restart tests keep
working with the guard in place.

## Results

At merge time (GitHub Actions `test` job on head `d94777b`, `python -m pytest -q`, PostgreSQL 16,
Python 3.12): `165 passed, 2 skipped in 48.91s` (success). GitGuardian Security Checks also passed.
That is 12 more than Phase 01's 153, matching the 12 new tests.

PR #10 also states: "With the guard turned off, 5 of the new tests fail." This was not re-run;
**Unable to verify** independently.

Re-run on 2026-11-21 against current code (branch `claude/lucid-brahmagupta-dri4yd`, commit
`ee49980`), not the result at merge time:

```
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bot_test \
  python -m pytest tests/test_sessions.py tests/test_updates.py -q
33 passed in 5.11s
```

## Outstanding failures

None at merge time. Which 2 tests are skipped in the full suite: **Not documented.**
