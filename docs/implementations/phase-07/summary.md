# Phase 07 — Assessment features: Final summary

| | |
|---|---|
| Roadmap stage | Stage 7 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stoic-babbage-iym3c3` |
| Pull request | not opened yet |
| Status | In progress |

## Accomplished

- `/deadline <id> [when]`: set, show or remove a deadline, typed in `TIMEZONE`.
- Answers stop at the deadline itself. The assessment then closes by itself, and its publisher is
  told once how many submitted.
- One reminder, `REMINDER_HOURS` before the deadline, to each student who has not submitted. It has
  a Submit or Answer now button, is sent once across processes, and is sent again only if the
  deadline moves.
- `/settings <id>` and a ⚙️ button under the draft preview: reference answers shown or hidden, and
  resubmission allowed or not, per assessment, with the bot-wide settings as defaults.
- 43 new tests; full suite 308 passed, 2 skipped. 15 of 15 removed safeguards are caught by a test.
- The roadmap, the phase index and the Phase 05/06 headers now record Stages 5 and 6 as merged
  (separate commit).

## Changes and decisions

- **"Open" is one SQL condition, `Database.OPEN`:** published, and no deadline or one still ahead.
  New code that asks whether an assessment takes answers or messages must use it, not `status =
  'published'`.
- **Per-assessment options are nullable columns; NULL = the bot-wide setting.** Use
  `Settings.shows_answers(assessment)` and `Settings.allows_resubmission(assessment)` (or
  `COALESCE(a.<column>, default)` in SQL), never the settings directly. A new option should follow
  the same pattern and appear in `/settings`.
- **Reminders are sent at most once** (marked sent before sending). A stop at that moment loses one
  reminder rather than repeating it.
- **Moving a deadline resets its reminders.** Students who still have not submitted are reminded
  before the new deadline.
- **`/deadline` is typed only; `/settings` may be a button.** Its option buttons carry explicit
  values, so repeated taps are harmless.
- **Assessment messages carry the deadline.** That is why no reminder is queued for an assessment
  published inside the reminder window.

## Outstanding issues

- **Saved but unsubmitted answers are discarded at the deadline**, not submitted. An auto-submit
  option is a natural follow-up.
- **`/status` does not show reminders or upcoming deadlines.** The counts are in `reminders` (see
  `Database.reminder_counts`).
- **One reminder time for the whole bot**; a per-assessment `REMINDER_HOURS` would be another
  nullable column.
- **During a deploy, the previous version ignores deadlines and options** until it stops. It can
  accept an answer, or send a queued message, after a deadline (at most until this version closes
  the assessment, within 30 s). See [implementation.md](implementation.md).
- **Not verified on real Telegram clients or on Railway.**
- **Still open from Phases 05/06:** no `/resend`, and no automatic regrading. They are separate
  work, for their own branch.

## Final status

Implemented and tested; PR not opened yet.

## Pull request

Not opened yet.
