"""Unknown commands and command buttons (Stage 6).

* An unknown or mistyped command gets an answer instead of silence: the closest commands the
  sender may use, or /help.
* A button made from a `Suggestion` (callback data `cmd:<name>[:<number>]`) runs the very
  handler registered for that command, with the number as its argument. The handler makes its
  usual permission and state checks, so a button can do nothing the typed command could not.
  Only commands marked `button=True` in the registry can be run this way.

Registered last, after every other handler, so that it can find the command handlers and only
sees commands nobody else handled.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .. import commands, guidance
from ..commands import Suggestion
from ..utils import esc, services

log = logging.getLogger(__name__)

PRIVATE = filters.ChatType.PRIVATE


def command_callbacks(application: Application) -> dict[str, object]:
    """The callback of every CommandHandler registered, by command name."""
    found = {}
    for handlers in application.handlers.values():
        for handler in handlers:
            if isinstance(handler, CommandHandler):
                for name in handler.commands:
                    found.setdefault(name, handler.callback)
    return found


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    user_id = update.effective_user.id
    typed = update.effective_message.text.split()[0]
    matches = commands.closest(typed, svc.settings, user_id)
    if not matches:
        await guidance.reply(update, context, f"I don't know {esc(typed)}.",
                             [Suggestion("help", why="Everything you can do")], kind=guidance.INPUT)
        return
    suggestions, lines = [], []
    for command in matches:
        if not any(a.required for a in command.args):
            suggestions.append(Suggestion(command.name, why=esc(command.summary)))
            continue
        # A command that needs an assessment: show it with an id it can act on right now.
        real = guidance.assessment_choices(svc.db, command.name, limit=1)
        if real:
            suggestions.append(Suggestion(command.name, real[0].args, why=esc(command.summary)))
        else:
            lines.append(f"{esc(command.usage)} — {esc(command.summary)} (nothing needs it right now)")
    text = "\n".join([f"I don't know {esc(typed)}. Did you mean:", *lines])
    await guidance.reply(update, context, text, suggestions, kind=guidance.INPUT)


async def on_command_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, name, *args = query.data.split(":")
    command = commands.get(name)
    callback = context.application.bot_data.get("command_callbacks", {}).get(name)
    if command is None or not command.button or callback is None:
        await query.answer("This button no longer works. Send /help to see what you can do.", show_alert=True)
        return
    if query.message is None or query.message.chat.type != ChatType.PRIVATE:
        await query.answer()
        return
    if not command.allowed(services(context).settings, update.effective_user.id):
        await query.answer("🔒 This is for administrators only.", show_alert=True)
        return
    await query.answer()
    context.args = args
    await callback(update, context)  # the command's own checks apply, as when it is typed


def register(application: Application) -> None:
    callbacks = command_callbacks(application)
    application.bot_data["command_callbacks"] = callbacks
    # Any cmd: button, so that one from an older version (or for a command no longer allowed on a
    # button) gets an answer instead of a spinner; on_command_button checks the registry.
    application.add_handler(CallbackQueryHandler(on_command_button, pattern=r"^cmd:[a-z]+(:\d+)?$"))
    application.add_handler(MessageHandler(PRIVATE & filters.COMMAND, unknown_command))
