"""Handlers shared by everyone: user tracking, /start, /help, /myid."""

from __future__ import annotations

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, TypeHandler, filters

from ..utils import services

PRIVATE = filters.ChatType.PRIVATE

USER_HELP = (
    "<b>📚 Student guide</b>\n"
    "When an admin publishes an assessment, its questions will appear in this chat.\n\n"
    "<b>How to answer</b>\n"
    "1. Open /pending to find an assessment.\n"
    "2. Send answers as text, a PDF/DOCX file, or photos of your work. You can send more than one message.\n"
    "3. Tap <b>✅ Submit</b> when finished to receive your score and feedback.\n\n"
    "<b>Need help?</b> Ask a question in ordinary language, or use /explain for a photo or file. "
    "If I accidentally save a chat message as an answer, tap <b>Undo</b>.\n\n"
    "<b>Useful commands</b>\n"
    "/pending — View assessments you can answer\n"
    "/myresults — View your grades and feedback scores\n"
    "/cancel — Discard your unsent answers\n"
    "/explain — Explain a question, photo or document\n"
    "/myid — Show your Telegram ID"
)

ADMIN_HELP = (
    "<b>🛠 Administrator guide</b>\n"
    "Create an assessment by sending a PDF, DOCX, TXT, scanned document, or photos of notes. "
    "For multiple pages, send photos in one album. For small print, attach an image as a file.\n\n"
    "<b>Quick steps</b>\n"
    "1. Upload your teaching material. Add a caption such as "
    "<i>10 multiple-choice questions from chapter 2, hard</i> to guide question generation.\n"
    "2. Review the generated draft and reference answers.\n"
    "3. Choose <b>Send to all users</b>, <b>Regenerate</b>, or <b>Discard</b>.\n\n"
    "<b>Admin commands</b>\n"
    "/generate — Reply to pasted notes to generate questions\n"
    "/assessments — View drafts and published assessments\n"
    "/results [id] — View grades and download CSV (latest by default)\n"
    "/close &lt;id&gt; — Close an assessment to new answers\n"
    "/regrade &lt;id&gt; — Retry failed grading\n"
    "/users — Check registered user count"
)


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remember everyone who talks to the bot privately; they receive assessments."""
    user, chat = update.effective_user, update.effective_chat
    if user and chat and chat.type == ChatType.PRIVATE and not user.is_bot:
        services(context).db.upsert_user(user.id, user.username, user.full_name)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_admin = services(context).settings.is_admin(update.effective_user.id)
    if is_admin:
        greeting = (
            f"👋 Welcome, {update.effective_user.first_name}! <b>Administrator access is active.</b>\n"
            "Start by uploading notes or a document to create an assessment.\n\n"
        )
        guide = ADMIN_HELP + "\n\n<b>Also answering assessments?</b> Use /pending or /help."
    else:
        greeting = (
            f"👋 Welcome, {update.effective_user.first_name}! You're ready to receive assessments.\n"
            "Start with /pending to see if there is one to answer.\n\n"
        )
        guide = USER_HELP
    await update.effective_message.reply_text(
        greeting + guide, parse_mode=ParseMode.HTML
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_admin = services(context).settings.is_admin(update.effective_user.id)
    await update.effective_message.reply_text(
        (ADMIN_HELP + "\n\n" + USER_HELP) if is_admin else USER_HELP, parse_mode=ParseMode.HTML
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    note = "You are an administrator." if services(context).settings.is_admin(user_id) else (
        "To request administrator access, share this ID with the bot owner. They must add it to the bot configuration."
    )
    await update.effective_message.reply_text(
        f"Your Telegram ID is <code>{user_id}</code>\n{note}", parse_mode=ParseMode.HTML
    )


def register(application: Application) -> None:
    application.add_handler(TypeHandler(Update, track_user), group=-1)
    application.add_handler(CommandHandler("start", start, filters=PRIVATE))
    application.add_handler(CommandHandler("help", help_command, filters=PRIVATE))
    application.add_handler(CommandHandler("myid", my_id, filters=PRIVATE))
