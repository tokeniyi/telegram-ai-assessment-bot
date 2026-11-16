"""Getting text out of what a user sent: download, OCR with a progress note, album grouping."""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Message
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..extract import ExtractionError
from ..ocr import DocumentResult, describe_result
from ..ocr.detector import file_kind
from ..utils import esc, services

log = logging.getLogger(__name__)

ALBUM_WAIT = 1.5      # seconds to wait for the remaining photos of an album
PROGRESS_EVERY = 2.5  # seconds between edits of the progress note (Telegram rate limits edits)
ECHO_CHARS = 1500     # how much of the text read from a photo is shown back to the sender


def is_image(message: Message) -> bool:
    if message.photo:
        return True
    document = message.document
    return bool(document) and file_kind(document.file_name, b"", document.mime_type) == "image"


async def gather_album(message: Message, context: ContextTypes.DEFAULT_TYPE) -> list[Message] | None:
    """Photos sent together arrive as separate messages; hand them back as one batch.

    Returns the messages to process now, or None when this message joined a batch
    that another handler call is already waiting for.
    """
    if not message.media_group_id or not is_image(message):
        return [message]
    albums = context.application.bot_data.setdefault("albums", {})
    key = (message.chat_id, message.media_group_id)
    if key in albums:
        albums[key].append(message)
        return None
    albums[key] = [message]
    await asyncio.sleep(ALBUM_WAIT)
    return sorted(albums.pop(key), key=lambda m: m.message_id)


async def _download(message: Message, max_mb: int) -> tuple[str | None, bytes, str | None]:
    if message.photo:
        target, name, mime = message.photo[-1], "photo.jpg", "image/jpeg"  # the largest size
    else:
        target, name, mime = message.document, message.document.file_name, message.document.mime_type
    if target.file_size and target.file_size > max_mb * 1024 * 1024:
        raise ExtractionError(f"That file is larger than {max_mb} MB.")
    file = await target.get_file()
    return name, bytes(await file.download_as_bytearray()), mime


class ProgressNote:
    """One chat message that follows a long OCR job. Nothing is sent for files that need no OCR."""

    def __init__(self, bot, chat_id: int):
        self._bot, self._chat_id = bot, chat_id
        self._message: Message | None = None
        self._last = 0.0
        self._text = ""

    async def update(self, stage: str, done: int, total: int) -> None:
        if stage == "ocr":
            text = (f"🔎 Reading the scanned page{'s' if total != 1 else ''}… {done}/{total}"
                    if total > 1 else "🔎 Reading the text in the image…")
        else:
            text = f"🤖 The AI is reading the difficult page{'s' if total != 1 else ''}… {done}/{total}"
        if text == self._text or (self._message and done < total and time.monotonic() - self._last < PROGRESS_EVERY):
            return
        await self._show(text)

    async def _show(self, text: str, html: bool = False) -> None:
        self._text, self._last = text, time.monotonic()
        try:
            if self._message is None:
                self._message = await self._bot.send_message(self._chat_id, text, parse_mode="HTML" if html else None)
            else:
                await self._message.edit_text(text, parse_mode="HTML" if html else None)
        except TelegramError as exc:  # a failed progress update must never fail the job
            log.debug("Progress note not updated: %s", exc)

    async def finish(self, text: str | None) -> None:
        """Replace the note with the final summary (HTML), or remove it when there is nothing to say."""
        if text:
            await self._show(text, html=True)
        elif self._message is not None:
            try:
                await self._message.delete()
            except TelegramError:
                pass


def summary(document: DocumentResult) -> str | None:
    """What the uploader should know about how the file was read (HTML)."""
    line = describe_result(document)
    parts = [esc(line)] if line else []
    parts += [f"⚠️ {esc(warning)}" for warning in document.warnings]
    return "\n".join(parts) or None


async def read_upload(
    messages: list[Message], context: ContextTypes.DEFAULT_TYPE, *, force_vision: bool = False,
) -> DocumentResult | None:
    """Download and read one file or an album of images.

    Tells the user what went wrong and returns None on failure. For scans and
    photos a progress note is shown and ends as a summary of how well it was read.
    """
    svc = services(context)
    message = messages[0]
    chat_id, user_id = message.chat_id, message.from_user.id
    if context.user_data.get("reading"):
        await context.bot.send_message(chat_id, "⏳ I am still reading your previous file. Send this one again in a moment.")
        return None
    context.user_data["reading"] = True
    note = ProgressNote(context.bot, chat_id)
    max_pages = svc.settings.ocr.max_pages if svc.settings.is_admin(user_id) else svc.settings.ocr.max_pages_user
    try:
        files = [await _download(m, svc.settings.max_file_mb) for m in messages]
        if len(files) == 1:
            name, data, mime = files[0]
            document = await svc.ocr.extract(name, data, mime, progress=note.update, max_pages=max_pages,
                                             force_vision=force_vision)
        else:
            document = await svc.ocr.extract_images([data for _, data, _ in files], progress=note.update,
                                                    max_pages=max_pages, force_vision=force_vision)
    except ExtractionError as exc:
        await note.finish(None)
        await context.bot.send_message(chat_id, f"❌ {exc}")
        return None
    except TelegramError as exc:
        await note.finish(None)
        await context.bot.send_message(chat_id, f"❌ Could not download the file: {exc}")
        return None
    finally:
        context.user_data["reading"] = False
    if document.used_ocr:
        log.info("OCR for user %s: %s", user_id, document.report())
    await note.finish(summary(document))
    return document


def echo_block(document: DocumentResult) -> str:
    """The text read from a photo, shown back so that the sender can check it."""
    text = document.text
    shown = text[:ECHO_CHARS] + ("…" if len(text) > ECHO_CHARS else "")
    return f"📷 <b>This is what I read:</b>\n<pre>{esc(shown)}</pre>"
