"""Study check-ins: a short, AI-written message every few days to students who asked for them,
encouraging them to review and inviting them to reply if they are stuck. A side feature, outside
the staged roadmap (docs/implementations/feature-study-checkins/).

* Opt-in. Students are offered check-ins once, with /start; /checkins turns them on, off or
  pauses them for a week. Nothing is sent to anyone who did not say yes.
* When: every CHECKIN_DAYS days at CHECKIN_HOUR in TIMEZONE. A check-in is put off to the next
  day when the student used the bot in the last RECENT_ACTIVITY, or was sent a deadline reminder
  today; one whose hour passed while no bot was running is put off to the next hour. After two
  check-ins in a row with no answer, the gap doubles; after three, it is four times as long. Any
  message from the student brings it back to normal.
* What: the AI writes it from what the database knows about the student (`SOURCES`): the
  assessments they still have to answer, the questions they recently lost points on, and what they
  last asked. A new source (study-session memory, later) is one more function in `SOURCES`. When the
  AI provider is down or its reply is unusable, a built-in message is sent instead.
* Once: the database hands each due check-in to one process and moves it to its next time in the
  same transaction (Database.claim_checkin), so two bot versions running together send it once.

The scheduling decision (`decide`) is a pure function, so it is tested without waiting for time.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Any, Callable

from . import deadlines
from .ai import AIError
from .alerts import AI_DOWN_AFTER
from .utils import esc, num

if TYPE_CHECKING:
    from .config import Settings
    from .utils import Services

log = logging.getLogger(__name__)

RECENT_ACTIVITY = timedelta(hours=12)  # no check-in to a student who used the bot this recently
MISSED_AFTER = timedelta(hours=3)      # a check-in this late (no bot was running) waits for the next hour
PAUSE = timedelta(days=7)              # /checkins pause
MAX_FACT_CHARS = 200                   # per question or chat message given to the AI
MAX_CONTEXT_CHARS = 2_000


# ── when ─────────────────────────────────────────────────────────────────
def slot(now: datetime, tz: tzinfo, hour: int, days_ahead: int) -> datetime:
    """`hour` o'clock (in `tz`) `days_ahead` days from today; the next day if that has passed."""
    day: date = now.astimezone(tz).date() + timedelta(days=days_ahead)
    moment = datetime.combine(day, time(hour), tzinfo=tz)
    if moment <= now:
        moment = datetime.combine(day + timedelta(days=1), time(hour), tzinfo=tz)
    return moment.astimezone(timezone.utc)


def day_start(now: datetime, tz: tzinfo) -> datetime:
    """Midnight today in `tz`, for "already reminded today"."""
    return datetime.combine(now.astimezone(tz).date(), time(0), tzinfo=tz).astimezone(timezone.utc)


def gap_days(days: int, ignored: int) -> int:
    """Days to the next check-in after `ignored` check-ins in a row went unanswered."""
    return days * (1 if ignored < 2 else 2 if ignored == 2 else 4)


def decide(row: dict[str, Any], now: datetime, settings: Settings) -> tuple[str, datetime | None, int]:
    """What to do with a due check-in: (action, next_at, ignored).

    "blocked": the user blocked the bot, stop. "missed", "recent", "reminded": not now, at `next_at`.
    "send": send it now; the next one is at `next_at`, and `ignored` counts this one.
    """
    tz, hour = settings.tz, settings.checkin_hour
    if not row["user_active"]:
        return "blocked", None, row["ignored"]
    if now - row["next_at"] > MISSED_AFTER:
        return "missed", slot(now, tz, hour, 0), row["ignored"]
    active, last_sent = row["last_active_at"], row["last_sent_at"]
    answered = last_sent is None or (active is not None and active > last_sent)
    ignored = 0 if answered else row["ignored"]
    if active is not None and now - active < RECENT_ACTIVITY:
        return "recent", slot(now, tz, hour, 1), ignored
    if row["reminded_today"]:
        return "reminded", slot(now, tz, hour, 1), ignored
    ignored += 1
    return "send", slot(now, tz, hour, gap_days(settings.checkin_days, ignored)), ignored


def every(days: int) -> str:
    return "every day" if days == 1 else f"every {days} days"


def at_hour(settings: Settings) -> str:
    """'17:00 WAT'."""
    moment = slot(datetime.now(timezone.utc), settings.tz, settings.checkin_hour, 0).astimezone(settings.tz)
    return f"{moment:%H:%M} {moment.tzname()}"


# ── what the AI is told about the student ────────────────────────────────
def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_FACT_CHARS else text[:MAX_FACT_CHARS - 1] + "…"


def open_assessments(svc: Services, user_id: int) -> list[str]:
    lines = []
    for a in svc.db.open_assessments_for(user_id, default_resubmission=svc.settings.allow_resubmission)[:3]:
        line = f'Assessment still to answer: "{_short(a["title"])}"'
        if a.get("deadline"):
            line += f", closes {deadlines.time_left(a['deadline'])}"
        saved = len(svc.db.draft_parts(user_id, a["id"]))
        if saved:
            line += f" (they have saved {saved} answer message(s), not submitted)"
        lines.append(line + ".")
    return lines


def recent_results(svc: Services, user_id: int) -> list[str]:
    """The latest graded result and the questions in it that lost points. No reference answers."""
    graded = [s for s in svc.db.user_submissions(user_id, limit=5) if s["status"] == "graded"]
    if not graded:
        return []
    last = graded[0]
    lines = [f'Latest result: "{_short(last["title"])}", {num(last["score"])}/{num(last["max_score"])} points.']
    questions = {q["position"]: q for q in svc.db.get_questions(last["assessment_id"])}
    missed = [item for item in json.loads(last["details"] or "[]") if item["awarded"] < item["max"]]
    for item in missed[:3]:
        q = questions.get(item["number"])
        if q:
            lines.append(f"  Lost points on: {_short(q['text'])}")
    return lines


def recent_questions(svc: Services, user_id: int) -> list[str]:
    """The last things the student asked or said in chat (not their saved answers)."""
    history = svc.db.chat_history(user_id)
    said = []
    for i, (role, text) in enumerate(history):
        reply = history[i + 1][1] if i + 1 < len(history) else ""
        if role == "Student" and not reply.startswith("(saved as an answer"):
            said.append(text)
    return [f"They recently wrote: {_short(text)}" for text in said[-3:]]


# Each source returns lines of facts about the student. Study-session memory will be one more.
SOURCES: tuple[Callable[[Services, int], list[str]], ...] = (open_assessments, recent_results, recent_questions)


def facts(svc: Services, user_id: int) -> str:
    lines: list[str] = []
    for source in SOURCES:
        try:
            lines += source(svc, user_id)
        except Exception:  # one broken source must not stop the check-in
            log.exception("Check-in source %s failed for user %s", source.__name__, user_id)
    return "\n".join(lines)[:MAX_CONTEXT_CHARS]


# ── the message ──────────────────────────────────────────────────────────
def first_name(full_name: str | None) -> str:
    return (full_name or "").split(" ")[0]


def fallback(name: str) -> str:
    hello = f"Hi {name}!" if name else "Hi!"
    return (f"{hello} A quick check-in: have you had a chance to review your notes lately? "
            "If anything is unclear or you're stuck on a question, reply here and I'll help.")


def ai_is_down(svc: Services) -> bool:
    """Stage 5 follows the provider: after AI_DOWN_AFTER failed requests in a row it is down, and
    a check-in does not wait for yet another timeout."""
    return svc.db.ai_health()["consecutive_failures"] >= AI_DOWN_AFTER


async def compose(svc: Services, user_id: int, full_name: str | None) -> tuple[str, str]:
    """(plain text of the check-in, "ai" or "fallback")."""
    name = first_name(full_name)
    if ai_is_down(svc):
        return fallback(name), "fallback"
    try:
        return await svc.ai.checkin(name, facts(svc, user_id)), "ai"
    except AIError as exc:
        log.warning("Check-in for %s written without the AI: %s", user_id, exc)
        return fallback(name), "fallback"


def message_html(text: str) -> str:
    return (f"💬 {esc(text)}\n\n<i>Just reply here when you'd like help. "
            "/checkins changes or stops these messages.</i>")
