"""Handle every Telegram update at most once.

Telegram sends an update again when the bot did not confirm it: the process crashed or was
redeployed right after fetching a batch, or two bot versions polled at the same time during a
deploy. Without a guard, the copy would be handled again: an answer saved twice, a second
"Send to all users", the same photo read and explained twice.

The first handler of all (group -2) claims the update's id in PostgreSQL. Only the process whose
insert succeeds goes on; every other copy stops here. Claiming happens before handling, so an
update whose handling was cut off by a crash is not repeated when Telegram resends it: the user
sends that message again, which is safer than applying half-done work twice.

Distinct updates that mean the same thing (a double tap on Submit sends two updates with
different ids) are a separate problem, handled where it matters by the database methods
themselves (see Database.submit_draft).
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, ApplicationHandlerStop, ContextTypes, TypeHandler

from ..utils import services

log = logging.getLogger(__name__)

# Telegram keeps an unconfirmed update for at most 24 hours, so after two days a copy can no
# longer arrive and the record can go.
RETENTION_HOURS = 48
PRUNE_EVERY = 1000  # claims between two clean-ups


async def claim_update(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not isinstance(update, Update):
        return
    db = services(context).db
    try:
        first = db.claim_update(context.bot.id, update.update_id)
    except Exception:
        # Better to risk handling a rare copy twice than to drop every update while the
        # database is unreachable; the handlers report their own database errors.
        log.warning("Could not record update %s; handling it anyway", update.update_id, exc_info=True)
        return
    if not first:
        log.info("Update %s was already handled; ignoring the copy", update.update_id)
        raise ApplicationHandlerStop

    claims = context.bot_data["update_claims"] = context.bot_data.get("update_claims", 0) + 1
    if claims % PRUNE_EVERY == 0:
        prune(db)


def prune(db) -> None:
    try:
        removed = db.prune_processed_updates(RETENTION_HOURS)
    except Exception:
        log.warning("Could not clean up old update records", exc_info=True)
        return
    if removed:
        log.info("Forgot %s update records older than %s hours", removed, RETENTION_HOURS)


def register(application: Application) -> None:
    application.add_handler(TypeHandler(Update, claim_update), group=-2)
