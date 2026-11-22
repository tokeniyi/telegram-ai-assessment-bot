# Phase 07 — Assessment features: Phase overview

| | |
|---|---|
| Roadmap stage | Stage 7 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stoic-babbage-iym3c3` |
| Pull request | [#16](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/16) |
| Status | Merged |

## Objective

Give each assessment its own deadline, remind students before it, and let the admin decide per
assessment whether students see reference answers and may resubmit. Until now an assessment stayed
open until someone typed `/close`, nobody was reminded, and both options were one setting for the
whole bot (`SHOW_CORRECT_ANSWERS`, `ALLOW_RESUBMISSION`).

Started on 2026-11-22 at the user's request ("start stage 7").

## Scope

In:

- `/deadline <id> [when]`: set, show or remove the deadline of a draft or an open assessment.
- Answers stop at the deadline itself. The background work then closes the assessment, cancels
  messages not yet sent, and tells the publisher once how many submitted.
- One reminder per student, `REMINDER_HOURS` (24) before the deadline. It goes to students who were
  sent the assessment, can be reached, and have not submitted. Moving the deadline reminds them again.
- `/settings <id>` and a ⚙️ button under the draft preview show the deadline and both options.
  Buttons change them. A column left empty follows the bot-wide setting.
- The deadline is shown to students (assessment message, `/pending`, the chat model's context) and
  to admins (preview, `/assessments`).
- `TIMEZONE` for typing and showing times.

Out (left for later, see [summary.md](summary.md)):

- Submitting saved but unsubmitted answers automatically at the deadline.
- Reminders in `/status`.
- `/resend` and automatic regrading (Phase 05/06 outstanding issues). They belong to their own branch.

## Expected outcome

From the roadmap: deadlines with assessments closing automatically, reminders before the deadline
to students who have not submitted, and per-assessment settings for reference answers and
resubmission. Each of these must hold with two bot processes on one database.

## Builds on

- **Phase 03:** work is driven from database rows by `work.run_once` every 30 s. Exactly-once
  decisions are conditional `UPDATE … RETURNING` statements. Closing at the deadline and reminders
  reuse both. Reminders are an outbox like `deliveries`.
- **Phase 03 decision "no repeat when in doubt" for assessment messages:** reminders follow it. Each
  is marked sent before it is sent.
- **Phase 05:** log with `event(...)`. New events are `deadline.closed`, `reminder.*` and
  `assessment.option_changed`.
- **Phase 06:** every new command is in the registry (`bot/commands.py`). Suggestions use
  `commands.Suggestion` with real ids. `/deadline` changes state, so it is never a button.
  `/settings` only shows state, so it may be one.
- **Phase 06 outstanding issues** (`/resend`, automatic regrading, Telegram clients not verified)
  are not addressed here. They stay open.
