"""Shared helpers: access to app services, message formatting and safe sending."""

from __future__ import annotations

import asyncio
import html
import re
import time
from dataclasses import dataclass, field
from typing import Any

from telegram import Bot, Message
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .ai import AssessmentAI
from .ai.service import LETTERS
from .config import Settings
from .db import Database
from .ocr import OCRService

MESSAGE_LIMIT = 4000  # Telegram's limit is 4096; leave headroom


@dataclass
class Services:
    settings: Settings
    db: Database
    ai: AssessmentAI
    ocr: OCRService
    # Set when the bot starts shutting down: slow work finishes the item in hand and takes no
    # new one, so nothing is cut off halfway (the next process picks up what is left).
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: float = field(default_factory=time.monotonic)  # for /status


def services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.application.bot_data["services"]


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def num(value: float | int | None) -> str:
    """7.0 -> '7', 2.5 -> '2.5'."""
    return f"{value:g}" if value is not None else "–"


def percent(score: float | None, maximum: float | None) -> str:
    if score is None or not maximum:
        return "–"
    return f"{round(100 * score / maximum)}%"


def pack_blocks(blocks: list[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """Join HTML blocks into as few messages as possible without splitting a block.

    A single block longer than the limit is sent as plain text pieces, because
    cutting through HTML tags would make Telegram reject the message.
    """
    messages: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > limit:
            if current:
                messages.append(current)
                current = ""
            plain = esc(html.unescape(re.sub(r"<[^>]+>", "", block)))
            messages.extend(plain[i: i + limit] for i in range(0, len(plain), limit))
            continue
        joined = f"{current}\n\n{block}" if current else block
        if len(joined) > limit:
            messages.append(current)
            current = block
        else:
            current = joined
    if current:
        messages.append(current)
    return messages


async def send_blocks(bot: Bot, chat_id: int, blocks: list[str], reply_markup=None) -> list[Message]:
    """Send blocks as one or more HTML messages; the keyboard goes on the last one."""
    texts = pack_blocks(blocks)
    sent = []
    for index, text in enumerate(texts):
        sent.append(await bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup if index == len(texts) - 1 else None,
            disable_web_page_preview=True,
        ))
    return sent


def question_blocks(questions: list[dict[str, Any]], with_answers: bool) -> list[str]:
    blocks = []
    for q in questions:
        points = f"{num(q['points'])} pt" + ("" if q["points"] == 1 else "s")
        block = f"<b>{q['position']}.</b> {esc(q['text'])} <i>({points})</i>"
        if q["options"]:
            block += "\n" + "\n".join(
                f"   {LETTERS[i]}) {esc(option)}" for i, option in enumerate(q["options"])
            )
        if with_answers:
            block += f"\n<i>Answer: {esc(q['answer'])}</i>"
        blocks.append(block)
    return blocks


def total_points(questions: list[dict[str, Any]]) -> float:
    return sum(q["points"] for q in questions)


def display_name(row: dict[str, Any]) -> str:
    name = row.get("full_name") or "Unknown"
    return f"{name} (@{row['username']})" if row.get("username") else name
