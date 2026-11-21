# Implementation history

One folder per implementation phase. Each phase matches a stage in [ROADMAP.md](../../ROADMAP.md)
and has its own branch and pull request. New phases follow [TEMPLATE.md](TEMPLATE.md); the rules
are in [CLAUDE.md](../../CLAUDE.md).

| Phase | Topic | Branch | PR | Status | Docs |
|---|---|---|---|---|---|
| 01 | Saved answers survive restarts | `claude/hopeful-mccarthy-qftfmw` | [#8](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/8), [#9](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/9) | Merged | [overview](phase-01/overview.md) · [implementation](phase-01/implementation.md) · [issues](phase-01/issues-and-fixes.md) · [testing](phase-01/testing.md) · [summary](phase-01/summary.md) |
| 02 | Each Telegram update handled once | `claude/idempotent-updates` | [#10](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/10) | Merged | [overview](phase-02/overview.md) · [implementation](phase-02/implementation.md) · [issues](phase-02/issues-and-fixes.md) · [testing](phase-02/testing.md) · [summary](phase-02/summary.md) |
| 03 | Work that survives a restart | `claude/lucid-brahmagupta-dri4yd` | [#12](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/12) | In review | [overview](phase-03/overview.md) · [implementation](phase-03/implementation.md) · [issues](phase-03/issues-and-fixes.md) · [testing](phase-03/testing.md) · [summary](phase-03/summary.md) |

## Before the staged roadmap

PRs #1 to #7 (core bot, OCR and `/explain`, conversational chat, PostgreSQL, Docker/Railway, CI/CD
check, message wording) were merged before the roadmap split work into stages. They are described
in [docs/DOCUMENTATION.md](../DOCUMENTATION.md) and are not repeated here as phases.
