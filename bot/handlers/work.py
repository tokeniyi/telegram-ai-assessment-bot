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
run during a deploy. A process that stops between sending a message and marking it sent leaves a
row that is sent again once its lease runs out; that narrow window is the only way a user can
receive an assessment twice.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import Bot
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import Application

from ..ai import AIError
from ..utils import Services, send_blocks, total_points
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
async def _send_once(bot: Bot, user_id: int, blocks: list[str]) -> None:
    try:
        await send_blocks(bot, user_id, blocks)
    except RetryAfter as exc:
        delay = exc.retry_after
        await asyncio.sleep(delay.total_seconds() if hasattr(delay, "total_seconds") else delay)
        await send_blocks(bot, user_id, blocks)


async def deliver(bot: Bot, svc: Services, assessment_id: int | None = None) -> int:
    """Send every queued assessment message that no other process is sending (only those of
    `assessment_id` if given). Returns how many were attempted."""
    db = svc.db
    blocks_cache: dict[int, list[str]] = {}
    attempted = 0
    while True:
        row = db.claim_delivery(DELIVERY_LEASE, assessment_id)
        if row is None:
            return attempted
        attempted += 1
        aid, user_id = row["assessment_id"], row["user_id"]
        if aid not in blocks_cache:
            blocks_cache[aid] = student.assessment_blocks(db.get_assessment(aid), db.get_questions(aid))
        try:
            await _send_once(bot, user_id, blocks_cache[aid])
        except Forbidden:
            db.set_user_active(user_id, False)
            db.finish_delivery(aid, user_id, "blocked")
        except TelegramError as exc:
            status = db.retry_delivery_later(aid, user_id, DELIVERY_RETRY_DELAY, MAX_DELIVERY_ATTEMPTS, str(exc))
            log.warning("Could not send assessment %s to %s (%s): %s", aid, user_id,
                        "giving up" if status == "failed" else "will retry", exc)
        else:
            db.finish_delivery(aid, user_id, "sent")
        await asyncio.sleep(SEND_DELAY)


def delivery_report(assessment_id: int, counts: dict[str, int]) -> str:
    sent = counts.get("sent", 0)
    report = f"📤 Assessment #{assessment_id} sent to {sent} user{'s' if sent != 1 else ''}."
    if counts.get("blocked"):
        report += f"\n{counts['blocked']} blocked the bot and were skipped."
    if counts.get("failed"):
        report += f"\n{counts['failed']} could not be reached (see the log)."
    if counts.get("cancelled"):
        report += f"\n{counts['cancelled']} were not sent because the assessment was closed."
    return report + f"\nUse /results {assessment_id} to follow the scores."


async def report_deliveries(bot: Bot, svc: Services, assessment_id: int | None = None) -> None:
    """Tell each publisher, once, that their assessment has finished sending."""
    for row in svc.db.claim_delivery_reports(assessment_id):
        admin_id = row["published_by"] or row["created_by"]
        try:
            await bot.send_message(admin_id, delivery_report(row["id"], row["counts"]))
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
    try:
        results, overall = await svc.ai.grade(questions, submission["raw_text"])
    except AIError:
        svc.db.mark_submission_failed(submission_id)
        raise
    score = round(sum(r["awarded"] for r in results), 2)
    maximum = total_points(questions)
    if not svc.db.save_grade(submission_id, score, maximum, overall, results):
        log.info("Submission %s was graded by another process meanwhile", submission_id)
        return None
    return student.result_blocks(assessment, questions, results, score, maximum, overall,
                                 svc.settings.show_correct_answers)


async def send_result(bot: Bot, svc: Services, submission: dict[str, Any], blocks: list[str]) -> bool:
    """Send a result whose grade this process saved (only one process can: Database.save_grade).

    It is recorded as sent afterwards. If the process stops first, or Telegram fails for a reason
    that may pass, recover_grading sends it once more after RESULT_GRACE seconds.
    """
    try:
        await send_blocks(bot, submission["user_id"], blocks)
    except Forbidden:
        svc.db.set_user_active(submission["user_id"], False)
        svc.db.mark_result_sent(submission["id"])  # blocked: there is nobody to send it to
        return False
    except TelegramError as exc:
        log.warning("Could not send result of submission %s to %s (will retry once): %s",
                    submission["id"], submission["user_id"], exc)
        return False
    svc.db.mark_result_sent(submission["id"])
    return True


async def notify_admins(bot: Bot, svc: Services, text: str) -> None:
    for admin_id in svc.settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:  # an admin who never started the bot cannot be messaged
            log.debug("Could not notify admin %s", admin_id)


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
    while (claim := svc.db.claim_stale_grading(lease, MAX_GRADING_ATTEMPTS)) is not None:
        outcome, submission = claim
        handled += 1
        aid = submission["assessment_id"]
        if outcome == "gave_up":
            log.error("Gave up grading submission %s after %s attempts", submission["id"],
                      submission["grading_attempts"])
            await _tell_student_grading_failed(bot, submission["user_id"])
            await notify_admins(bot, svc, f"⚠️ Grading a submission to assessment #{aid} was interrupted "
                                          f"{submission['grading_attempts']} times.\nRun /regrade {aid} to retry.")
            continue
        log.info("Resuming grading of submission %s (attempt %s)", submission["id"], submission["grading_attempts"])
        try:
            blocks = await grade(svc, submission["id"])
        except AIError as exc:
            log.error("Grading failed for submission %s: %s", submission["id"], exc)
            await _tell_student_grading_failed(bot, submission["user_id"])
            await notify_admins(bot, svc, f"⚠️ Grading failed for a submission to assessment #{aid}: {exc}\n"
                                          f"Run /regrade {aid} to retry.")
            continue
        if blocks is not None:
            await send_result(bot, svc, submission, blocks)

    while (submission := svc.db.claim_unsent_result(RESULT_GRACE)) is not None:
        handled += 1
        log.info("Sending the result of submission %s, which was graded but never sent", submission["id"])
        try:
            await send_blocks(bot, submission["user_id"], student.stored_result_blocks(svc, submission))
        except TelegramError as exc:
            log.warning("Could not send result of submission %s: %s", submission["id"], exc)
    return handled


# ── the loop ─────────────────────────────────────────────────────────────
async def run_once(bot: Bot, svc: Services) -> None:
    """Finish everything left over. Each part is guarded so one failure does not stop the rest."""
    for name, step in (("sending", lambda: deliver(bot, svc)),
                       ("delivery reports", lambda: report_deliveries(bot, svc)),
                       ("grading", lambda: recover_grading(bot, svc))):
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Background %s failed; will try again in %s s", name, WORK_INTERVAL)


async def run_forever(application: Application, interval: float = WORK_INTERVAL) -> None:
    svc: Services = application.bot_data["services"]
    while True:
        await run_once(application.bot, svc)
        await asyncio.sleep(interval)


def start(application: Application) -> None:
    application.bot_data["work_task"] = asyncio.get_running_loop().create_task(
        run_forever(application), name="background-work"
    )


async def stop(application: Application) -> None:
    task = application.bot_data.pop("work_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

