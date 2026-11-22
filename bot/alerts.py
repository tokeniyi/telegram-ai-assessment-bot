"""Telling the admins when something keeps going wrong (Stage 5).

* **The AI provider is down.** Every request reports its outcome (AssessmentAI's observer). After
  AI_DOWN_AFTER requests in a row could not be made, the admins get one alert; when a request
  gets through again, they get one "back" message listing the grading that failed meanwhile.
* **Grading failed.** The first failure is reported at once; failures after it are collected and
  reported together, at most once every GRADING_NOTICE_INTERVAL seconds (the background loop sends
  what is left), and not at all while the admins know the provider is down. An outage with a
  whole class submitting is a handful of messages, not one per student.

Each message goes out once even with several bot processes: who sends it is decided by a locked
row in PostgreSQL (Database.record_ai_failure, record_ai_success, claim_grading_failure_notice).
Monitoring never breaks the work it watches: its own errors are logged and swallowed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from telegram import Bot

from . import guidance
from .ai import AIUnavailable
from .commands import Suggestion
from .logs import event
from .utils import esc

if TYPE_CHECKING:
    from .utils import Services

log = logging.getLogger(__name__)

AI_DOWN_AFTER = 3              # requests in a row that could not be made before the admins are told
GRADING_NOTICE_INTERVAL = 900  # seconds between two notices about failed grading


async def notify_admins(bot: Bot, svc: Services, text: str, suggestions: list[Suggestion] = ()) -> None:
    """Send `text` (HTML) to every admin, with the suggested next steps and their buttons."""
    for admin_id in svc.settings.admin_ids:
        body, markup = guidance.render(svc.settings, admin_id, text, suggestions)
        try:
            await bot.send_message(admin_id, body, parse_mode="HTML", reply_markup=markup)
        except Exception:  # an admin who never started the bot cannot be messaged
            log.debug("Could not notify admin %s", admin_id)


def clock(moment: datetime | None) -> str:
    """14:05 UTC today, or 20 Nov 14:05 UTC on another day."""
    if moment is None:
        return "–"
    moment = moment.astimezone(timezone.utc)
    same_day = moment.date() == datetime.now(timezone.utc).date()
    return moment.strftime("%H:%M UTC" if same_day else "%d %b %H:%M UTC")


AUTO_RETRY_NOTE = "The bot retries failed grading by itself once the AI answers again; you can also retry now."


def grading_failure_notice(rows: list[dict[str, Any]], auto_retry: bool = False) -> tuple[str, list[Suggestion]]:
    """The notice (HTML) and a /regrade suggestion for each assessment in it. `auto_retry`: say
    that the bot retries them itself (Stage 8)."""
    text, retry = _grading_failure_text(rows)
    return (f"{text}\n{AUTO_RETRY_NOTE}" if auto_retry else text), retry


def _grading_failure_text(rows: list[dict[str, Any]]) -> tuple[str, list[Suggestion]]:
    counts: dict[int, int] = {}
    for row in rows:
        counts[row["assessment_id"]] = counts.get(row["assessment_id"], 0) + 1
    retry = [Suggestion("regrade", (str(aid),), why="Retry", button=f"🔁 Retry grading #{aid}")
             for aid in sorted(counts)][:guidance.MAX_BUTTONS]
    if len(rows) == 1:
        row = rows[0]
        return (f"⚠️ Grading failed for a submission to assessment #{row['assessment_id']}: "
                f"{esc(row['grading_error'] or 'unknown error')}\nThe student's answers are saved."), retry
    lines = [f"⚠️ Grading failed for {len(rows)} submissions:"]
    lines += [f"• assessment #{aid}: {n}" for aid, n in sorted(counts.items())]
    latest = rows[-1]["grading_error"]
    if latest:
        lines.append(f"Latest error: {esc(latest)}")
    lines.append(f"More failures are reported at most every {GRADING_NOTICE_INTERVAL // 60} minutes; "
                 "/status shows the current state.")
    return "\n".join(lines), retry


async def report_grading_failures(bot: Bot, svc: Services, force: bool = False) -> int:
    """Tell the admins about grading failures not reported yet, if a notice is due. Returns how
    many failures were reported."""
    try:
        rows = svc.db.claim_grading_failure_notice(GRADING_NOTICE_INTERVAL, force)
    except Exception:
        log.exception("Could not check for grading failures to report")
        return 0
    if rows:
        event(log, "alert.grading_failed", logging.WARNING, failures=len(rows),
              assessments=",".join(sorted({str(r["assessment_id"]) for r in rows})))
        await notify_admins(bot, svc, *grading_failure_notice(rows, auto_retry=_auto_retry_left(svc, rows)))
    return len(rows)


def _auto_retry_left(svc: Services, rows: list[dict[str, Any]]) -> bool:
    """Whether the bot will still retry any of these failures by itself."""
    limit = svc.settings.auto_regrade_attempts
    return bool(limit) and any(r.get("auto_regrades", 0) < limit for r in rows)


async def report_regraded(bot: Bot, svc: Services) -> int:
    """Tell the admins about grading the bot retried by itself and that worked (Stage 8): the first
    at once, later ones together at most every GRADING_NOTICE_INTERVAL. Returns how many."""
    try:
        rows = svc.db.claim_regraded_notice(GRADING_NOTICE_INTERVAL)
        still_failed = svc.db.failed_grading_by_assessment() if rows else []
    except Exception:
        log.exception("Could not check for regraded submissions to report")
        return 0
    if not rows:
        return 0
    counts: dict[int, int] = {}
    for row in rows:
        counts[row["assessment_id"]] = counts.get(row["assessment_id"], 0) + 1
    event(log, "alert.auto_regraded", submissions=len(rows), assessments=",".join(map(str, sorted(counts))))
    where = ", ".join(f"#{aid}: {n}" for aid, n in sorted(counts.items()))
    text = (f"🔁 Grading that had failed was retried automatically: {guidance.plural(len(rows), 'submission')} "
            f"graded ({where}), and the students were sent their results. No action needed.")
    suggestions = []
    if still_failed:
        total = sum(r["n"] for r in still_failed)
        text += f"\n{total} still could not be graded."
        suggestions = guidance.regrade_suggestions(svc.db)
    await notify_admins(bot, svc, text, suggestions)
    return len(rows)


class AIMonitor:
    """AssessmentAI's observer: keeps the shared record of AI outcomes and sends the alerts."""

    def __init__(self, svc: Services, bot: Bot):
        self.svc, self.bot = svc, bot

    async def ai_unavailable(self, error: AIUnavailable) -> None:
        try:
            alert = self.svc.db.record_ai_failure(str(error), AI_DOWN_AFTER)
        except Exception:
            log.exception("Could not record a failed AI request")
            return
        if alert is None:
            return
        event(log, "alert.ai_down", logging.ERROR, failures=alert["consecutive_failures"],
              since=alert["failing_since"], error=alert["last_error"])
        # No /regrade here: retrying cannot work until the provider answers again.
        await notify_admins(self.bot, self.svc, (
            f"🚨 The AI provider is not answering: {alert['consecutive_failures']} requests in a row "
            f"failed since {clock(alert['failing_since'])}.\nLatest error: {esc(alert['last_error'])}\n\n"
            "Students' answers are saved; grading that fails waits to be retried. "
            "You will get a message when the provider answers again, with the grading to retry."
        ), [Suggestion("status", why="Details", button="🩺 Status")])

    async def ai_succeeded(self) -> None:
        try:
            outage = self.svc.db.record_ai_success()
        except Exception:
            log.exception("Could not record a successful AI request")
            return
        if outage is None:
            return
        event(log, "alert.ai_back", since=outage["failing_since"], failures=outage["consecutive_failures"])
        text = f"✅ The AI provider is answering again (it was failing since {clock(outage['failing_since'])})."
        try:
            self.svc.db.claim_grading_failure_notice(GRADING_NOTICE_INTERVAL, force=True)  # reported here
            failed = self.svc.db.failed_grading_by_assessment()
        except Exception:
            log.exception("Could not list failed grading")
            failed = []
        retry = []
        if failed:
            total = sum(r["n"] for r in failed)
            text += f"\n{total} submission{'s are' if total != 1 else ' is'} waiting to be graded."
            if self.svc.settings.auto_regrade_attempts:
                text += " The bot grades them again by itself within a few minutes; you can also retry now."
            retry = guidance.regrade_suggestions(self.svc.db)
        await notify_admins(self.bot, self.svc, text, retry)
