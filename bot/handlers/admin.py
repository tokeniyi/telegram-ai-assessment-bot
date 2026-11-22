"""Admin side: upload material, generate questions, publish, see results."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters,
)

from .. import commands, deadlines, guidance
from ..ai import AIError, AIUnavailable
from ..alerts import clock
from ..commands import Suggestion
from ..guidance import AUTOMATIC, INPUT, PERMISSION, STATE, TEMPORARY, plural
from ..extract import SUPPORTED_DESCRIPTION
from ..logs import event
from ..utils import (
    display_name, esc, num, percent, question_blocks, send_blocks, services, total_points,
)
from . import student, work
from .reading import read_upload

log = logging.getLogger(__name__)

PRIVATE = filters.ChatType.PRIVATE


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return services(context).settings.is_admin(update.effective_user.id)


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if _is_admin(update, context):
        return True
    # Nothing an administrator's command could do is suggested; /help lists what this user can use.
    await guidance.reply(update, context, "This command is for administrators only.",
                         [Suggestion("help", why="What you can do")], kind=PERMISSION)
    return False


def _ai_failure(exc: AIError, retry: str) -> tuple[str, str]:
    """(kind, explanation) for a failed AI request: the provider not answering is temporary;
    a reply that could not be used may work on another try."""
    if isinstance(exc, AIUnavailable):
        return TEMPORARY, f"The AI provider is not answering right now ({esc(exc)}). {retry} in a few minutes."
    return STATE, f"{esc(exc)} {retry}."


def _settings_suggestion(assessment_id: int, why: str = "Deadline and options") -> Suggestion:
    return Suggestion("settings", (str(assessment_id),), why=why, button=f"⚙️ Settings #{assessment_id}")


def _draft_keyboard(assessment_id: int) -> InlineKeyboardMarkup:
    settings = _settings_suggestion(assessment_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Send to all users", callback_data=f"pub:{assessment_id}")],
        [
            InlineKeyboardButton("🔄 Regenerate", callback_data=f"regen:{assessment_id}"),
            InlineKeyboardButton("🗑 Discard", callback_data=f"del:{assessment_id}"),
        ],
        [InlineKeyboardButton(settings.button, callback_data=settings.callback_data)],
    ])


async def _send_preview(context: ContextTypes.DEFAULT_TYPE, chat_id: int, assessment_id: int) -> None:
    db = services(context).db
    assessment = db.get_assessment(assessment_id)
    questions = db.get_questions(assessment_id)
    header = (
        f"📝 <b>Draft #{assessment_id} — {esc(assessment['title'])}</b>\n"
        f"{len(questions)} questions · {num(total_points(questions))} points\n"
        f"{_options_summary(services(context).settings, assessment)}\n"
        "🔒 Reference answers are visible only to administrators. Review before publishing."
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
        kind, why = _ai_failure(exc, "Nothing was saved; send the material again")
        body, markup = guidance.render(
            svc.settings, message.from_user.id, f"Could not generate questions. {why}",
            [Suggestion("status", why="Check the AI provider")] if kind == TEMPORARY else [], kind=kind)
        await status.edit_text(f"❌ {body}", parse_mode=ParseMode.HTML, reply_markup=markup)
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
        example = commands.COMMANDS["generate"].example
        await guidance.reply(update, context,
            f"/generate works as a reply. Send a {esc(SUPPORTED_DESCRIPTION)} to generate questions "
            "directly, or send your notes as a message and reply to it with /generate, optionally "
            f"followed by instructions: {commands.code(example)}", kind=INPUT)
        return
    if not material:
        return
    if len(material.strip()) < 100:
        await guidance.reply(update, context, "There's not enough text to generate useful questions "
                             f"({len(material.strip())} characters; at least 100 are needed). Reply "
                             "/generate to longer notes.", kind=INPUT)
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
        if not svc.db.discard_draft_assessment(assessment_id):
            await query.answer("This draft was already published or discarded.", show_alert=True)
            await query.edit_message_reply_markup(None)
            return
        await query.answer("Discarded")
        await query.edit_message_reply_markup(None)
        await context.bot.send_message(chat_id, f"🗑 Draft #{assessment_id} was discarded. No assessment was sent.")

    elif action == "regen":
        await query.answer("Regenerating…")
        await query.edit_message_reply_markup(None)
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        try:
            title, questions = await svc.ai.generate(assessment["source_text"], assessment["instructions"])
        except AIError as exc:
            _, why = _ai_failure(exc, "The draft is unchanged; tap Regenerate again")
            await context.bot.send_message(chat_id, f"❌ Could not regenerate. {why}", parse_mode=ParseMode.HTML,
                                           reply_markup=_draft_keyboard(assessment_id))
            return
        if not svc.db.replace_questions(assessment_id, title, questions):
            await context.bot.send_message(
                chat_id, f"Draft #{assessment_id} was published or discarded while the new questions "
                         "were being written, so they were not used."
            )
            return
        await _send_preview(context, chat_id, assessment_id)

    elif action == "pub":
        if assessment["deadline"] and assessment["deadline"] <= datetime.now(timezone.utc):
            await query.answer()
            await guidance.reply(update, context,
                f"The deadline of draft #{assessment_id} "
                f"({esc(deadlines.format_moment(assessment['deadline'], svc.settings.tz))}) has passed, so it "
                "was not sent. Set a new deadline (or remove it), then tap 📤 Send to all users again.",
                [Suggestion("deadline", (str(assessment_id), "2d"), why="Close in 2 days"),
                 Suggestion("deadline", (str(assessment_id), "off"), why="No deadline")], kind=STATE)
            return
        # One database step changes the status and queues the messages, so of two taps (or two
        # bot processes) only one gets past this line.
        queued = svc.db.publish_assessment(assessment_id, update.effective_user.id)
        if queued is None:
            await query.answer("This draft was already published or discarded.", show_alert=True)
            await query.edit_message_reply_markup(None)
            return
        await query.answer("Publishing…")
        await query.edit_message_reply_markup(None)
        log.info("Assessment %s published; sending to %s users", assessment_id, queued)
        if not queued:  # nobody to send it to: there is no queue to report on
            svc.db.mark_report_sent(assessment_id)
            await context.bot.send_message(chat_id, work.delivery_report(assessment_id, {}), parse_mode=ParseMode.HTML)
            return
        # Sent from the queue: if the bot stops halfway, the next start sends the rest.
        await work.deliver(context.bot, svc, assessment_id)
        await work.report_deliveries(context.bot, svc, assessment_id)


# ── commands ─────────────────────────────────────────────────────────────
_NOT_POSSIBLE = {  # (command, state) -> why the command cannot act on an assessment in that state
    ("close", "draft"): "is still a draft, so it has not been sent to anyone. Publish it with "
                        "📤 Send to all users under its preview.",
    ("close", "closed"): "is already closed.",
    ("results", "draft"): "is still a draft, so nobody has answered it yet.",
    ("regrade", "draft"): "is still a draft, so it has no submissions to grade.",
    ("deadline", "closed"): "is closed, so its deadline can no longer be changed.",
}


async def _assessment_from_args(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> dict[str, Any] | None:
    """The assessment named in the command's argument, checked against what the command accepts
    (commands.COMMANDS). When it is missing or wrong, the reply shows the command with ids it
    can act on right now, taken from the database."""
    db = services(context).db
    spec = commands.COMMANDS[command]
    arg = spec.args[0]
    choices = lambda: guidance.assessment_choices(db, command)  # noqa: E731

    if context.args:
        raw = context.args[0]
        if not arg.accepts(raw):
            options = choices()  # real ids, never the registry's sample one
            await guidance.reply(update, context, f"“{esc(raw)}” is not an assessment number. "
                                 + ("For example:" if options else f"Write it as {commands.code(spec.usage)}."),
                                 options, kind=INPUT)
            return None
        assessment = db.get_assessment(int(raw.lstrip("#")))
        if assessment is None:
            options = choices()
            await guidance.reply(update, context, f"There is no assessment #{esc(raw.lstrip('#'))}."
                                 + (" Did you mean one of these?" if options else " See /assessments."),
                                 options, kind=INPUT)
            return None
        if arg.states and assessment["status"] not in arg.states:
            why = _NOT_POSSIBLE.get((command, assessment["status"]), f"{guidance.describe_state(assessment)}.")
            nexts = [guidance.results_suggestion(assessment["id"])] \
                if command in ("close", "deadline") and assessment["status"] == "closed" else []
            await guidance.reply(update, context, f"Assessment #{assessment['id']} {why}", nexts, kind=STATE)
            return None
        return assessment

    if not arg.required:  # /results: the latest published or closed assessment
        latest = [a for a in db.list_assessments() if a["status"] in arg.states]
        if latest:
            return db.get_assessment(latest[0]["id"])
        await guidance.reply(update, context, "No assessment has been published yet, so there are no "
                             "results. Send a document or photo to create a draft, then publish it.", kind=STATE)
        return None

    options = choices()
    if options:
        await guidance.reply(update, context, f"/{command} needs an assessment number, for example:",
                             options, kind=INPUT)
    else:
        nothing = {"close": "No assessment is open, so there is nothing to close.",
                   "regrade": "No grading has failed, so there is nothing to retry.",
                   "deadline": "No assessment is a draft or open, so there is nothing to set a deadline for.",
                   "settings": "There are no assessments yet. Send a document or photo to create a draft."}[command]
        await guidance.reply(update, context, f"/{command} needs an assessment number. {nothing}", kind=STATE)
    return None


async def assessments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    rows = services(context).db.list_assessments()
    if not rows:
        await update.effective_message.reply_text("📭 No assessments yet. Send a document or photo to create your first draft.")
        return
    icons = {"draft": "✏️", "published": "🟢", "closed": "🔒"}
    lines = ["<b>Assessments</b> (newest first)"]
    tz = services(context).settings.tz
    for row in rows:
        due = ""
        if row["deadline"] and row["status"] != "closed":
            due = f", closes {esc(deadlines.format_moment(row['deadline'], tz))}"
        lines.append(
            f"{icons.get(row['status'], '')} <b>#{row['id']}</b> {esc(row['title'])} — {row['status']}, "
            f"{row['num_questions']} questions, {row['num_submitters']} submitted{due}"
        )
    db = services(context).db
    nexts = guidance.regrade_suggestions(db)  # only what needs the admin
    answered = [r for r in rows if r["status"] != "draft" and r["num_submitters"]]
    if answered:
        nexts.append(guidance.results_suggestion(answered[0]["id"], f"Scores of #{answered[0]['id']}"))
    await guidance.reply(update, context, "\n".join(lines), nexts)


async def results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    assessment = await _assessment_from_args(update, context, "results")
    if assessment is None:
        return
    svc = services(context)
    db = svc.db
    rows = db.latest_submissions(assessment["id"])
    questions = db.get_questions(assessment["id"])
    header = f"📊 <b>#{assessment['id']} — {esc(assessment['title'])}</b> ({assessment['status']})"
    if not rows:
        await update.effective_message.reply_text(
            f"{header}\nNo submissions yet. Results will appear once users submit their answers.", parse_mode=ParseMode.HTML
        )
        return

    graded = [r for r in rows if r["status"] == "graded"]
    _, active_users = db.count_users()
    summary = f"{header}\n{len(rows)} submitted (of {active_users} active users)"
    if graded:
        average = sum(r["score"] for r in graded) / len(graded)
        summary += f" · average {num(round(average, 2))}/{num(graded[0]['max_score'])}"
    nexts = []
    if len(graded) < len(rows):
        counts = db.grading_counts(assessment["id"], work.grading_lease(svc))
        summary += "\n" + "\n".join(guidance.grading_lines(counts))
        if counts["failed"]:  # only failed grading needs the admin; the rest finishes by itself
            nexts = [Suggestion("regrade", (str(assessment["id"]),), why="Retry the failed grading",
                                button=f"🔁 Retry grading #{assessment['id']}")]
    summary, markup = guidance.render(svc.settings, update.effective_user.id, summary, nexts)
    blocks = [summary]
    for rank, row in enumerate(rows, start=1):
        if row["status"] == "graded":
            score = f"{num(row['score'])}/{num(row['max_score'])} ({percent(row['score'], row['max_score'])})"
        else:
            score = "not graded"
        blocks.append(f"{rank}. {esc(display_name(row))} — {score}")
    # one line per user: join them so they are not separated by blank lines
    await send_blocks(context.bot, update.effective_chat.id, [blocks[0], "\n".join(blocks[1:])], reply_markup=markup)

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
        caption="📊 Downloadable results with scores and submitted answers.",
    )


async def close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    assessment = await _assessment_from_args(update, context, "close")
    if assessment is None:
        return
    aid = assessment["id"]
    if not services(context).db.close_assessment(aid):  # closed meanwhile (another admin or process)
        await guidance.reply(update, context, f"Assessment #{aid} is already closed.",
                             [guidance.results_suggestion(aid)], kind=STATE)
        return
    await guidance.reply(update, context, f"🔒 Assessment #{aid} is closed. No more answers are accepted.",
                         [guidance.results_suggestion(aid, "See the final scores")])


async def regrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Grade the submissions of an assessment whose grading failed (or was abandoned).

    Each submission is claimed in the database just before it is graded (claim_for_regrade), so
    two /regrade runs, a double tap on a retry button, or the background work never grade the
    same submission twice: whoever does not get the claim leaves it alone.
    """
    if not await _require_admin(update, context):
        return
    assessment = await _assessment_from_args(update, context, "regrade")
    if assessment is None:
        return
    message = update.effective_message
    svc = services(context)
    aid = assessment["id"]
    lease = work.grading_lease(svc)
    counts = svc.db.grading_counts(aid, lease)
    if not counts["failed"] and not counts["interrupted"]:
        if counts["grading_now"]:
            await guidance.reply(update, context, f"{plural(counts['grading_now'], 'submission')} to #{aid} "
                                 "being graded right now; results are sent when ready. No action needed.",
                                 kind=AUTOMATIC)
        elif not svc.db.latest_submissions(aid):
            await guidance.reply(update, context, f"Nobody has submitted answers to #{aid} yet, so there "
                                 "is nothing to grade.", kind=STATE)
        else:
            await guidance.reply(update, context, f"Every submission to #{aid} is already graded.",
                                 [guidance.results_suggestion(aid)], kind=STATE)
        return
    waiting = svc.db.ungraded_submissions(aid)
    await message.reply_text(f"⏳ Grading {plural(counts['failed'] + counts['interrupted'], 'submission')}…")
    done = busy = delivered = 0
    for submission in waiting:
        # Claimed one at a time, just before grading, so that a long batch does not outlive its
        # leases. One that is being graded right now (just submitted, picked up after a restart,
        # or by another /regrade) is left to whoever is grading it.
        if not svc.db.claim_for_regrade(submission["id"], lease):
            busy += 1
            continue
        try:
            blocks = await work.grade(svc, submission["id"])
        except AIError as exc:
            left = svc.db.grading_counts(aid, lease)["failed"]
            kind, why = _ai_failure(exc, "Try again")
            await guidance.reply(update, context,
                f"Stopped after grading {done}. {why}\n{plural(left, 'submission')} still to grade; "
                "the students' answers are saved.",
                [Suggestion("regrade", (str(aid),), why="Retry", button=f"🔁 Retry grading #{aid}")], kind=kind)
            return
        done += 1
        if blocks is not None and await work.send_result(context.bot, svc, submission, blocks):
            delivered += 1
    report = f"✅ Graded {done} submission(s); {delivered} user(s) received their result."
    if busy:
        report += (f"\n🔁 {busy} were being graded already (by another process or retry) and will be "
                   "sent when ready. No action needed.")
    await message.reply_text(report)


async def users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    total, active = services(context).db.count_users()
    await update.effective_message.reply_text(
        f"👥 {total} registered users, {active} reachable. Everyone who has started the bot "
        "receives published assessments."
    )


# ── deadlines and per-assessment options (Stage 7) ───────────────────────
def _options_summary(settings, assessment: dict[str, Any]) -> str:
    """One line for the draft preview: the deadline and both options."""
    due = deadlines.describe(assessment["deadline"], settings.tz) if assessment["deadline"] else "none"
    answers = "shown" if settings.shows_answers(assessment) else "hidden"
    resubmit = "allowed" if settings.allows_resubmission(assessment) else "once only"
    return f"⏰ Deadline: {esc(due)} · 📖 Reference answers after grading: {answers} · 🔁 Submitting: {resubmit}"


def _deadline_examples(assessment: dict[str, Any]) -> list[Suggestion]:
    aid = str(assessment["id"])
    examples = [Suggestion("deadline", (aid, "2d"), why="Close in 2 days"),
                Suggestion("deadline", (aid, "tomorrow 17:00"), why="Close tomorrow at 17:00")]
    if assessment["deadline"]:
        examples.append(Suggestion("deadline", (aid, "off"), why="Remove the deadline"))
    return examples


def _settings_view(settings, assessment: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """/settings: the assessment's deadline and options, with a button per option that sets the
    other value (an explicit value, so a double tap does not undo the first)."""
    aid, status = assessment["id"], assessment["status"]
    default = lambda column: " (bot default)" if assessment[column] is None else ""  # noqa: E731
    answers = settings.shows_answers(assessment)
    resubmit = settings.allows_resubmission(assessment)
    lines = [
        f"⚙️ <b>Settings of #{aid} — {esc(assessment['title'])}</b> ({status})",
        f"⏰ Deadline: {esc(deadlines.describe(assessment['deadline'], settings.tz))}"
        + (", closed" if status == "closed" else ""),
        f"📖 Reference answers in results: {'shown' if answers else 'hidden'}{default('show_answers')}",
        f"🔁 Resubmission: {'allowed, the latest attempt counts' if resubmit else 'not allowed, one submission each'}"
        f"{default('allow_resubmission')}",
        "<i>Changes apply from now on; results already sent stay as they are.</i>",
    ]
    if status in ("draft", "published"):  # typed: /deadline is not a button command
        lines += [f"👉 {s.html()}" for s in _deadline_examples(assessment)]
    body = "\n".join(lines)
    rows = [[InlineKeyboardButton("📖 Hide reference answers" if answers else "📖 Show reference answers",
                                  callback_data=f"opt:{aid}:show_answers:{int(not answers)}")]]
    if status != "closed":
        rows.append([InlineKeyboardButton("🔁 One submission only" if resubmit else "🔁 Allow resubmission",
                                          callback_data=f"opt:{aid}:allow_resubmission:{int(not resubmit)}")])
    return body, InlineKeyboardMarkup(rows)


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    assessment = await _assessment_from_args(update, context, "settings")
    if assessment is None:
        return
    body, markup = _settings_view(services(context).settings, assessment)
    await update.effective_message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup,
                                              disable_web_page_preview=True)


async def on_option_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update, context):
        await query.answer("Administrators only.", show_alert=True)
        return
    _, raw_id, option, value = query.data.split(":")
    svc = services(context)
    assessment = svc.db.get_assessment(int(raw_id))
    if assessment is None:
        await query.answer("This assessment no longer exists.", show_alert=True)
        await query.edit_message_reply_markup(None)
        return
    if option == "allow_resubmission" and assessment["status"] == "closed":
        await query.answer("This assessment is closed, so nobody can submit to it any more.", show_alert=True)
        return
    assessment = svc.db.set_option(assessment["id"], option, value == "1")
    event(log, "assessment.option_changed", assessment=assessment["id"], option=option,
          value=value == "1", admin=update.effective_user.id)
    await query.answer("Saved")
    body, markup = _settings_view(svc.settings, assessment)
    await query.edit_message_text(body, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)


async def deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/deadline <id> [when]: show, set or remove when an assessment closes by itself."""
    if not await _require_admin(update, context):
        return
    assessment = await _assessment_from_args(update, context, "deadline")
    if assessment is None:
        return
    svc = services(context)
    aid, tz = assessment["id"], svc.settings.tz
    when = " ".join(context.args[1:]).strip()
    if not when:
        await guidance.reply(update, context, f"⏰ Deadline of #{aid}: "
                             f"{esc(deadlines.describe(assessment['deadline'], tz))}. To change it, add when it "
                             f"closes (times are {esc(svc.settings.timezone)}), for example:",
                             _deadline_examples(assessment))
        return
    try:
        new = deadlines.parse(when, datetime.now(timezone.utc), tz)
    except deadlines.DeadlineError as exc:
        await guidance.reply(update, context, f"{esc(exc)} Times are {esc(svc.settings.timezone)}. For example:",
                             _deadline_examples(assessment), kind=INPUT)
        return
    row = svc.db.set_deadline(aid, new)
    if row is None:  # closed meanwhile, or its old deadline passed before the background work closed it
        await guidance.reply(update, context, f"Assessment #{aid} is closed, so its deadline can no longer be "
                             "changed.", [guidance.results_suggestion(aid)], kind=STATE)
        return
    if new is None:
        text = f"⏰ Assessment #{aid} has no deadline now. It stays open until you close it."
    else:
        text = (f"⏰ Assessment #{aid} now closes {esc(deadlines.describe(new, tz))}. After that no more "
                "answers are accepted, and you are told how many students submitted.")
        hours = svc.settings.reminder_hours
        if hours:
            text += f"\nStudents who haven't submitted are reminded {hours} h before."
    if row["status"] == "draft":
        text += "\nIt is still a draft: tap 📤 Send to all users under its preview to publish it."
    await guidance.reply(update, context, text, [_settings_suggestion(aid, "All options")])


# ── /status ──────────────────────────────────────────────────────────────
STATUS_CHECK_TIMEOUT = 12  # seconds /status waits for the AI provider


async def _check_ai(svc) -> str:
    """Ask the provider whether it answers, without spending tokens (AIProvider.check)."""
    check = getattr(svc.ai.provider, "check", None)
    if check is None:
        return "➖ not checked (this provider has no check)"
    started = time.monotonic()
    try:
        note = await asyncio.wait_for(check(), STATUS_CHECK_TIMEOUT)
    except asyncio.TimeoutError:
        return f"❌ no answer within {STATUS_CHECK_TIMEOUT} s"
    except AIError as exc:
        return f"❌ {esc(exc)}"
    except Exception as exc:  # the check must not break /status
        log.warning("AI provider check failed unexpectedly: %r", exc)
        return f"❌ check failed: {esc(exc)}"
    return f"✅ {esc(note)} ({time.monotonic() - started:.1f} s)"


def _ai_history(ai: dict[str, Any]) -> list[str]:
    if ai["consecutive_failures"]:
        lines = [f"Recent requests: ❌ {ai['consecutive_failures']} in a row failed since "
                 f"{clock(ai['failing_since'])}: {esc(ai['last_error'])}"]
        if ai["last_success_at"]:
            lines.append(f"Last success: {clock(ai['last_success_at'])}")
        if ai["down_notified_at"]:
            lines.append(f"🚨 Admins alerted at {clock(ai['down_notified_at'])}")
        return lines
    if ai["last_success_at"]:
        return [f"Recent requests: ✅ the last one got an answer at {clock(ai['last_success_at'])}"]
    return ["Recent requests: none recorded yet"]


def _per_assessment(rows: list[dict[str, Any]], status: str) -> tuple[int, str]:
    rows = [r for r in rows if r["status"] == status]
    shown = ", ".join(f"#{r['assessment_id']}: {r['n']}" for r in rows[:5])
    return sum(r["n"] for r in rows), shown + (", …" if len(rows) > 5 else "")


def _uptime(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours < 48 else f"{hours // 24} days"


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """What needs the admin's attention: grading, sending, and the AI provider."""
    if not await _require_admin(update, context):
        return
    svc = services(context)
    message = await update.effective_message.reply_text("🩺 Checking…")
    overview, ai_now = svc.db.status_overview(work.grading_lease(svc)), await _check_ai(svc)

    lines = ["🩺 <b>Bot status</b>", "",
             f"<b>AI provider</b> · {esc(svc.settings.ai_provider)} / {esc(svc.ai.provider.model)}",
             f"Now: {ai_now}", *_ai_history(overview["ai"]), "", "<b>Grading</b>"]
    grading = []
    if overview["grading_now"]:
        grading.append(f"⏳ {overview['grading_now']} being graded now")
    if overview["interrupted"]:
        grading.append(f"🔁 {overview['interrupted']} interrupted; graded again automatically")
    failed = overview["failed_grading"]
    if failed:
        grading.append(f"❌ {sum(r['n'] for r in failed)} failed.")
        if overview["grading_error"]:
            grading.append(f"   Latest error: {esc(overview['grading_error'])}")
    if overview["results_unsent"]:
        grading.append(f"📨 {overview['results_unsent']} result(s) not delivered yet; sent again automatically")
    lines += grading or ["✅ Nothing waiting"]

    lines += ["", "<b>Sending assessments</b>"]
    sending = []
    queued, where = _per_assessment(overview["deliveries"], "pending")
    if queued:
        sending.append(f"⏳ {queued} message(s) still to send ({where})")
    failed_sends, where = _per_assessment(overview["deliveries"], "failed")
    if failed_sends:
        sending.append(f"❌ {failed_sends} could not be sent ({where})")
        if overview["delivery_error"]:
            sending.append(f"   Latest error: {esc(overview['delivery_error'])}")
    if overview["blocked_users"]:
        sending.append(f"🚫 {overview['blocked_users']} user(s) have blocked the bot")
    lines += sending or ["✅ Nothing queued or failed"]

    body, markup = guidance.render(svc.settings, update.effective_user.id, "\n".join(lines),
                                   guidance.regrade_suggestions(svc.db))  # only failed grading needs the admin
    mode = "webhook" if svc.settings.webhook_url else "polling"
    body += f"\n\n<i>Answered by a process running for {_uptime(time.monotonic() - svc.started_at)} ({mode}).</i>"
    await message.edit_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)


def register(application: Application) -> None:
    application.add_handler(CommandHandler("generate", generate, filters=PRIVATE))
    application.add_handler(CommandHandler("assessments", assessments, filters=PRIVATE))
    application.add_handler(CommandHandler("results", results, filters=PRIVATE))
    application.add_handler(CommandHandler("close", close, filters=PRIVATE))
    application.add_handler(CommandHandler("deadline", deadline, filters=PRIVATE))
    application.add_handler(CommandHandler("settings", settings, filters=PRIVATE))
    application.add_handler(CommandHandler("regrade", regrade, filters=PRIVATE))
    application.add_handler(CommandHandler("users", users, filters=PRIVATE))
    application.add_handler(CommandHandler("status", status, filters=PRIVATE))
    application.add_handler(CallbackQueryHandler(on_draft_button, pattern=r"^(pub|regen|del):\d+$"))
    application.add_handler(CallbackQueryHandler(on_option_button, pattern=r"^opt:\d+:(show_answers|allow_resubmission):[01]$"))
