"""/checkins and its buttons: a student turns study check-ins on, off, or pauses them (bot/checkins.py).

Buttons (`chk:on`, `chk:off`, `chk:pause`) come with the offer made once at /start, with each
check-in, and with the /checkins status; they do exactly what the typed command does.
"""

from __future__ import annotations

from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from .. import checkins, deadlines, guidance
from ..commands import Suggestion
from ..utils import esc, services

PRIVATE = filters.ChatType.PRIVATE
CHOICES = {"on": "on", "yes": "on", "start": "on", "resume": "on",
           "off": "off", "no": "off", "stop": "off", "pause": "pause"}
BUTTONS = {"on": "✅ Turn on", "off": "🔕 Turn off", "pause": "⏸ Pause a week"}


def keyboard(*choices: str, labels: dict[str, str] | None = None) -> InlineKeyboardMarkup:
    labels = {**BUTTONS, **(labels or {})}
    return InlineKeyboardMarkup([[InlineKeyboardButton(labels[c], callback_data=f"chk:{c}") for c in choices]])


def offer(settings) -> tuple[str, InlineKeyboardMarkup]:
    """The question added to a student's first /start (HTML) and its buttons."""
    text = (f"\n\n📅 <b>Study check-ins</b>\nWould you like me to message you {checkins.every(settings.checkin_days)} "
            "to see how your studying is going? Reply to any check-in if you're stuck and I'll help. "
            "You can stop them at any time.")
    return text, keyboard("on", "off", labels={"on": "✅ Yes, check in on me", "off": "No thanks"})


def _schedule(settings) -> str:
    return f"{checkins.every(settings.checkin_days)} around {checkins.at_hour(settings)}"


def describe(settings, row: dict | None, now: datetime) -> tuple[str, InlineKeyboardMarkup]:
    """What /checkins shows: where the user's check-ins stand, and buttons to change it."""
    tz = settings.tz
    if row and row["status"] == "on" and row["paused_until"] and row["paused_until"] > now:
        return (f"⏸ Your study check-ins are paused until {esc(deadlines.format_moment(row['paused_until'], tz, now))}. "
                "They start again by themselves.", keyboard("on", "off", labels={"on": "▶️ Resume now"}))
    if row and row["status"] == "on":
        return (f"✅ Your study check-ins are on: {esc(_schedule(settings))}. "
                f"The next one: {esc(deadlines.format_moment(row['next_at'], tz, now))}.",
                keyboard("pause", "off"))
    return (f"🔕 Your study check-ins are off. When they are on, I message you {esc(_schedule(settings))} "
            "about what you're studying, and you can reply if you need help.", keyboard("on"))


async def apply(update: Update, context: ContextTypes.DEFAULT_TYPE, choice: str) -> None:
    svc = services(context)
    settings, user_id = svc.settings, update.effective_user.id
    now = datetime.now(timezone.utc)
    tz, hour = settings.tz, settings.checkin_hour
    row = svc.db.get_checkins(user_id)
    if choice == "on":
        next_at = checkins.slot(now, tz, hour, 1)
        svc.db.set_checkins(user_id, "on", next_at)
        text = (f"✅ Study check-ins are on. I'll message you {esc(_schedule(settings))}; the first one "
                f"{esc(deadlines.format_moment(next_at, tz, now))}. Reply to any of them when you need help.")
        await guidance.reply(update, context, text, [Suggestion("checkins", ("off",), why="To stop them")])
    elif choice == "off":
        svc.db.set_checkins(user_id, "off")
        await guidance.reply(update, context, "🔕 Study check-ins are off. I won't message you unless "
                             "there is an assessment or a result for you.",
                             [Suggestion("checkins", ("on",), why="To turn them back on")])
    elif row is None or row["status"] != "on":
        await guidance.reply(update, context, "Your study check-ins are off already, so there is nothing to pause.",
                             [Suggestion("checkins", ("on",), why="To turn them on")], kind=guidance.STATE)
    else:
        until = now + checkins.PAUSE
        next_at = checkins.slot(until, tz, hour, 0)
        svc.db.set_checkins(user_id, "on", next_at, paused_until=until)
        await guidance.reply(update, context,
                             f"⏸ Paused for a week. The next check-in: {esc(deadlines.format_moment(next_at, tz, now))}.",
                             [Suggestion("checkins", ("off",), why="To stop them for good")])


async def checkins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = services(context)
    if not svc.settings.checkin_days:
        await guidance.reply(update, context, "Study check-ins are switched off on this bot.", kind=guidance.STATE)
        return
    typed = " ".join(context.args or []).strip().lower()
    if not typed:
        text, markup = describe(svc.settings, svc.db.get_checkins(update.effective_user.id), datetime.now(timezone.utc))
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)
        return
    choice = CHOICES.get(typed)
    if choice is None:
        await guidance.reply(update, context, f"I can't use “{esc(typed)}” here. Choose one of:",
                             [Suggestion("checkins", (c,)) for c in ("on", "off", "pause")], kind=guidance.INPUT)
        return
    await apply(update, context, choice)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not services(context).settings.checkin_days:
        await query.answer("Study check-ins are switched off on this bot.", show_alert=True)
        return
    await query.answer()
    if query.message is None:  # too old for Telegram to show; nothing to reply to
        return
    if query.data != "chk:pause":  # pause stays on a check-in, so that the message keeps its other buttons
        try:
            await query.edit_message_reply_markup(None)  # the choice is made: these buttons are done
        except TelegramError:
            pass
    await apply(update, context, query.data.split(":")[1])


def register(application: Application) -> None:
    application.add_handler(CommandHandler("checkins", checkins_command, filters=PRIVATE))
    application.add_handler(CallbackQueryHandler(on_button, pattern=r"^chk:(on|off|pause)$"))
