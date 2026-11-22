"""/explain: a student sends something they do not understand and gets it explained."""

from __future__ import annotations

import logging

from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from ..ai import AIError, AIUnavailable
from ..utils import esc, send_blocks, services
from .reading import read_upload

log = logging.getLogger(__name__)

PRIVATE = filters.ChatType.PRIVATE
MAX_EXPLAIN_CHARS = 20_000
USAGE = (
    "💡 Need an explanation? Send a photo or file with the caption /explain "
    "(you can add a question after it), or reply /explain to a message, photo or file.\n"
    "You can also just ask: /explain what is osmosis?"
)


def caption_question(caption: str | None) -> str | None:
    """The question in a caption such as '/explain why is this negative?'; None if it is not /explain."""
    words = (caption or "").strip().split(maxsplit=1)
    if not words or words[0].lower().split("@")[0] != "/explain":
        return None
    return words[1] if len(words) > 1 else ""


async def explain_upload(
    messages: list[Message], context: ContextTypes.DEFAULT_TYPE, question: str | None,
) -> None:
    """Read the photo(s) or file and explain what is in them."""
    if not await _allowed(messages[0], context):
        return
    # Explaining one picture well matters more than speed: always let the vision model look.
    document = await read_upload(messages, context, force_vision=True)
    if document is not None:
        await _explain(messages[0], context, document.text, question)


async def explain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    question = " ".join(context.args) or None
    source = message.reply_to_message
    if not await _allowed(message, context):
        return
    if source and (source.photo or source.document):
        document = await read_upload([source], context, force_vision=True)
        if document is not None:
            await _explain(message, context, document.text, question)
    elif source and (source.text or source.caption):
        await _explain(message, context, source.text or source.caption, question)
    elif question:
        await _explain(message, context, question, None)
    else:
        await message.reply_text(USAGE)


async def _allowed(message: Message, context: ContextTypes.DEFAULT_TYPE) -> bool:
    svc = services(context)
    if svc.settings.allow_explain or svc.settings.is_admin(message.from_user.id):
        return True
    await context.bot.send_message(message.chat_id, "💡 Explanations aren't available right now. Please contact an administrator if you need help.")
    return False


async def _explain(message: Message, context: ContextTypes.DEFAULT_TYPE, material: str, question: str | None) -> None:
    svc = services(context)
    chat_id = message.chat_id
    if context.user_data.get("explaining"):
        await context.bot.send_message(chat_id, "⏳ I'm still working on your previous question. Please send the next one after it finishes.")
        return
    context.user_data["explaining"] = True
    try:
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        explanation = await svc.ai.explain(material[:MAX_EXPLAIN_CHARS], question)
    except AIError as exc:
        log.error("Explanation failed: %s", exc)
        if isinstance(exc, AIUnavailable):  # temporary: the same request may work later
            text = "⏳ I couldn't explain that just now: my AI is not answering. Please try again in a few minutes."
        else:  # the AI answered, but not usefully: asking differently helps more than waiting
            text = "⚠️ I couldn't explain that just now. Try again, or ask about one part at a time."
        await context.bot.send_message(chat_id, text)
        return
    finally:
        context.user_data["explaining"] = False
    await send_blocks(context.bot, chat_id, [f"💡 {esc(explanation)}"])


def register(application: Application) -> None:
    application.add_handler(CommandHandler("explain", explain_command, filters=PRIVATE))
