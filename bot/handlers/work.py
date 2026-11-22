"""Slow work that must survive a restart: sending a published assessment and grading.

Both are driven from rows in PostgreSQL rather than from the handler that started them:

* Publishing writes one `deliveries` row per user (Database.publish_assessment). Whoever sends
  takes one row at a time with a lease, sends, and marks it. A restart leaves the unsent rows,
  and the next process finishes them.
* A submission is leased to the process that grades it (Database.submit_draft). If that process
  stops, the lease runs out and the submission is graded again, at most MAX_GRADING_ATTEMPTS
  times, after which it is marked 'failed' for /regrade.

`run_forever` runs on start and then every WORK_INTERVAL seconds, so leftovers are picked up
without an admin doing anything. The handlers call the same functions for the work they start.

Leases make each piece of work owned by one process at a time, including while two bot versions
run during a deploy. An assessment message is retried only when Telegram certainly did not get it
(not after a timeout, and not once part of it was sent), so the one way a user can receive an
assessment twice is a process stopping between sending it and marking it sent, after which the row
is sent again once its lease runs out. Results are the other way round: a result that may not have
arrived is sent once more, because a lost result is worse than a repeated one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application

from .. import guidance
from ..ai import AIError
from ..alerts import report_grading_failures
from ..logs import event
from ..utils import Services, pack_blocks, send_blocks, total_points
from . import student

log = logging.getLogger(__name__)

WORK_INTERVAL = 30           # seconds between two checks for leftover work
SEND_DELAY = 0.05            # seconds between messages; well under Telegram's ~30 msg/s limit
DELIVERY_LEASE = 300         # seconds one process may take to send one user's message
DELIVERY_RETRY_DELAY = 60    # seconds before retrying a send that failed for a passing reason
MAX_DELIVERY_ATTEMPTS = 3
MAX_GRADING_ATTEMPTS = 3     # grading runs that may be cut short before a submission is 'failed'
RESULT_GRACE = 300           # seconds a graded result may stay unsent before another process sends it


def grading_lease(services: Services) -> float:
    """How long one grading run may take before another process may assume it was abandoned.

    Longer than the AI call can take, including the SDKs' own retries, so a slow but live
    grading run is never duplicated.
    """
    return max(600.0, 4 * services.settings.ai_timeout_seconds)


# ── sending a published assessment ───────────────────────────────────────
class PartlySent(Exception):
    """Some of the messages of one assessment reached the user; sending again would repeat them."""


async def _send_once(bot: Bot, user_id: int, blocks: list[str]) -> None:
    """Send the assessment's messages, waiting once if Telegram asks to slow down."""
    texts = pack_blocks(blocks)
    for index, text in enumerate(texts):
        for attempt in (1, 2):
            try:
                await bot.send_message(user_id, text, parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)
                break
            except RetryAfter as exc:
                if attempt == 2:
                    raise PartlySent(f"part {index + 1} of {len(texts)}: {exc}") if index else exc
                delay = exc.retry_after
                await asyncio.sleep(delay.total_seconds() if hasattr(delay, "total_seconds") else delay)
            except TelegramError as exc:
                if index and not isinstance(exc, Forbidden):
                    raise PartlySent(f"part {index + 1} of {len(texts)}: {exc}") from exc
                raise


async def deliver(bot: Bot, svc: Services, assessment_id: int | None = None) -> int:
    """Send every queued assessment message that no other process is sending (only those of
    `assessment_id` if given). Returns how many were attempted."""
    db = svc.db
    blocks_cache: dict[int, list[str]] = {}
    attempted = 0
    while not svc.stopping.is_set():  # when stopping, the next process sends the rest
        row = db.claim_delivery(DELIVERY_LEASE, assessment_id)
        if row is None:
            break
        attempted += 1
        aid, user_id = row["assessment_id"], row["user_id"]
        if aid not in blocks_cache:
            blocks_cache[aid] = student.assessment_blocks(db.get_assessment(aid), db.get_questions(aid))
        fields = dict(assessment=aid, user=user_id, attempt=row["attempts"])
        try:
            await _send_once(bot, user_id, blocks_cache[aid])
        except Forbidden:
            db.set_user_active(user_id, False)
            db.finish_delivery(aid, user_id, "blocked")
            event(log, "delivery.blocked", **fields)
        except (TimedOut, PartlySent) as exc:
            # Telegram may have delivered it (or part of it): sending again could repeat it.
            db.finish_delivery(aid, user_id, "failed", str(exc))
            event(log, "delivery.failed", logging.WARNING, **fields, retry=False,
                  reason="may have arrived; not repeated", error=str(exc))
        except TelegramError as exc:
            status = db.retry_delivery_later(aid, user_id, DELIVERY_RETRY_DELAY, MAX_DELIVERY_ATTEMPTS, str(exc))
            event(log, "delivery.failed", logging.WARNING, **fields, retry=status == "pending",
                  reason={"failed": "giving up", "pending": "will retry"}.get(status, "assessment closed"),
                  error=str(exc))
        else:
            db.finish_delivery(aid, user_id, "sent")
            event(log, "delivery.sent", logging.DEBUG, **fields)
        await asyncio.sleep(SEND_DELAY)
    return attempted


def delivery_report(assessment_id: int, counts: dict[str, int]) -> str:
    """The publisher's report (HTML); its button is delivery_markup."""
    sent = counts.get("sent", 0)
    report = f"📤 Assessment #{assessment_id} sent to {sent} user{'s' if sent != 1 else ''}."
    if counts.get("blocked"):
        report += f"\n{counts['blocked']} blocked the bot and were skipped."
    if counts.get("failed"):
        report += f"\n{counts['failed']} could not be reached (/status lists them)."
    if counts.get("cancelled"):
        report += f"\n{counts['cancelled']} were not sent because the assessment was closed."
    if not sent:  # nobody received it: there will be no scores to follow
        return report + "\nUsers receive assessments once they have sent /start to the bot."
    return report + "\n👉 " + guidance.results_suggestion(assessment_id, "Follow the scores").html()


def delivery_markup(assessment_id: int, counts: dict[str, int]):
    if not counts.get("sent"):
        return None
    suggestion = guidance.results_suggestion(assessment_id)
    return InlineKeyboardMarkup([[InlineKeyboardButton(suggestion.button, callback_data=suggestion.callback_data)]])


async def report_deliveries(bot: Bot, svc: Services, assessment_id: int | None = None) -> None:
    """Tell each publisher, once, that their assessment has finished sending."""
    for row in svc.db.claim_delivery_reports(assessment_id):
        admin_id = row["published_by"] or row["created_by"]
        counts = row["counts"]
        event(log, "delivery.finished", assessment=row["id"], sent=counts.get("sent", 0),
              blocked=counts.get("blocked", 0), failed=counts.get("failed", 0),
              cancelled=counts.get("cancelled", 0))
        try:
            await bot.send_message(admin_id, delivery_report(row["id"], row["counts"]), parse_mode=ParseMode.HTML,
                                   reply_markup=delivery_markup(row["id"], row["counts"]))
        except TelegramError as exc:
            log.warning("Could not report delivery of assessment %s to %s: %s", row["id"], admin_id, exc)


# ── grading ──────────────────────────────────────────────────────────────
async def grade(svc: Services, submission_id: int) -> list[str] | None:
    """Grade a submission the caller has claimed, save the grade and return the result blocks.

    Returns None if another process saved a grade first (its result is that process's to send).
    Raises AIError if grading fails; the submission is then marked 'failed' for /regrade.
    """
    submission = svc.db.get_submission(submission_id)
    assessment = svc.db.get_assessment(submission["assessment_id"])
    questions = svc.db.get_questions(assessment["id"])
    fields = dict(submission=submission_id, assessment=assessment["id"], user=submission["user_id"],
                  attempt=submission["grading_attempts"])
    event(log, "grading.started", **fields)
    started = time.monotonic()
    try:
        results, overall = await svc.ai.grade(questions, submission["raw_text"])
    except AIError as exc:
        svc.db.mark_submission_failed(submission_id, str(exc))
        event(log, "grading.failed", logging.ERROR, **fields, seconds=time.monotonic() - started, error=str(exc))
        raise
    score = round(sum(r["awarded"] for r in results), 2)
    maximum = total_points(questions)
    if not svc.db.save_grade(submission_id, score, maximum, overall, results):
        event(log, "grading.superseded", **fields, reason="graded by another process meanwhile")
        return None
    event(log, "grading.finished", **fields, score=score, max=maximum, seconds=time.monotonic() - started)
    return student.result_blocks(assessment, questions, results, score, maximum, overall,
                                 svc.settings.show_correct_answers)


async def send_result(bot: Bot, svc: Services, submission: dict[str, Any], blocks: list[str]) -> bool:
    """Send a result whose grade this process saved (only one process can: Database.save_grade).

    It is recorded as sent afterwards. If the process stops first, or Telegram fails for a reason
    that may pass, recover_grading sends it once more after RESULT_GRACE seconds.
    """
    fields = dict(submission=submission["id"], user=submission["user_id"])
    # The student's next step, only if there is one: other assessments still to answer.
    nexts = guidance.student_next_steps(svc.db, svc.settings, submission["user_id"], exclude=submission["assessment_id"])
    markup = None
    if nexts:
        footer, markup = guidance.render(svc.settings, submission["user_id"], "", nexts)
        blocks = [*blocks, footer.strip()]
    try:
        await send_blocks(bot, submission["user_id"], blocks, reply_markup=markup)
    except Forbidden:
        svc.db.set_user_active(submission["user_id"], False)
        svc.db.mark_result_sent(submission["id"])  # blocked: there is nobody to send it to
        event(log, "result.blocked", **fields)
        return False
    except TelegramError as exc:
        event(log, "result.failed", logging.WARNING, **fields, retry=True, error=str(exc))
        return False
    svc.db.mark_result_sent(submission["id"])
    event(log, "result.sent", **fields)
    return True


GRADING_DELAYED = (
    "✅ Your answers are saved, but grading is temporarily unavailable. "
    "You don't need to submit again. Your result will be sent after the administrator retries grading."
)


async def _tell_student_grading_failed(bot: Bot, user_id: int) -> None:
    try:
        await bot.send_message(user_id, GRADING_DELAYED)
    except TelegramError:
        log.debug("Could not tell %s that grading failed", user_id)


async def recover_grading(bot: Bot, svc: Services) -> int:
    """Grade submissions whose grading was abandoned, and send results that were never sent.
    Returns how many submissions were handled."""
    lease = grading_lease(svc)
    handled = 0
    while not svc.stopping.is_set() and (claim := svc.db.claim_stale_grading(lease, MAX_GRADING_ATTEMPTS)) is not None:
        outcome, submission = claim
        handled += 1
        aid = submission["assessment_id"]
        if outcome == "gave_up":
            event(log, "grading.gave_up", logging.ERROR, submission=submission["id"], assessment=aid,
                  user=submission["user_id"], attempts=submission["grading_attempts"])
            await _tell_student_grading_failed(bot, submission["user_id"])
            await report_grading_failures(bot, svc)
            continue
        event(log, "grading.resumed", submission=submission["id"], assessment=aid,
              attempt=submission["grading_attempts"])
        try:
            blocks = await grade(svc, submission["id"])
        except AIError:
            await _tell_student_grading_failed(bot, submission["user_id"])
            await report_grading_failures(bot, svc)
            continue
        if blocks is not None:
            await send_result(bot, svc, submission, blocks)

    while not svc.stopping.is_set() and (submission := svc.db.claim_unsent_result(RESULT_GRACE)) is not None:
        handled += 1
        fields = dict(submission=submission["id"], user=submission["user_id"], resent=True)
        try:
            await send_blocks(bot, submission["user_id"], student.stored_result_blocks(svc, submission))
        except TelegramError as exc:
            event(log, "result.failed", logging.WARNING, **fields, retry=False, error=str(exc))
        else:
            event(log, "result.sent", **fields)
    return handled


# ── the loop ─────────────────────────────────────────────────────────────
async def run_once(bot: Bot, svc: Services) -> None:
    """Finish everything left over. Each part is guarded so one failure does not stop the rest."""
    for name, step in (("sending", lambda: deliver(bot, svc)),
                       ("delivery reports", lambda: report_deliveries(bot, svc)),
                       ("grading", lambda: recover_grading(bot, svc)),
                       ("alerts", lambda: report_grading_failures(bot, svc))):
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Background %s failed; will try again in %s s", name, WORK_INTERVAL)


async def run_forever(application: Application, interval: float = WORK_INTERVAL) -> None:
    svc: Services = application.bot_data["services"]
    while not svc.stopping.is_set():
        await run_once(application.bot, svc)
        try:  # sleep, but wake at once when the bot starts shutting down
            await asyncio.wait_for(svc.stopping.wait(), interval)
        except asyncio.TimeoutError:
            pass


def start(application: Application) -> None:
    application.bot_data["work_task"] = asyncio.get_running_loop().create_task(
        run_forever(application), name="background-work"
    )


async def stop(application: Application, timeout: float | None = None) -> None:
    """Stop the background loop. It finishes the message or grading in hand (up to `timeout`
    seconds, default SHUTDOWN_GRACE_SECONDS) and takes no new work; past the timeout it is
    cancelled, and the leases let the next process redo what was cut off."""
    svc: Services = application.bot_data["services"]
    svc.stopping.set()
    task = application.bot_data.pop("work_task", None)
    if task is None:
        return
    if timeout is None:
        timeout = svc.settings.shutdown_grace_seconds
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
        return
    except asyncio.TimeoutError:
        log.warning("Background work did not finish within %s s; stopping it", timeout)
    except Exception:
        log.exception("Background work failed while stopping")
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
