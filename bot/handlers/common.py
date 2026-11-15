"""Handlers shared by everyone: user tracking, /start, /help, /myid."""

from __future__ import annotations

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, TypeHandler, filters

from ..utils import services

PRIVATE = filters.ChatType.PRIVATE

USER_HELP = (
    "<b>How it works</b>\n"
    "1. When an assessment is published you receive its questions here.\n"
    "2. Reply with your answers in any format — numbered, one per line, or a single paragraph. "
    "You can send several messages, or a text/PDF/DOCX file.\n"
    "3. Tap <b>Submit</b>. Your answers are graded and you get your score and feedback.\n\n"
    "<b>Commands</b>\n"
    "/pending — assessments waiting for your answers\n"
    "/myresults — your scores\n"
    "/cancel — discard the answers you have typed so far\n"
    "/myid — show your Telegram ID"
)

ADMIN_HELP = (
    "\n\n<b>Admin</b>\n"
    "Send a PDF, DOCX or TXT file and I generate questions from it. Add a caption to steer the "
    "result, e.g. <i>10 multiple-choice questions on chapter 2, hard</i>. You preview the draft, "
    "then send it to all users.\n"
    "/generate — reply with this to a text message to use that text as the material\n"
    "/assessments — list assessments\n"
    "/results [id] — scores and CSV export (latest assessment if no id)\n"
    "/close &lt;id&gt; — stop accepting answers\n"
    "/regrade &lt;id&gt; — grade submissions whose grading failed\n"
    "/users — number of registered users"
)


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remember everyone who talks to the bot privately; they receive assessments."""
    user, chat = update.effective_user, update.effective_chat
    if user and chat and chat.type == ChatType.PRIVATE and not user.is_bot:
        services(context).db.upsert_user(user.id, user.username, user.full_name)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_admin = services(context).settings.is_admin(update.effective_user.id)
    greeting = f"👋 Hi {update.effective_user.first_name}! You are registered"
    greeting += " as an <b>administrator</b>.\n\n" if is_admin else " and will receive assessments here.\n\n"
    await update.effective_message.reply_text(
        greeting + USER_HELP + (ADMIN_HELP if is_admin else ""), parse_mode=ParseMode.HTML
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_admin = services(context).settings.is_admin(update.effective_user.id)
    await update.effective_message.reply_text(
        USER_HELP + (ADMIN_HELP if is_admin else ""), parse_mode=ParseMode.HTML
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    note = "You are an administrator." if services(context).settings.is_admin(user_id) else (
        "To become an administrator, add this number to ADMIN_IDS in the bot's .env file and restart it."
    )
    await update.effective_message.reply_text(
        f"Your Telegram ID is <code>{user_id}</code>\n{note}", parse_mode=ParseMode.HTML
    )


def register(application: Application) -> None:
    application.add_handler(TypeHandler(Update, track_user), group=-1)
    application.add_handler(CommandHandler("start", start, filters=PRIVATE))
    application.add_handler(CommandHandler("help", help_command, filters=PRIVATE))
    application.add_handler(CommandHandler("myid", my_id, filters=PRIVATE))
