"""Free-form chat: every typed message is either conversation, part of an answer, or a request
for review, and the AI decides which.

Without this, each text message to a user with an open assessment was stored as an answer, so
"hey" or "what does osmosis mean?" ended up in the graded submission.

One AI call per message returns an intent and, for conversation, the reply:
  chat    -> the reply is sent, nothing is stored
  answer  -> the text is added to the user's draft, exactly as before
  review  -> the bot offers the Submit button (submitting is final, so the user confirms it)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from .. import deadlines, guidance
from ..ai import AIError
from ..utils import esc, num, send_blocks, services
from . import student

log = logging.getLogger(__name__)

PRIVATE = filters.ChatType.PRIVATE
MAX_MESSAGE_FOR_AI = 3_000   # characters of the message the AI sees; the whole text is still saved
MAX_HISTORY_ITEM = 800       # characters kept per remembered message
MAX_CONTEXT_CHARS = 6_000

# What an answer typically looks like: "1. B", "2) ...", "Q3 - ...", "a) ...".
ANSWER_LIKE = re.compile(r"^\s*(?:q(?:uestion)?\s*)?\d+\s*[.):\-–]|^\s*[A-Da-d]\s*[.)]\s", re.IGNORECASE | re.MULTILINE)
AI_DOWN = "❌ I could not reach my AI right now. Please try again in a moment, or use /pending to view assessments."


def looks_like_answer(text: str) -> bool:
    return bool(ANSWER_LIKE.search(text))


# ── what the AI is told ──────────────────────────────────────────────────
def build_context(context: ContextTypes.DEFAULT_TYPE, user_id: int, assessment: dict[str, Any] | None) -> str:
    """Facts the AI may use. Reference answers of an open assessment are never included."""
    svc = services(context)
    lines: list[str] = []
    if assessment:
        questions = svc.db.get_questions(assessment["id"])
        lines.append(f'Open assessment: "{assessment["title"]}". The student has not submitted it yet.')
        if assessment.get("deadline"):
            lines.append("Deadline: " + deadlines.describe(assessment["deadline"], svc.settings.tz))
        for q in questions:
            line = f"Q{q['position']} ({num(q['points'])} pt): {q['text']}"
            if q["options"]:
                line += " Options: " + "; ".join(f"{'ABCDEFGH'[i]}) {o}" for i, o in enumerate(q["options"]))
            lines.append(line)
        saved = len(student.draft_parts(context, user_id, assessment["id"]))
        lines.append(f"Answer messages the student has saved so far: {saved}.")
    else:
        lines.append("No assessment is open for this student right now.")

    # The only commands the model may mention: the ones this user can actually use (bot/commands.py).
    lines.append("Commands this user can use: " + guidance.commands_for_ai(svc.settings, user_id))
    if svc.settings.is_admin(user_id):
        lines.append("This user is an administrator. They create assessments by sending a PDF, DOCX or TXT "
                     "file or photos of notes (a caption steers the questions), or by replying /generate to "
                     "pasted text. Pasted text alone is not used as material.")
    graded = [s for s in svc.db.user_submissions(user_id, limit=5) if s["status"] == "graded"]
    if graded:
        last = graded[0]
        lines.append(f'Latest result: "{last["title"]}", {num(last["score"])}/{num(last["max_score"])} points.')
        questions = {q["position"]: q for q in svc.db.get_questions(last["assessment_id"])}
        show_answers = svc.settings.shows_answers(svc.db.get_assessment(last["assessment_id"]) or {})
        for item in json.loads(last["details"] or "[]"):
            q = questions.get(item["number"], {})
            line = (f"  Q{item['number']} {q.get('text', '')} | student answered: {item['student_answer'] or '(nothing)'} "
                    f"| {num(item['awarded'])}/{num(item['max'])} | feedback: {item['feedback']}")
            if show_answers and q:
                line += f" | reference answer: {q['answer']}"
            lines.append(line)
        if last["feedback"]:
            lines.append(f"  Overall feedback: {last['feedback']}")
    return "\n".join(lines)[:MAX_CONTEXT_CHARS]


def _history(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> list[tuple[str, str]]:
    return services(context).db.chat_history(user_id)


def _remember(context: ContextTypes.DEFAULT_TYPE, user_id: int, student_text: str, reply: str) -> None:
    """Store one exchange (the student's message and what the bot did) in the chat history."""
    svc = services(context)
    svc.db.append_chat_history(
        user_id, [("Student", student_text[:MAX_HISTORY_ITEM]), ("Assistant", reply[:MAX_HISTORY_ITEM])],
        keep=2 * svc.settings.chat_history_turns,
    )


# ── routing ──────────────────────────────────────────────────────────────
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text
    if not services(context).settings.allow_chat:
        await student.add_to_draft(update, context, text)  # chat is switched off: all text is an answer
        return
    # One message at a time per user: answers often arrive as a quick series of messages, and
    # each must be classified and saved in order. This lock orders them within this process;
    # the draft itself is changed in database transactions, which are safe across processes.
    async with context.user_data.setdefault("chat_lock", asyncio.Lock()):
        await _route(update, context, text)


async def _route(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    svc = services(context)
    message, user = update.effective_message, update.effective_user
    assessment = student.target_assessment(context, user.id)

    await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
    try:
        decision = await svc.ai.converse(
            text[:MAX_MESSAGE_FOR_AI], _history(context, user.id), build_context(context, user.id, assessment),
            can_answer=assessment is not None,
        )
    except AIError as exc:
        log.error("Chat classification failed: %s", exc)
        # Never lose an answer because the AI is down: keep what clearly is one.
        if assessment is not None and (student.draft_parts(context, user.id, assessment["id"]) or looks_like_answer(text)):
            await student.add_to_draft(update, context, text)
        else:
            await message.reply_text(AI_DOWN)
        return

    intent = decision["intent"]
    log.info("Chat intent for user %s: %s", user.id, intent)
    if intent == "answer":
        _remember(context, user.id, text, "(saved as an answer)")
        await student.add_to_draft(update, context, text)
    elif intent == "review":
        _remember(context, user.id, text, "(offered to submit the saved answers)")
        await _offer_review(update, context, assessment)
    else:
        _remember(context, user.id, text, decision["reply"])
        await send_blocks(context.bot, message.chat_id, [esc(decision["reply"])])


async def _offer_review(update: Update, context: ContextTypes.DEFAULT_TYPE, assessment: dict[str, Any]) -> None:
    message = update.effective_message
    parts = student.draft_parts(context, update.effective_user.id, assessment["id"])
    title = esc(assessment["title"])
    if not parts:
        await message.reply_text(
            f"I have no answers saved for “{assessment['title']}” yet. Send your answers here "
            "(typed, a file or a photo) and ask me to review them when you are done."
        )
        return
    await message.reply_text(
        f"✅ Ready to review. I have {len(parts)} message{'s' if len(parts) != 1 else ''} saved as your answers to "
        f"<b>{title}</b>. Tap <b>✅ Submit</b> to send them for grading.",
        parse_mode=ParseMode.HTML,
        reply_markup=student.draft_keyboard(assessment["id"]),
    )


def register(application: Application) -> None:
    application.add_handler(MessageHandler(PRIVATE & filters.TEXT & ~filters.COMMAND, on_text))
