# Phase 06 — Command guidance: Implementation details

| | |
|---|---|
| Roadmap stage | Stage 6 in [ROADMAP.md](../../../ROADMAP.md) |
| Branch | `claude/stage-6-command-guidance` (stacked on `claude/lucid-brahmagupta-dri4yd`, PR #14) |
| Pull request | [#15](https://github.com/tokeniyi/telegram-ai-assessment-bot/pull/15) (base `main`; merge #14 first) |
| Status | In review |

## Features implemented

### Command registry (`bot/commands.py`)
`COMMANDS` holds one `Command` per command:

- `role` (`user` or `admin`) and `feature`, a setting that must be on for non-admins
  (`allow_explain`).
- `args` (`Arg`): name, kind (`assessment` or `text`), whether it is required, an example, and the
  assessment states it accepts (`/close`: published; `/results`, `/regrade`: published or closed).
- `summary`, `example`, `invocations` (`command`, `reply`, `caption`), `menu`, `button`, `aliases`.

Functions: `get` (aliases, `@bot` suffix), `available` (what this user may use), `closest`
(difflib, only among available commands), `menu` (Telegram menus), `code` (tap-to-copy).

`Suggestion(name, args, why, button)` validates itself against the registry when it is made. An
unknown command, missing or extra arguments, an argument of the wrong form, or a button on a
command not marked `button=True` all raise `ValueError`. A wrong suggestion is a bug that fails
loudly in tests instead of reaching a user.

### Guidance replies (`bot/guidance.py`)
- `render(settings, user_id, text, suggestions, kind)` builds the HTML reply: an icon for the kind
  (✏️ input, 🔒 permission, ℹ️ state, ⏳ temporary, 🔁 automatic), the text, one `👉 why: /command`
  line per suggestion, and up to 3 buttons. It drops, and logs, any suggestion the user may not
  run.
- Values come from the database: `regrade_suggestions` (assessments with failed grading),
  `assessment_choices` (ids a command can act on now), `grading_lines` (failed / being graded /
  interrupted), `student_next_steps` (assessments still to answer).
- `commands_for_ai`: the user's commands, given to the chat model. Its prompt now tells it to name
  only those.

### Unknown commands and buttons (`bot/handlers/guide.py`)
Registered last.
- `unknown_command` answers any command no handler took: "did you mean" with the closest allowed
  commands. A command that needs an assessment is shown with a real id, or as its usage form with
  "nothing needs it right now".
- `on_command_button` handles `cmd:<name>[:<n>]`. It refuses commands not marked `button`, buttons
  outside private chats, and users without the role. Then it calls the callback of the
  CommandHandler registered for that command, with `context.args` set. Typed commands and buttons
  therefore share every check.

### Where guidance was applied

| Workflow | Before | Now |
|---|---|---|
| Unknown / mistyped command | silence | "did you mean", or `/help` |
| Admin command by a student | "administrators only" | the same, plus `/help`; nothing admin-only |
| `/close`, `/regrade`, `/results` argument missing, not a number, unknown id | fixed text, made-up example | the reason, plus ids the command can act on now |
| `/close` on a draft or a closed assessment, `/results`/`/regrade` on a draft | "is draft, not open" | why, and what to do (publish; see results) |
| `/close` done | confirmation | plus 📊 Results |
| `/assessments` | list | plus retry for failed grading, results of the latest answered one |
| `/results` with ungraded submissions | "run /regrade" for all | failed (with 🔁 retry), being graded, interrupted: "no action needed" |
| `/regrade` | always started | in progress only: "no action needed"; none submitted, all graded: says so; stopped by the AI: why (outage or bad reply), plus a retry button |
| `/status` | regrade hint text | retry suggestions and buttons |
| `/generate` misuse, too little text | usage text | the same, with the registry example and the character count |
| Generation or regeneration fails | the error | outage (try later, `/status`) told apart from an unusable reply |
| Publish report | "Use /results N" | tap-to-copy `/results N` and a button; nothing to follow when sent to nobody |
| Grading-failure notice, AI back message | `/regrade N` text | the same, plus retry buttons; the 🚨 alert suggests `/status` only |
| Student result | result | plus "N assessments still to answer" only if there are |
| Student: submit or open a closed or already-submitted assessment | one message for both | which of the two, plus `/pending`/`/myresults` only if useful |
| `/pending` empty, `/myresults` | text | `/myresults` if they have results; failed vs in-progress grading told apart |
| `/explain` fails | "try again later" | outage (wait) told apart from an unusable reply (ask differently) |
| Unhandled error | "try again" | plus `/help` |
| Menus, `/help` command lists | hand-written | generated from the registry |

Left unchanged on purpose: `/cancel` with nothing to discard (nothing to do), the extraction and
OCR error texts (they already say what to send instead), and the chat "AI unavailable" reply
(already suggests `/pending`).

## Architecture and design decisions

- **Buttons reuse the registered handler** instead of re-implementing the commands. A button can do
  nothing the typed command could not, and the checks cannot diverge.
- **Only safe commands get buttons.** They are read-only (`/results`, `/status`, `/pending`, …) or
  protected by the database (`/regrade`: each submission is claimed by `claim_for_regrade`).
  `/close` is irreversible and is shown as text only.
- **`/regrade` keeps assessment numbers.** Submission ids are never shown to admins, and one retry
  per assessment matches how failures are reported.
- **`/regrade` may still take over abandoned grading** (Phase 03 behaviour, guarded by a test). The
  bot never *suggests* it for interrupted grading; it says the background work retries it.
- **No invented values.** Examples in replies use ids from the database; when there are none, the
  usage form is shown with the reason.
- **Two independent checks for permission:** `render` drops disallowed suggestions, and every
  handler (and the button dispatcher) checks the role again.
- **Group chats stay silent.** Answering commands in groups would add noise where other bots run;
  the registry records private chats as the only context.

## Significant code changes

| File | Change |
|---|---|
| `bot/commands.py` (new) | Registry, `Suggestion`, menus. |
| `bot/guidance.py` (new) | `render`/`reply`, kinds, suggestions from the database. |
| `bot/handlers/guide.py` (new) | Unknown-command reply, command buttons. |
| `bot/handlers/admin.py` | Argument checks per command, `/regrade` states, `/results` grading lines, `/close`, `/assessments`, `/status` buttons, AI failure kinds, permission reply. |
| `bot/handlers/student.py` | Closed vs already-submitted; `/pending`, `/myresults` next steps. |
| `bot/handlers/work.py` | Publish report (HTML, button); result footer with the student's next step. |
| `bot/alerts.py` | HTML notices with suggestions and retry buttons; `regrade_hint` replaced by `guidance.regrade_suggestions`. |
| `bot/handlers/common.py`, `bot/app.py` | Help lists and menus from the registry; first name escaped in `/start`; error reply mentions `/help`. |
| `bot/handlers/explain.py`, `bot/handlers/chat.py`, `bot/ai/service.py` | Outage vs bad reply; the chat model gets the user's commands. |
| `bot/db.py` | `grading_counts`. |
| `tests/test_guidance.py` (new) | Stage 6 tests. |
| `tests/test_deploy.py`, `tests/test_recovery.py`, `tests/test_visibility.py` | Assertions updated for the new wording (see [issues-and-fixes.md](issues-and-fixes.md)). |
| `ROADMAP.md`, phase 03–05 docs | Stage 6 added; later stages renumbered (6 → 7, 7 → 8). |
| `README.md`, `CLAUDE.md` | Guidance described; rule for adding commands. |

## Schema and configuration changes

None. Buttons sent by this version (`cmd:…`) reach an older version during a deploy as unknown
callback data, which that version leaves unanswered; the user can tap again once the deploy is
over. **Not tested** against a real deploy.
