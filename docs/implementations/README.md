# Implementation history

One folder per implementation phase. Each phase matches a stage in [ROADMAP.md](../../ROADMAP.md)
and has its own branch and pull request. New phases follow [TEMPLATE.md](TEMPLATE.md); the rules
are in [CLAUDE.md](../../CLAUDE.md).

| Phase | Topic | Branch | PR | Status | Docs |
|---|---|---|---|---|---|
| 01 | Saved answers survive restarts | `claude/hopeful-mccarthy-qftfmw` | [#8](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/8), [#9](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/9) | Merged | [overview](phase-01/overview.md) · [implementation](phase-01/implementation.md) · [issues](phase-01/issues-and-fixes.md) · [testing](phase-01/testing.md) · [summary](phase-01/summary.md) |
| 02 | Each Telegram update handled once | `claude/idempotent-updates` | [#10](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/10) | Merged | [overview](phase-02/overview.md) · [implementation](phase-02/implementation.md) · [issues](phase-02/issues-and-fixes.md) · [testing](phase-02/testing.md) · [summary](phase-02/summary.md) |
| 03 | Work that survives a restart | `claude/lucid-brahmagupta-dri4yd` | [#12](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/12) | Merged | [overview](phase-03/overview.md) · [implementation](phase-03/implementation.md) · [issues](phase-03/issues-and-fixes.md) · [testing](phase-03/testing.md) · [summary](phase-03/summary.md) |
| 04 | Safer deploys | `claude/lucid-brahmagupta-dri4yd` | [#13](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/13) | Merged | [overview](phase-04/overview.md) · [implementation](phase-04/implementation.md) · [issues](phase-04/issues-and-fixes.md) · [testing](phase-04/testing.md) · [summary](phase-04/summary.md) |
| 05 | Visibility for the admin | `claude/lucid-brahmagupta-dri4yd` | [#14](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/14) | Merged | [overview](phase-05/overview.md) · [implementation](phase-05/implementation.md) · [issues](phase-05/issues-and-fixes.md) · [testing](phase-05/testing.md) · [summary](phase-05/summary.md) |
| 06 | Command guidance | `claude/stage-6-command-guidance` | [#15](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/15) | Merged | [overview](phase-06/overview.md) · [implementation](phase-06/implementation.md) · [issues](phase-06/issues-and-fixes.md) · [testing](phase-06/testing.md) · [summary](phase-06/summary.md) |
| 07 | Assessment features | `claude/stoic-babbage-iym3c3` | [#16](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/16) | Merged | [overview](phase-07/overview.md) · [implementation](phase-07/implementation.md) · [issues](phase-07/issues-and-fixes.md) · [testing](phase-07/testing.md) · [summary](phase-07/summary.md) |
| 08 | Recovery without the admin | `claude/stoic-babbage-iym3c3` | [#17](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/17) | Merged | [overview](phase-08/overview.md) · [implementation](phase-08/implementation.md) · [issues](phase-08/issues-and-fixes.md) · [testing](phase-08/testing.md) · [summary](phase-08/summary.md) |

## Before the staged roadmap

PRs #1 to #7 (core bot, OCR and `/explain`, conversational chat, PostgreSQL, Docker/Railway, CI/CD
check, message wording) were merged before the roadmap split work into stages. They are described
in [docs/DOCUMENTATION.md](../DOCUMENTATION.md) and are not repeated here as phases.
