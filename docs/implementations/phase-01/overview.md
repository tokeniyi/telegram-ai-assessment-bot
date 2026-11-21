# Phase 01 — Saved answers survive restarts: Overview

| | |
|---|---|
| Roadmap stage | Stage 1 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/hopeful-mccarthy-qftfmw` |
| Pull request | [#8](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/8) (follow-up: [#9](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/9), same branch) |
| Status | Merged (#8 on 2026-11-17, #9 on 2026-11-18) |

## Objective

A user's unfinished work (answers typed but not submitted, the assessment picked with `/pending`,
and chat history) was held in `context.user_data`, so every restart or Railway redeploy lost it
(commit `646850b`, PR #8 body). This phase moves that state to PostgreSQL.

## Scope

**In:**
- New tables `user_sessions` and `draft_answers`, created on start with `CREATE TABLE IF NOT EXISTS`.
- Draft, target assessment and chat history read and written through `bot/db.py` instead of memory.
- Buttons (Submit / Undo / Discard) sent by the previous process keep working.
- Exactly-once submit (`Database.submit_draft`).
- PR #9: `/cancel` success message aligned with the Discard button wording.

**Out (kept in memory on purpose):** the `grading`, `reading` and `explaining` flags and the
per-process chat lock. Per PR #8 they "only prevent duplicate slow work within one process and are
not correctness guards". Durable jobs (resuming grading and sending after a restart) are left for a
later stage; ROADMAP.md places this in Stage 3.

## Expected outcome

From ROADMAP.md Stage 1: answers typed so far, the assessment picked with `/pending` and chat
history are stored in PostgreSQL, "so a restart or redeploy loses nothing. Buttons sent by the
previous process keep working. Submit creates at most one submission, even on a double tap or with
two bot versions running."

## Builds on

- PR #4 (`86335fb`, "Replace SQLite storage with PostgreSQL"): the PostgreSQL storage layer these
  tables are added to.
- PR #7 (bot message wording) was merged into this branch before PR #8 merged (`74d9264`).
- No earlier phase documentation exists; this is the first documented phase.
