# Phase documentation template

Copy this folder layout for every new phase: `docs/implementations/phase-NN/` with the five files
below. Keep each file short; link to code (`bot/db.py`) and tests instead of pasting them. Write
only what happened. Anything that cannot be confirmed from commits, PRs or test runs is marked
**Not documented** or **Unable to verify**.

Every file starts with the same header:

```markdown
# Phase NN — <title>: <file topic>

| | |
|---|---|
| Roadmap stage | Stage NN in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `<branch-name>` |
| Pull request | [#NN](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/NN) (or "not opened yet") |
| Status | In progress / In review / Merged |
```

---

## `overview.md` — Phase overview

- **Objective:** the problem this phase solves, in one or two sentences.
- **Scope:** what is in, and what is explicitly out (and where it goes instead).
- **Expected outcome:** the "done when" condition, ideally copied from the roadmap.
- **Builds on:** findings, open issues and decisions from earlier phases that this phase uses.

## `implementation.md` — Implementation details

- **Features implemented:** one short section per feature.
- **Architecture and design decisions:** what was chosen, the alternatives, and why.
- **Significant code changes:** a table of files and what changed in each.
- **Schema / configuration changes:** new tables, columns, settings; whether they are backwards
  compatible with the previous version running at the same time.

## `issues-and-fixes.md` — Challenges, debugging and fixes

One entry per problem:

```markdown
### <short name>
- **Symptom:** what was observed (failing test, log line, wrong behaviour).
- **Root cause:** what was actually wrong.
- **Investigation:** how it was found.
- **Fix:** what changed (commit or file).
```

If nothing went wrong, say so; do not invent problems.

## `testing.md` — Testing and validation

- **Methodology:** what kinds of tests (unit, database, end-to-end against the fake Telegram API,
  concurrency, restart simulation) and what each one proves about behaviour.
- **Tests added:** file and test name, one line each on what behaviour it checks.
- **Results:** the exact command and the final summary line (e.g. `170 passed, 2 skipped`).
- **Outstanding failures:** none, or each one with the reason.

## `summary.md` — Final summary

- **Accomplished:** a short list.
- **Changes and decisions:** the decisions a later phase must know about, with reasons.
- **Outstanding issues:** known bugs, technical debt, incomplete work, recommendations for the next
  phase.
- **Final status:** e.g. "Implemented and tested; PR #NN open for review".
- **Pull request:** link.
