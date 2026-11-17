"""User side: collect answers, submit them, grade them, show results."""

from __future__ import annotations

import json
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters,
)

from ..ai import AIError
from ..utils import esc, num, percent, question_blocks, send_blocks, services, total_points
from . import explain
from .reading import echo_block, is_image, read_upload

log = logging.getLogger(__name__)

PRIVATE = filters.ChatType.PRIVATE
MAX_DRAFT_CHARS = 30_000


# ── which assessment is the user answering? ──────────────────────────────
def open_assessments(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> list[dict[str, Any]]:
    svc = services(context)
    return svc.db.open_assessments_for(user_id, include_submitted=svc.settings.allow_resubmission)


def target_assessment(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict[str, Any] | None:
    """The assessment the user picked with /pending, otherwise the newest open one."""
    open_ = open_assessments(context, user_id)
    chosen = context.user_data.get("target")
    for assessment in open_:
        if assessment["id"] == chosen:
            return assessment
    return open_[0] if open_ else None


def assessment_blocks(assessment: dict[str, Any], questions: list[dict[str, Any]]) -> list[str]:
    """The message users receive when an assessment is published."""
    header = (
        f"📝 <b>{esc(assessment['title'])}</b>\n"
        f"{len(questions)} questions · {num(total_points(questions))} points"
    )
    footer = (
        "✍️ <b>How to answer:</b> Send your answers as text, files, or photos. You can use any format "
        "(for example <code>1. B</code> then <code>2. your answer…</code>), then tap <b>✅ Submit</b> after sending your answers."
    )
    return [header, *question_blocks(questions, with_answers=False), footer]


# ── collecting the draft ─────────────────────────────────────────────────
async def add_to_draft(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    message, user = update.effective_message, update.effective_user
    svc = services(context)
    assessment = target_assessment(context, user.id)
    if assessment is None:
        hint = "📭 No assessments are waiting for your answers right now. Check /pending later."
        if svc.settings.is_admin(user.id):
            hint += " Send a PDF, DOCX or TXT file, or a photo, to create one."
        await message.reply_text(hint)
        return

    draft = context.user_data.get("draft")
    if not draft or draft["assessment_id"] != assessment["id"]:
        draft = {"assessment_id": assessment["id"], "parts": []}
        context.user_data["draft"] = draft
    if sum(len(p) for p in draft["parts"]) + len(text) > MAX_DRAFT_CHARS:
        await message.reply_text("⚠️ Your saved answers have reached the size limit. Tap Submit to send them, or use /cancel to start over.")
        return
    draft["parts"].append(text)

    parts = len(draft["parts"])
    await message.reply_text(
        f"Saved as your answer to <b>{esc(assessment['title'])}</b> "
        f"({parts} message{'s' if parts != 1 else ''} so far).\n"
        "You can keep sending answers, or tap ✅ Submit when you're finished. "
        "Saved something by mistake? Tap ↩️ Undo.",
        parse_mode=ParseMode.HTML,
        reply_markup=draft_keyboard(assessment["id"]),
    )


def draft_keyboard(assessment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Submit", callback_data=f"ans:submit:{assessment_id}"),
        InlineKeyboardButton("↩️ Undo", callback_data=f"ans:undo:{assessment_id}"),
        InlineKeyboardButton("🗑 Discard", callback_data="ans:clear:0"),
    ]])


def draft_parts(context: ContextTypes.DEFAULT_TYPE, assessment_id: int) -> list[str]:
    """The answer messages saved so far for this assessment (empty if none)."""
    draft = context.user_data.get("draft")
    return draft["parts"] if draft and draft["assessment_id"] == assessment_id else []


async def on_answer_upload(
    update: Update, context: ContextTypes.DEFAULT_TYPE, messages: list[Message], caption: str | None,
) -> None:
    """A non-admin sent a file or photo(s): its text is (part of) their answers.

    With nothing to answer, a picture can only be something they want explained.
    """
    message = update.effective_message
    svc = services(context)
    if target_assessment(context, update.effective_user.id) is None and is_image(messages[0]) and svc.settings.allow_explain:
        await explain.explain_upload(messages, context, question=None)
        return
    document = await read_upload(messages, context)
    if document is None:
        return
    if document.used_ocr:  # show what was read, so that a misread answer can be corrected before submitting
        await context.bot.send_message(
            message.chat_id,
            echo_block(document) + "\nIf something is wrong, send the correction as a message before you submit.",
            parse_mode=ParseMode.HTML,
        )
    text = f"{caption}\n{document.text}" if caption else document.text
    await add_to_draft(update, context, text)


# ── submitting and grading ───────────────────────────────────────────────
async def grade_submission(context: ContextTypes.DEFAULT_TYPE, submission_id: int) -> list[str]:
    """Grade a stored submission, save the result and return the result message blocks.

    Raises AIError if grading fails; the submission is then marked 'failed' and
    stays in the database for /regrade.
    """
    svc = services(context)
    submission = svc.db.get_submission(submission_id)
    assessment = svc.db.get_assessment(submission["assessment_id"])
    questions = svc.db.get_questions(assessment["id"])
    try:
        results, overall = await svc.ai.grade(questions, submission["raw_text"])
    except AIError:
        svc.db.mark_submission_failed(submission_id)
        raise
    score = round(sum(r["awarded"] for r in results), 2)
    maximum = total_points(questions)
    svc.db.save_grade(submission_id, score, maximum, overall, results)
    return result_blocks(assessment, questions, results, score, maximum, overall,
                         svc.settings.show_correct_answers)


def result_blocks(
    assessment: dict[str, Any], questions: list[dict[str, Any]], results: list[dict[str, Any]],
    score: float, maximum: float, overall: str, show_answers: bool,
) -> list[str]:
    blocks = [
        f"📊 <b>Result — {esc(assessment['title'])}</b>\n"
        f"Score: <b>{num(score)} / {num(maximum)}</b> ({percent(score, maximum)})"
    ]
    for question, result in zip(questions, results):
        icon = "✅" if result["awarded"] >= result["max"] else ("🟡" if result["awarded"] > 0 else "❌")
        block = f"{icon} <b>Q{result['number']}</b> — {num(result['awarded'])}/{num(result['max'])}"
        answer = result["student_answer"] or "(no answer found)"
        block += f"\nYour answer: {esc(answer)}"
        if result["feedback"]:
            block += f"\n{esc(result['feedback'])}"
        if show_answers and result["awarded"] < result["max"]:
            block += f"\n<i>Reference answer: {esc(question['answer'])}</i>"
        blocks.append(block)
    if overall:
        blocks.append(f"💬 {esc(overall)}")
    return blocks


async def on_answer_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, action, raw_id = query.data.split(":")
    user = update.effective_user
    svc = services(context)
    draft = context.user_data.get("draft")

    if action == "undo":
        parts = draft["parts"] if draft and draft["assessment_id"] == int(raw_id) else []
        if not parts:
            await query.answer("Nothing to undo.", show_alert=True)
            await query.edit_message_reply_markup(None)
            return
        parts.pop()
        if not parts:
            context.user_data.pop("draft", None)
        await query.answer("Removed")
        await query.edit_message_text(
            f"↩️ Removed your last message from the answers ({len(parts)} left)."
            if parts else "↩️ Removed. You have no saved answers now.",
            reply_markup=draft_keyboard(int(raw_id)) if parts else None,
        )
        return

    if action == "clear":
        context.user_data.pop("draft", None)
        await query.answer("Discarded")
        await query.edit_message_text("🗑 Your unsent answers have been discarded.")
        return

    assessment_id = int(raw_id)
    if not draft or draft["assessment_id"] != assessment_id or not draft["parts"]:
        await query.answer("Nothing to submit — these answers were already submitted or discarded.",
                           show_alert=True)
        await query.edit_message_reply_markup(None)
        return
    if context.user_data.get("grading"):
        await query.answer("Your previous submission is still being graded.", show_alert=True)
        return
    if assessment_id not in {a["id"] for a in open_assessments(context, user.id)}:
        context.user_data.pop("draft", None)
        await query.answer()
        await query.edit_message_text("This assessment is closed or you have already submitted it.")
        return

    context.user_data["grading"] = True
    try:
        raw_text = "\n".join(draft["parts"])
        context.user_data.pop("draft", None)
        context.user_data.pop("target", None)
        submission_id = svc.db.create_submission(assessment_id, user.id, raw_text)
        await query.answer("Submitted")
        await query.edit_message_text("✅ Your answers have been submitted! Grading is in progress…")
        await context.bot.send_chat_action(user.id, ChatAction.TYPING)
        try:
            blocks = await grade_submission(context, submission_id)
        except AIError as exc:
            log.error("Grading failed for submission %s: %s", submission_id, exc)
            await context.bot.send_message(
                user.id,
                "✅ Your answers are saved, but grading is temporarily unavailable. "
                "You don't need to submit again. Your result will be sent after the administrator retries grading.",
            )
            for admin_id in svc.settings.admin_ids:
                try:
                    await context.bot.send_message(
                        admin_id,
                        f"⚠️ Grading failed for a submission to assessment #{assessment_id}: {exc}\n"
                        f"Run /regrade {assessment_id} to retry.",
                    )
                except Exception:  # an admin who never started the bot cannot be messaged
                    log.debug("Could not notify admin %s", admin_id)
            return
        await send_blocks(context.bot, user.id, blocks)
    finally:
        context.user_data["grading"] = False


# ── commands ─────────────────────────────────────────────────────────────
async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    open_ = open_assessments(context, update.effective_user.id)
    if not open_:
        await update.effective_message.reply_text("📭 You're all caught up! No assessments are waiting for your answers right now.")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"#{a['id']} · {a['title']}"[:60], callback_data=f"show:{a['id']}")]
        for a in open_[:20]
    ])
    await update.effective_message.reply_text(
        "📝 <b>Assessments to complete</b>\nChoose an assessment below to view its questions:", parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


async def on_show_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assessment_id = int(query.data.split(":")[1])
    user = update.effective_user
    open_ = {a["id"]: a for a in open_assessments(context, user.id)}
    await query.answer()
    if assessment_id not in open_:
        await query.edit_message_text("That assessment is closed or you have already submitted it.")
        return
    context.user_data["target"] = assessment_id
    context.user_data.pop("draft", None)
    questions = services(context).db.get_questions(assessment_id)
    await send_blocks(context.bot, user.id, assessment_blocks(open_[assessment_id], questions))


async def my_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = services(context).db.user_submissions(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("📊 No results yet. After submitting an assessment, your scores will appear here.")
        return
    lines = ["<b>Your results</b>"]
    for row in rows:
        if row["status"] == "graded":
            result = f"{num(row['score'])}/{num(row['max_score'])} ({percent(row['score'], row['max_score'])})"
        else:
            result = "waiting to be graded"
        lines.append(f"• {esc(row['title'])} — {result}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    had_draft = context.user_data.pop("draft", None)
    await update.effective_message.reply_text(
        "Your typed answers were discarded." if had_draft else "There are no unsent answers to discard."
    )


def stored_result_blocks(context: ContextTypes.DEFAULT_TYPE, submission: dict[str, Any]) -> list[str]:
    """Rebuild the result message of an already graded submission."""
    svc = services(context)
    assessment = svc.db.get_assessment(submission["assessment_id"])
    questions = svc.db.get_questions(assessment["id"])
    return result_blocks(
        assessment, questions, json.loads(submission["details"]), submission["score"],
        submission["max_score"], submission["feedback"] or "", svc.settings.show_correct_answers,
    )


def register(application: Application) -> None:
    application.add_handler(CommandHandler("pending", pending, filters=PRIVATE))
    application.add_handler(CommandHandler("myresults", my_results, filters=PRIVATE))
    application.add_handler(CommandHandler("cancel", cancel, filters=PRIVATE))
    application.add_handler(CallbackQueryHandler(on_answer_button, pattern=r"^ans:(submit|undo|clear):\d+$"))
    application.add_handler(CallbackQueryHandler(on_show_button, pattern=r"^show:\d+$"))
