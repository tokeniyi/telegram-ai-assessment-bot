"""What to tell a user, and which command to suggest next (Stage 6).

Every reply that reports a problem says what kind of problem it is, because the right next step
depends on it:

    INPUT      ✏️  the command was typed wrongly: show the correct form with real values
    PERMISSION 🔒  the user may not do this: never suggest a command they cannot use
    STATE      ℹ️  not possible right now (closed, still a draft, nothing failed): say why
    TEMPORARY  ⏳  a service failed (the AI provider): nothing was lost, try again later
    AUTOMATIC  🔁  the bot is already retrying: no action is needed, so no command is suggested

Suggestions are `commands.Suggestion`s, checked against the command registry when made. The
values in them come from the database (the assessments that really have failed grading, the
ones that are really open), and `render` drops any suggestion the user is not allowed to run,
so a student is never shown an administrator's command.

A suggestion may also carry a button. Buttons run the same handler as the typed command
(bot/handlers/guide.py), with the same permission and state checks.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .commands import Suggestion
from .utils import esc

if TYPE_CHECKING:
    from .config import Settings
    from .db import Database

log = logging.getLogger(__name__)

INPUT, PERMISSION, STATE, TEMPORARY, AUTOMATIC = "input", "permission", "state", "temporary", "automatic"
ICONS = {INPUT: "✏️", PERMISSION: "🔒", STATE: "ℹ️", TEMPORARY: "⏳", AUTOMATIC: "🔁"}
MAX_BUTTONS = 3


def render(settings: Settings, user_id: int | None, text: str, suggestions: list[Suggestion] = (),
           kind: str | None = None) -> tuple[str, InlineKeyboardMarkup | None]:
    """The HTML message and its buttons. `text` is HTML (escape what users wrote)."""
    allowed = []
    for suggestion in suggestions:
        if suggestion.command.allowed(settings, user_id):
            allowed.append(suggestion)
        else:  # a bug in the caller: it must not even try
            log.warning("Not suggesting %s to user %s: not allowed", suggestion.text, user_id)
    lines = [f"{ICONS[kind]} {text}" if kind else text]
    lines += [f"👉 {s.html()}" for s in allowed]
    buttons = [[InlineKeyboardButton(s.button, callback_data=s.callback_data)]
               for s in allowed if s.button][:MAX_BUTTONS]
    return "\n".join(lines), InlineKeyboardMarkup(buttons) if buttons else None


async def reply(update, context, text: str, suggestions: list[Suggestion] = (), kind: str | None = None) -> None:
    """Answer the message (or the button) that started this update."""
    settings = context.application.bot_data["services"].settings
    body, markup = render(settings, update.effective_user.id, text, suggestions, kind)
    await update.effective_message.reply_text(body, parse_mode="HTML", reply_markup=markup,
                                              disable_web_page_preview=True)


# ── suggestions from the bot's actual state ──────────────────────────────
def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def regrade_suggestions(db: Database, limit: int = 3) -> list[Suggestion]:
    """`/regrade <id>` for the assessments that really have failed grading, newest first.
    Interrupted grading is not included: the background work retries it by itself."""
    return [Suggestion("regrade", (str(r["assessment_id"]),),
                       why=f"Retry grading {plural(r['n'], 'submission')} to #{r['assessment_id']}",
                       button=f"🔁 Retry grading #{r['assessment_id']}")
            for r in db.failed_grading_by_assessment()[:limit]]


def results_suggestion(assessment_id: int, why: str = "See the scores") -> Suggestion:
    return Suggestion("results", (str(assessment_id),), why=why, button=f"📊 Results #{assessment_id}")


def assessment_choices(db: Database, command: str, limit: int = 3) -> list[Suggestion]:
    """Ready-to-use examples of `/command <id>` with ids this command can act on right now."""
    if command == "regrade":
        return regrade_suggestions(db, limit)
    states = {"close": ("published",), "results": ("published", "closed"), "deadline": ("draft", "published"),
              "settings": ("draft", "published", "closed")}[command]
    rows = [a for a in db.list_assessments() if a["status"] in states][:limit]
    return [Suggestion(command, (str(a["id"]),), why=f"#{a['id']} {esc(a['title'])}") for a in rows]


def grading_lines(counts: dict[str, int]) -> list[str]:
    """How the grading of one assessment stands, for /results. Only failed grading needs the
    admin; work in progress and interrupted grading say so, without suggesting anything."""
    lines = []
    if counts["failed"]:
        lines.append(f"❌ {plural(counts['failed'], 'submission')} could not be graded.")
    if counts["grading_now"]:
        lines.append(f"⏳ {counts['grading_now']} being graded now. No action needed.")
    if counts["interrupted"]:
        lines.append(f"🔁 {counts['interrupted']} interrupted; graded again automatically. No action needed.")
    return lines


def student_next_steps(db: Database, settings: Settings, user_id: int, exclude: int | None = None) -> list[Suggestion]:
    """A student's next step, only when there is one: assessments still to answer."""
    open_ = [a for a in db.open_assessments_for(user_id, default_resubmission=settings.allow_resubmission)
             if a["id"] != exclude]
    if not open_:
        return []
    return [Suggestion("pending", why=f"{plural(len(open_), 'assessment')} still to answer",
                       button="📝 Open assessments")]


def has_results(db: Database, user_id: int) -> bool:
    return bool(db.user_submissions(user_id, limit=1))


def commands_for_ai(settings: Settings, user_id: int) -> str:
    """The commands this user may use, for the chat model's context, so that the commands it
    mentions are real and allowed."""
    from .commands import available
    return "; ".join(f"{c.usage} ({c.summary})" for c in available(settings, user_id) if c.menu)


def describe_state(assessment: dict[str, Any]) -> str:
    return {"draft": "is still a draft", "published": "is open", "closed": "is closed"}.get(
        assessment["status"], f"is {assessment['status']}")
