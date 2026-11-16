"""Entry point for every file and photo: decide what the sender wants done with it."""

from __future__ import annotations

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ..utils import services
from . import admin, explain, student
from .reading import gather_album

PRIVATE = filters.ChatType.PRIVATE


async def on_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A caption starting with /explain asks for an explanation. Otherwise a file from an
    admin is study material, and from anyone else it is their answers."""
    messages = await gather_album(update.effective_message, context)
    if messages is None:
        return  # part of an album that is handled as one batch
    caption = next((m.caption for m in messages if m.caption), None)
    question = explain.caption_question(caption)
    if question is not None:
        await explain.explain_upload(messages, context, question)
    elif services(context).settings.is_admin(update.effective_user.id):
        await admin.on_material(messages, context, caption)
    else:
        await student.on_answer_upload(update, context, messages, caption)


def register(application: Application) -> None:
    application.add_handler(MessageHandler(PRIVATE & (filters.Document.ALL | filters.PHOTO), on_upload))
