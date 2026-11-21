# Phase 02 — Each Telegram update handled once: Overview

| | |
|---|---|
| Roadmap stage | Stage 2 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/idempotent-updates` |
| Pull request | [#10](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/10) |
| Status | Merged (2026-11-19) |

## Objective

Telegram resends an update the bot did not confirm: after a crash or redeploy right after the bot
fetched a batch, or when two bot versions poll at the same time during a Railway deploy. The copy
was handled again, "so an answer could be saved twice or the same upload sent to the AI twice"
(PR #10 body, commit `d94777b`). This phase makes the bot handle each update at most once.

## Scope

**In:**
- New `processed_updates` table and a guard handler (`bot/handlers/dedupe.py`) that runs before
  every other handler and drops copies, in this process or another one.
- Fail-open behaviour when the claim cannot be recorded.
- Pruning of old records.
- Test-harness change so harnesses standing for a restarted bot share one update-ID counter.

**Out:**
- Distinct updates that mean the same thing (e.g. two taps on a button) are different update IDs and
  are not merged here. Submit double taps are already handled by `Database.submit_draft` (Phase 01);
  the "Send to all users" double tap is listed in ROADMAP Stage 3.
- Replaying an update whose handling was cut short by a crash (at-most-once, by design).

## Expected outcome

From ROADMAP.md Stage 2: "The first handler now claims each update's ID in `processed_updates`;
copies are dropped before any other handler runs, in any process."

## Builds on

- Phase 01: `Database.submit_draft` remains the guard against double-tap submits; the restart
  harness (`make_harness`) used in `tests/test_sessions.py` is extended here.
