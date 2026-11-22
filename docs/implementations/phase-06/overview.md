# Phase 06 — Command guidance: Phase overview

| | |
|---|---|
| Roadmap stage | Stage 6 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stage-6-command-guidance` (stacked on `claude/lucid-brahmagupta-dri4yd`, PR #14) |
| Pull request | not opened yet |
| Status | In progress |

## Objective

Users should not have to memorise commands. When a command fails, the bot explains what went
wrong in plain words, says whether it can be fixed, and suggests the command that fixes it, with
real arguments. When an operation succeeds, it suggests the next step only if there is one.
Suggestions are always valid, allowed for the user, and consistent with the bot's state.

Requested by the user on 2026-11-22, after asking whether a grading failure suggests `/regrade`.

## What existed before this phase

From a review of every handler before any change:

- **Grading failures already suggested `/regrade <assessment>`** to admins (Phase 05 notices, batched),
  and `/status` listed failed grading. Students were told their answers were saved; they were
  never shown an admin command.
- **`/regrade` takes an assessment number,** not a submission id. Admins never see submission ids,
  so suggestions keep the assessment number (not `/regrade 123` for a submission).
- **Gaps:**
  - `/results` said "run /regrade" for any ungraded submission, including ones being graded or
    already retried automatically.
  - Unknown and mistyped commands got no answer.
  - Wrong arguments got fixed text with a made-up example (`/close 3`).
  - `/close` on a draft said only "is draft, not open".
  - A permission error gave no way forward.
  - Admin commands could appear only in admin chats, but nothing enforced that.
  - The command menus, `/help` and the chat model's list of commands were three hand-written
    lists.

## Scope

- A command registry, the single source of truth for menus, `/help`, "did you mean",
  suggestions and command buttons. It records role, arguments and their form, accepted states,
  examples, invocations (reply, caption) and feature switches.
- Replies that classify the problem: invalid input, permission, state, temporary outage, or
  already retried automatically.
- Suggestions with ids from the database, shown as tap-to-copy code, and as buttons for safe
  commands. A button runs the real handler with its own permission and state checks.
- Applied across admin and student commands, the publish report, results, alerts, `/explain`, the
  chat model's context, and the error handler.
- Grading: `/regrade` is suggested only for failed grading; work in progress and interrupted grading
  say "no action needed"; concurrent retries never grade a submission twice.

Out of scope: answering commands in group chats (they are ignored, as before); a `/resend` command
for failed assessment messages (Phase 05 outstanding issue); retrying failed grading automatically.

## Expected outcome

Every suggested command exists, is allowed for the user, takes the arguments given, and can act
on the state it is suggested for, as checked by tests against the registered handlers.

## Builds on

- Phase 05: the failure notices, `/status` and `failed_grading_by_assessment` are reused. The notices
  now carry retry buttons. Phase 05's decision "no `/regrade` while the AI is down" is kept: the
  🚨 alert suggests `/status`, not a retry that cannot work.
- Phase 03: `claim_for_regrade` (a lease per submission) is what makes a retry button safe to tap
  twice. Its decision that `/regrade` may take over abandoned grading is kept.
- Phase 02: each button press is an update, deduplicated like any other.
