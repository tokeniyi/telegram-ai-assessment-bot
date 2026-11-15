"""Builds the Telegram application from settings."""

from __future__ import annotations

import logging

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder, ContextTypes

from .ai import AssessmentAI, build_provider
from .config import Settings
from .db import Database
from .handlers import register_handlers
from .utils import Services

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand("start", "Register and see how the bot works"),
    BotCommand("pending", "Assessments waiting for your answers"),
    BotCommand("myresults", "Your scores"),
    BotCommand("cancel", "Discard the answers typed so far"),
    BotCommand("help", "Help"),
]
ADMIN_COMMANDS = [
    BotCommand("assessments", "List assessments"),
    BotCommand("results", "Scores and CSV export"),
    BotCommand("generate", "Reply to a text message to use it as material"),
    BotCommand("close", "Stop accepting answers for an assessment"),
    BotCommand("regrade", "Retry failed grading"),
    BotCommand("users", "Number of registered users"),
]


async def _post_init(application: Application) -> None:
    services: Services = application.bot_data["services"]
    await application.bot.set_my_commands(USER_COMMANDS)
    for admin_id in services.settings.admin_ids:
        try:
            await application.bot.set_my_commands(
                USER_COMMANDS + ADMIN_COMMANDS, scope=BotCommandScopeChat(admin_id)
            )
        except TelegramError:
            # Telegram refuses this until the admin has started the bot once.
            log.info("Admin %s has not started the bot yet; admin command menu not set", admin_id)
    me = await application.bot.get_me()
    log.info("Bot @%s is running. AI: %s / %s. Admins: %s",
             me.username, services.settings.ai_provider, services.settings.ai_model,
             sorted(services.settings.admin_ids) or "none")
    if not services.settings.admin_ids:
        log.warning("ADMIN_IDS is empty: nobody can upload material. Send /myid to the bot, "
                    "put the number in ADMIN_IDS in .env and restart.")


async def _post_shutdown(application: Application) -> None:
    application.bot_data["services"].db.close()


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id, "⚠️ Something went wrong on my side. Please try again."
            )
        except TelegramError:
            pass


def build_application(settings: Settings, builder: ApplicationBuilder | None = None) -> Application:
    builder = builder or ApplicationBuilder()
    application = (
        builder.token(settings.telegram_bot_token)
        .concurrent_updates(True)  # an AI call for one user must not block everyone else
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["services"] = Services(
        settings=settings,
        db=Database(settings.database_path),
        ai=AssessmentAI(build_provider(settings), settings.default_num_questions, settings.max_questions),
    )
    register_handlers(application)
    application.add_error_handler(_on_error)
    return application
