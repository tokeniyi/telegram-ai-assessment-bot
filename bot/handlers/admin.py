"""Admin side: upload material, generate questions, publish, see results."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters,
)

from ..ai import AIError
from ..extract import SUPPORTED_DESCRIPTION
from ..utils import (
    display_name, esc, num, percent, question_blocks, send_blocks, services, total_points,
)
from . import student
from .reading import read_upload

log = logging.getLogger(__name__)

PRIVATE = filters.ChatType.PRIVATE
BROADCAST_DELAY = 0.05  # seconds between messages; keeps well under Telegram's ~30 msg/s limit


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return services(context).settings.is_admin(update.effective_user.id)


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if _is_admin(update, context):
        return True
    await update.effective_message.reply_text("This command is for administrators only.")
    return False


def _draft_keyboard(assessment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Send to all users", callback_data=f"pub:{assessment_id}")],
        [
            InlineKeyboardButton("🔄 Regenerate", callback_data=f"regen:{assessment_id}"),
            InlineKeyboardButton("🗑 Discard", callback_data=f"del:{assessment_id}"),
        ],
    ])


async def _send_preview(context: ContextTypes.DEFAULT_TYPE, chat_id: int, assessment_id: int) -> None:
    db = services(context).db
    assessment = db.get_assessment(assessment_id)
    questions = db.get_questions(assessment_id)
    header = (
        f"📝 <b>Draft #{assessment_id} — {esc(assessment['title'])}</b>\n"
        f"{len(questions)} questions · {num(total_points(questions))} points\n"
        "Reference answers are shown to you only."
    )
    await send_blocks(
        context.bot, chat_id, [header, *question_blocks(questions, with_answers=True)],
        reply_markup=_draft_keyboard(assessment_id),
    )


# ── material in, draft out ───────────────────────────────────────────────
async def _create_draft(
    message: Message, context: ContextTypes.DEFAULT_TYPE, material: str,
    source_name: str | None, instructions: str | None,
) -> None:
    svc = services(context)
    limit = svc.settings.max_document_chars
    note = ""
    if len(material) > limit:
        note = (
            f"\n⚠️ The material has {len(material):,} characters; only the first {limit:,} are used "
            "(MAX_DOCUMENT_CHARS in .env). Questions will not cover the rest."
        )
        material = material[:limit]

    status = await message.reply_text(f"⏳ Generating questions with {svc.ai.provider.model}…{note}")
    await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
    try:
        title, questions = await svc.ai.generate(material, instructions)
    except AIError as exc:
        log.error("Question generation failed: %s", exc)
        await status.edit_text(f"❌ Could not generate questions: {exc}")
        return
    assessment_id = svc.db.create_assessment(
        title, source_name, material, instructions, message.from_user.id, questions
    )
    await _send_preview(context, message.chat_id, assessment_id)


async def on_material(messages: list[Message], context: ContextTypes.DEFAULT_TYPE, caption: str | None) -> None:
    """An admin sent a file, a photo or an album of photographed pages: draft an assessment from it."""
    document = await read_upload(messages, context)
    if document is None:
        return
    first = messages[0]
    if first.document:
        name = first.document.file_name
    else:
        name = "photo" if len(messages) == 1 else f"{len(messages)} photos"
    await _create_draft(first, context, document.text, name, caption)


async def generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/generate [instructions], sent as a reply to a text message or a file."""
    if not await _require_admin(update, context):
        return
    message = update.effective_message
    source = message.reply_to_message
    instructions = " ".join(context.args) or None
    if source and (source.document or source.photo):
        document = await read_upload([source], context)
        material = document.text if document else None
        name = source.document.file_name if source.document else "photo"
    elif source and (source.text or source.caption):
        material, name = source.text or source.caption, "pasted text"
    else:
        await message.reply_text(
            f"Send a {SUPPORTED_DESCRIPTION} to generate questions from it.\n"
            "To use pasted notes instead: send the notes as a message, then reply to that message "
            "with /generate (optionally followed by instructions, e.g. /generate 8 questions, easy)."
        )
        return
    if not material:
        return
    if len(material.strip()) < 100:
        await message.reply_text("That text is too short to write questions from.")
        return
    await _create_draft(message, context, material, name, instructions)


# ── draft buttons ────────────────────────────────────────────────────────
async def on_draft_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update, context):
        await query.answer("Administrators only.", show_alert=True)
        return
    action, raw_id = query.data.split(":")
    assessment_id = int(raw_id)
    svc = services(context)
    assessment = svc.db.get_assessment(assessment_id)
    if assessment is None or assessment["status"] != "draft":
        await query.answer("This draft was already published or discarded.", show_alert=True)
        await query.edit_message_reply_markup(None)
        return
    chat_id = query.message.chat_id

    if action == "del":
        svc.db.delete_assessment(assessment_id)
        await query.answer("Discarded")
        await query.edit_message_reply_markup(None)
        await context.bot.send_message(chat_id, f"🗑 Draft #{assessment_id} discarded.")

    elif action == "regen":
        await query.answer("Regenerating…")
        await query.edit_message_reply_markup(None)
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        try:
            title, questions = await svc.ai.generate(assessment["source_text"], assessment["instructions"])
        except AIError as exc:
            await context.bot.send_message(
                chat_id, f"❌ Could not regenerate: {exc}", reply_markup=_draft_keyboard(assessment_id)
            )
            return
        svc.db.replace_questions(assessment_id, title, questions)
        await _send_preview(context, chat_id, assessment_id)

    elif action == "pub":
        svc.db.set_status(assessment_id, "published")  # before sending, so a second tap is a no-op
        await query.answer("Publishing…")
        await query.edit_message_reply_markup(None)
        sent, blocked, failed = await _broadcast(context, assessment_id)
        report = f"📤 Assessment #{assessment_id} sent to {sent} user{'s' if sent != 1 else ''}."
        if blocked:
            report += f"\n{blocked} blocked the bot and were skipped."
        if failed:
            report += f"\n{failed} could not be reached (see the log)."
        report += f"\nUse /results {assessment_id} to follow the scores."
        await context.bot.send_message(chat_id, report)


async def _broadcast(context: ContextTypes.DEFAULT_TYPE, assessment_id: int) -> tuple[int, int, int]:
    db = services(context).db
    blocks = student.assessment_blocks(db.get_assessment(assessment_id), db.get_questions(assessment_id))
    sent = blocked = failed = 0
    for user_id in db.active_user_ids():
        try:
            try:
                await send_blocks(context.bot, user_id, blocks)
            except RetryAfter as exc:
                delay = exc.retry_after
                await asyncio.sleep(delay.total_seconds() if hasattr(delay, "total_seconds") else delay)
                await send_blocks(context.bot, user_id, blocks)
            sent += 1
        except Forbidden:
            db.set_user_active(user_id, False)
            blocked += 1
        except TelegramError as exc:
            log.warning("Could not send assessment %s to %s: %s", assessment_id, user_id, exc)
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY)
    return sent, blocked, failed


# ── commands ─────────────────────────────────────────────────────────────
async def _assessment_from_args(
    update: Update, context: ContextTypes.DEFAULT_TYPE, default_latest: bool
) -> dict[str, Any] | None:
    db = services(context).db
    message = update.effective_message
    if context.args:
        raw = context.args[0].lstrip("#")
        assessment = db.get_assessment(int(raw)) if raw.isdigit() else None
        if assessment is None:
            await message.reply_text(f"There is no assessment {context.args[0]}. See /assessments.")
        return assessment
    if default_latest:
        latest = [a for a in db.list_assessments() if a["status"] != "draft"]
        if latest:
            return db.get_assessment(latest[0]["id"])
        await message.reply_text("No assessment has been published yet.")
        return None
    await message.reply_text("Give the assessment number, for example: /close 3  (see /assessments)")
    return None


async def assessments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    rows = services(context).db.list_assessments()
    if not rows:
        await update.effective_message.reply_text("No assessments yet. Send a file to create one.")
        return
    icons = {"draft": "✏️", "published": "🟢", "closed": "🔒"}
    lines = ["<b>Assessments</b> (newest first)"]
    for row in rows:
        lines.append(
            f"{icons.get(row['status'], '')} <b>#{row['id']}</b> {esc(row['title'])} — {row['status']}, "
            f"{row['num_questions']} questions, {row['num_submitters']} submitted"
        )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    assessment = await _assessment_from_args(update, context, default_latest=True)
    if assessment is None:
        return
    db = services(context).db
    rows = db.latest_submissions(assessment["id"])
    questions = db.get_questions(assessment["id"])
    header = f"📊 <b>#{assessment['id']} — {esc(assessment['title'])}</b> ({assessment['status']})"
    if not rows:
        await update.effective_message.reply_text(
            f"{header}\nNo submissions yet.", parse_mode=ParseMode.HTML
        )
        return

    graded = [r for r in rows if r["status"] == "graded"]
    _, active_users = db.count_users()
    summary = f"{header}\n{len(rows)} submitted (of {active_users} active users)"
    if graded:
        average = sum(r["score"] for r in graded) / len(graded)
        summary += f" · average {num(round(average, 2))}/{num(graded[0]['max_score'])}"
    if len(graded) < len(rows):
        summary += f"\n⚠️ {len(rows) - len(graded)} not graded yet — run /regrade {assessment['id']}"
    blocks = [summary]
    for rank, row in enumerate(rows, start=1):
        if row["status"] == "graded":
            score = f"{num(row['score'])}/{num(row['max_score'])} ({percent(row['score'], row['max_score'])})"
        else:
            score = "not graded"
        blocks.append(f"{rank}. {esc(display_name(row))} — {score}")
    # one line per user: join them so they are not separated by blank lines
    await send_blocks(context.bot, update.effective_chat.id, [blocks[0], "\n".join(blocks[1:])])

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["user_id", "username", "name", "status", "score", "max_score", "percent",
                     "submitted_at", *[f"q{q['position']}" for q in questions], "feedback", "raw_answers"])
    for row in rows:
        details = json.loads(row["details"]) if row["details"] else []
        per_question = {d["number"]: d["awarded"] for d in details}
        writer.writerow([
            row["user_id"], row["username"] or "", row["full_name"] or "", row["status"],
            row["score"] if row["score"] is not None else "", row["max_score"] or "",
            percent(row["score"], row["max_score"]).rstrip("%–"), row["created_at"],
            *[per_question.get(q["position"], "") for q in questions],
            row["feedback"] or "", row["raw_text"],
        ])
    await context.bot.send_document(
        update.effective_chat.id,
        document=io.BytesIO(buffer.getvalue().encode("utf-8-sig")),
        filename=f"assessment_{assessment['id']}_results.csv",
        caption="Full results, including each user's raw answers.",
    )


async def close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    assessment = await _assessment_from_args(update, context, default_latest=False)
    if assessment is None:
        return
    if assessment["status"] != "published":
        await update.effective_message.reply_text(
            f"Assessment #{assessment['id']} is {assessment['status']}, not open."
        )
        return
    services(context).db.set_status(assessment["id"], "closed")
    await update.effective_message.reply_text(
        f"🔒 Assessment #{assessment['id']} is closed. No more answers are accepted."
    )


async def regrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Grade the submissions of an assessment whose grading failed earlier."""
    if not await _require_admin(update, context):
        return
    assessment = await _assessment_from_args(update, context, default_latest=False)
    if assessment is None:
        return
    message = update.effective_message
    waiting = services(context).db.ungraded_submissions(assessment["id"])
    if not waiting:
        await message.reply_text("Every submission for this assessment is already graded.")
        return
    await message.reply_text(f"⏳ Grading {len(waiting)} submission(s)…")
    done = 0
    for submission in waiting:
        try:
            blocks = await student.grade_submission(context, submission["id"])
        except AIError as exc:
            await message.reply_text(f"❌ Stopped after {done}: {exc}")
            return
        done += 1
        try:
            await send_blocks(context.bot, submission["user_id"], blocks)
        except TelegramError as exc:
            log.warning("Could not send result to %s: %s", submission["user_id"], exc)
    await message.reply_text(f"✅ Graded {done} submission(s). The users have their results.")


async def users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    total, active = services(context).db.count_users()
    await update.effective_message.reply_text(
        f"👥 {total} registered users, {active} reachable. Everyone who has started the bot "
        "receives published assessments."
    )


def register(application: Application) -> None:
    application.add_handler(CommandHandler("generate", generate, filters=PRIVATE))
    application.add_handler(CommandHandler("assessments", assessments, filters=PRIVATE))
    application.add_handler(CommandHandler("results", results, filters=PRIVATE))
    application.add_handler(CommandHandler("close", close, filters=PRIVATE))
    application.add_handler(CommandHandler("regrade", regrade, filters=PRIVATE))
    application.add_handler(CommandHandler("users", users, filters=PRIVATE))
    application.add_handler(CallbackQueryHandler(on_draft_button, pattern=r"^(pub|regen|del):\d+$"))
