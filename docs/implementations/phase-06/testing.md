# Phase 06 — Command guidance: Testing and validation

| | |
|---|---|
| Roadmap stage | Stage 6 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stage-6-command-guidance` (stacked on `claude/lucid-brahmagupta-dri4yd`, PR #14) |
| Pull request | [#15](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/15) (base `main`; merge #14 first) |
| Status | Merged |

## Methodology

- **The registry against the real bot.**
  - The registered `CommandHandler`s are compared with the registry, aliases included.
  - Every command is typed by a student and by an admin. Each reaches its own handler, never the
    unknown-command reply, and admin commands refuse students.
  - Every command is sent in a group chat and gets no answer.
  - Every button command's callback data matches the registered button pattern and maps to that
    command's real handler.
  - The reply and caption invocations are exercised, since the bot has no conversation states.
- **Through the bot.** Students and admins drive the fake Telegram API and AI provider. Assertions
  check the reply's kind icon, the suggested commands with their ids, and the buttons.
- **Permissions.** A student runs many flows (failed grading, closed assessment, typos close to
  admin commands, `/help`, `/regrade`). No admin command appears in any text or button they
  receive. Forged `cmd:regrade` and `cmd:close` buttons are refused.
- **Concurrency.** Two taps on one retry button and a `/regrade` from another process run at the
  same time, with a slow AI. Each submission is graded once, and each student gets one result.
- **Mutation checks.** 23 safeguards were removed one at a time (script kept out of the repository).
  After the fixes in [issues-and-fixes.md](issues-and-fixes.md), item 7, all 23 make a test fail.
  - **Suggestions:** `render` dropping disallowed suggestions; the three `Suggestion` validations;
    typos matched only among allowed commands; no invented id in "did you mean"; examples only with
    ids in an accepted state.
  - **Buttons:** the role check, button-safe commands only, private chats only.
  - **Registry and roles:** the feature switch in `allowed()`; menus hiding admin commands; the
    permission reply suggesting nothing admin-only.
  - **Grading:** the argument state check; `/results` suggesting `/regrade` only for failed grading;
    "no action needed" for grading in progress; the claim before each regrade; outage vs bad reply.
  - **Next steps:** the just-graded assessment left out of "still to answer"; `/myresults` only with
    results; no results suggestion when an assessment was sent to nobody.
  - **Alerts and chat:** no retry suggested while the AI is down; the chat model told only the
    user's own commands.

## Tests added (`tests/test_guidance.py`, 26 tests, 27 cases)

| Area | Tests |
|---|---|
| Registry vs handlers | `test_the_registry_lists_exactly_the_registered_commands`, `test_every_command_answers_with_its_role_enforced`, `test_commands_answer_only_in_private_chats`, `test_feature_switches_hide_a_command_from_those_who_cannot_use_it`, `test_menus_and_help_come_from_the_registry`, `test_every_button_command_is_routed_to_its_real_handler`, `test_reply_and_caption_invocations_in_the_registry_really_work`, `test_suggestions_are_checked_against_the_registry` |
| Typos | `test_a_mistyped_command_suggests_the_closest_one_the_user_may_use` |
| Arguments | `test_assessment_arguments_are_checked_and_examples_use_real_ids`, `test_generate_explains_how_it_is_used` |
| Permissions | `test_students_are_never_shown_admin_commands`, `test_render_drops_a_suggestion_the_user_may_not_run`, `test_buttons_check_permission_and_only_run_safe_commands`, `test_a_command_button_does_nothing_outside_a_private_chat` |
| Next steps | `test_next_steps_are_suggested_only_when_there_is_one`, `test_publishing_to_nobody_suggests_nothing_to_follow`, `test_myresults_tells_failed_from_in_progress_grading`, `test_with_resubmission_the_graded_assessment_is_not_offered_as_still_to_answer` |
| Grading retries | `test_results_suggest_regrade_only_for_failed_grading`, `test_regrade_reports_what_needs_no_action`, `test_a_double_tap_on_retry_grades_each_submission_once`, `test_a_regrade_stopped_by_the_ai_says_why_and_offers_a_retry` (2 cases) |
| Failures and alerts | `test_generation_failures_tell_an_outage_from_a_bad_reply`, `test_the_ai_down_alert_does_not_suggest_a_retry_that_cannot_work`, `test_the_chat_model_is_told_only_the_commands_the_user_may_use` |

## Results

```
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bot_test python -m pytest -q
265 passed, 2 skipped in 119.52s
```

Before this phase: 238 passed, 2 skipped (this branch's base, PR #14). Run locally on Python 3.13
with PostgreSQL 16; CI runs Python 3.12 (result recorded on the PR).

## Not tested

- On real Telegram clients: how tap-to-copy code and the buttons look and behave. **Not done**
  (no Telegram access from the development environment).
- A `cmd:` button reaching the previous version during a deploy. **Not done**.

## Outstanding failures

None.
