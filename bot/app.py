"""Builds the Telegram application from settings."""

from __future__ import annotations

import logging

from telegram import BotCommandScopeChat, Update
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder, ContextTypes

from . import commands
from .ai import AssessmentAI, build_provider
from .alerts import AIMonitor
from .config import Settings
from .db import Database
from .handlers import register_handlers
from .handlers import work
from .handlers.dedupe import prune as prune_update_records
from .ocr import OCRService
from .utils import Services

log = logging.getLogger(__name__)

async def _post_init(application: Application) -> None:
    services: Services = application.bot_data["services"]
    # Both menus come from the command registry (bot/commands.py).
    await application.bot.set_my_commands(commands.menu(admin=False, settings=services.settings))
    for admin_id in services.settings.admin_ids:
        try:
            await application.bot.set_my_commands(
                commands.menu(admin=True, settings=services.settings), scope=BotCommandScopeChat(admin_id)
            )
        except TelegramError:
            # Telegram refuses this until the admin has started the bot once.
            log.info("Admin %s has not started the bot yet; admin command menu not set", admin_id)
    me = await application.bot.get_me()
    log.info("Bot @%s is running. AI: %s / %s. Admins: %s",
             me.username, services.settings.ai_provider, services.settings.ai_model,
             sorted(services.settings.admin_ids) or "none")
    prune_update_records(services.db)
    # Finish sending and grading that a restart interrupted, then keep checking for leftovers.
    work.start(application)
    log.info(services.ocr.describe())
    services.ocr.warm_up()
    if not services.settings.admin_ids:
        log.warning("ADMIN_IDS is empty: nobody can upload material. Send /myid to the bot, "
                    "put the number in ADMIN_IDS in .env and restart.")


async def _post_stop(application: Application) -> None:
    await work.stop(application)


async def _post_shutdown(application: Application) -> None:
    await work.stop(application)  # also when start-up failed after post_init, so post_stop never ran
    application.bot_data["services"].ocr.shutdown()
    application.bot_data["services"].db.close()


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ Something went wrong on my side. Please try again. If it keeps happening, "
                "/help lists what I can do."
            )
        except TelegramError:
            pass


def build_application(settings: Settings, builder: ApplicationBuilder | None = None) -> Application:
    builder = builder or ApplicationBuilder()
    if settings.telegram_api_url:  # a local Bot API server (https://github.com/tdlib/telegram-bot-api)
        builder = (builder.base_url(f"{settings.telegram_api_url}/bot")
                   .base_file_url(f"{settings.telegram_api_url}/file/bot"))
    application = (
        builder.token(settings.telegram_bot_token)
        .concurrent_updates(True)  # an AI call for one user must not block everyone else
        .post_init(_post_init)
        .post_stop(_post_stop)
        .post_shutdown(_post_shutdown)
        .build()
    )
    ai = AssessmentAI(build_provider(settings), settings.default_num_questions, settings.max_questions)
    services = application.bot_data["services"] = Services(
        settings=settings,
        db=Database(settings.database_url),
        ai=ai,
        # the provider is looked up on every use, so that it can be swapped (tests do)
        ocr=OCRService(settings.ocr, get_provider=lambda: ai.provider),
    )
    ai.observer = AIMonitor(services, application.bot)  # alerts the admins when the AI is down
    register_handlers(application)
    application.add_error_handler(_on_error)
    return application
