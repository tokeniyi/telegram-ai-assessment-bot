# CLAUDE.md

Guidance for Claude and other coding agents working in this repository.

## Project

A Telegram bot (python-telegram-bot 22) that turns study material into AI-generated assessments,
sends them to users, and grades their answers. State lives in PostgreSQL (`bot/db.py`).
Planned work is in [ROADMAP.md](ROADMAP.md); what was built in each phase is in
[docs/implementations/](docs/implementations/README.md).

- Run: `python main.py` (settings from `.env`, see `.env.example`).
- Test: `python -m pytest -q`. The tests need PostgreSQL; set `TEST_DATABASE_URL`
  (CI uses `postgresql://postgres:postgres@localhost:5432/bot_test`). Each test gets its own schema.
- Several bot processes can share one database (two versions run together during a Railway
  deploy). Anything that must happen once is decided by the database (a conditional `UPDATE …
  RETURNING`, a primary key, a lease), never by in-memory state.

## Development conventions (mandatory)

### 1. One branch per phase

- Every implementation phase (roadmap stage) is developed on its own branch, created before work
  starts, with a descriptive name (e.g. `claude/stage-3-durable-work`), or the branch the session
  is assigned.
- Keep the branch to that phase's scope. Unrelated fixes go in their own branch and PR.
- When implementation and testing are done, open a PR into `main` for review. Do not merge it
  yourself.

### 2. Documentation for every phase

Each phase has a folder `docs/implementations/phase-NN/` with five files, following
[docs/implementations/TEMPLATE.md](docs/implementations/TEMPLATE.md):

| File | Contents |
|---|---|
| `overview.md` | Objective, scope, expected outcome, what it builds on |
| `implementation.md` | Features, architecture and design decisions, significant code and schema changes |
| `issues-and-fixes.md` | Bugs and problems met, root cause, investigation, fix |
| `testing.md` | Methodology, tests added, exact results, outstanding failures |
| `summary.md` | What was accomplished, decisions, outstanding issues, final status, PR link |

- Every file names the phase's branch and PR.
- Update the documentation while developing, not only at the end.
- Commit the documentation on the phase branch so that it merges with the code in the same PR.
- Add the phase to the index in [docs/implementations/README.md](docs/implementations/README.md)
  and update its status in [ROADMAP.md](ROADMAP.md).
- Write only what happened. Never invent bugs, results or decisions; mark anything that cannot be
  verified as **Not documented** or **Unable to verify**.

### 3. Pull requests

- The PR description links to the phase's documentation folder.
- A phase is complete only when it is implemented, tested, documented, and its PR is reviewed and
  merged.

### 4. Continuity between phases

- Before starting a phase, read the `summary.md` of earlier phases (especially "Outstanding
  issues") and reference the relevant findings in the new phase's `overview.md`.
- Record decisions a later phase must respect in `summary.md` under "Changes and decisions".

### 5. Testing

- Test behaviour, not just that code runs: drive the bot through the fake Telegram API and fake AI
  provider in `tests/conftest.py`, simulate restarts with two harnesses on one database, and test
  races with concurrent calls.
- When a test guards against a bug, check that it fails when the fix is removed.
- Report the exact test command and result in the phase's `testing.md`.

### 6. Documentation quality

Keep documentation concise, professional and easy to navigate. Link to code and tests rather than
copying them. Avoid duplication across files, but keep important technical detail.
