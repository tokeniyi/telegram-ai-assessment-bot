# Phase 06 — Command guidance: Final summary

| | |
|---|---|
| Roadmap stage | Stage 6 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stage-6-command-guidance` (stacked on `claude/lucid-brahmagupta-dri4yd`, PR #14) |
| Pull request | [#15](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/15) (base `main`; merge #14 first) |
| Status | In review |

## Accomplished

- A command registry (`bot/commands.py`): role, arguments and their form, accepted states,
  examples, invocations, feature switches, button safety. Menus, `/help`, "did you mean", buttons
  and every suggestion come from it. It is tested against the handlers that are really registered.
- Replies that say what kind of problem it is: invalid input, permission, state, temporary outage,
  or already retried automatically (no command suggested). Each suggests a fix only when one
  exists.
- Suggestions with real ids from the database, as tap-to-copy code, and as buttons for safe
  commands. Buttons run the real handler with its own checks.
- Applied across admin and student commands, the publish report, results, alerts, `/explain`,
  question generation, the chat model's context and the error reply.
- `/regrade` suggested only for failed grading. "No action needed" for grading in progress or
  interrupted. Concurrent retries never grade a submission twice (tested).
- 27 new test cases, 23 mutation checks. Full suite: 265 passed, 2 skipped.

## Changes and decisions

- **Add every new command to the registry** (rule added to `CLAUDE.md`). `tests/test_guidance.py`
  fails if the registry and the registered handlers disagree.
- **Suggest commands only through `commands.Suggestion`,** with values from the database
  (`bot/guidance.py`). Never write a command with an example id into a reply.
- **`button=True` only for read-only commands, or ones the database makes safe to repeat.**
  Destructive commands (`/close`) stay text.
- **`/regrade` takes an assessment number;** it may still take over abandoned grading (Phase 03),
  but the bot never suggests it for that.
- **Roadmap:** command guidance became Stage 6. Assessment features moved to Stage 7, and scale and
  limits to Stage 8. References in the Phase 03–05 docs were updated.

## Outstanding issues

- **Not verified on real Telegram clients:** tap-to-copy code, and buttons during a deploy (the
  previous version does not answer `cmd:` buttons, so a tap during the overlap shows a spinner
  until tapped again).
- **No `/resend` for failed assessment messages** (Phase 05); `/status` shows them but cannot offer a
  fix, so it suggests none.
- **Failed grading is not retried automatically** when the AI comes back; the ✅ message offers retry
  buttons.
- **Commands in group chats are ignored** without a reply, by decision (see
  [implementation.md](implementation.md)).

## Final status

Implemented and tested; PR #15 open for review (after PR #14).

## Pull request

[#15](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/15)
