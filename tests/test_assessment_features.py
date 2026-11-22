"""Stage 7: deadlines, reminders before them, and per-assessment options.

Driven through the bot with the fake Telegram API and AI provider. The passing of time is
simulated by moving deadlines (and publication times) with SQL; the background work is run with
work.run_once, as the bot does every 30 seconds. Two harnesses on one database stand for two bot
processes (a deploy).
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bot import deadlines
from bot.config import ConfigError, load_settings
from bot.handlers import work
from tests.conftest import ADMIN_ID, STUDENT_ID
from tests.test_guidance import command, last, sent
from tests.test_recovery import STUDENTS, draft_with_students, received

UTC = timezone.utc
NOW = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
# A grade that misses Q2, so that its reference answer is shown when answers are shown.
PARTLY_WRONG = json.dumps({"results": [
    {"number": 1, "student_answer": "B", "awarded": 1, "feedback": "Correct."},
    {"number": 2, "student_answer": "no idea", "awarded": 0, "feedback": "Not answered."}],
    "overall_feedback": "Revise osmosis."})


def svc(h):
    return h.app.bot_data["services"]


def move_deadline(h, aid, from_now: str, published_ago: str | None = None):
    """Put the deadline `from_now` (an SQL interval, negative for the past) away, as if time passed."""
    h.db._run("UPDATE assessments SET deadline = now() + %s::interval WHERE id = %s", (from_now, aid))
    if published_ago:
        h.db._run("UPDATE assessments SET published_at = (now() - %s::interval)::text WHERE id = %s",
                  (published_ago, aid))


async def published_with_deadline(h, when="2d", students=STUDENTS):
    await draft_with_students(h, students)
    await h.text(ADMIN_ID, f"/deadline 1 {when}")
    await h.press(ADMIN_ID, "pub:1")


def reminders_to(h, user_id):
    return [t for t, _ in sent(h, user_id) if t.startswith("⏰") and "Reminder" in t]


# ── reading and showing a deadline ───────────────────────────────────────
@pytest.mark.parametrize("text, expected", [
    ("2d", NOW + timedelta(days=2)),
    ("in 3 days", NOW + timedelta(days=3)),
    ("1d 12h", NOW + timedelta(days=1, hours=12)),
    ("90m", NOW + timedelta(minutes=90)),
    ("1w", NOW + timedelta(weeks=1)),
    ("2026-11-30 17:00", datetime(2026, 11, 30, 17, 0, tzinfo=UTC)),
    ("2026-11-30", datetime(2026, 11, 30, 23, 59, tzinfo=UTC)),
    ("tomorrow", datetime(2026, 11, 23, 23, 59, tzinfo=UTC)),
    ("Tomorrow 9:30", datetime(2026, 11, 23, 9, 30, tzinfo=UTC)),
    ("today 18:00", datetime(2026, 11, 22, 18, 0, tzinfo=UTC)),
    ("off", None), ("none", None),
])
def test_parse_deadline(text, expected):
    assert deadlines.parse(text, NOW, UTC) == expected


def test_parse_deadline_in_the_bots_time_zone():
    lagos = ZoneInfo("Africa/Lagos")  # UTC+1
    assert deadlines.parse("2026-11-30 17:00", NOW, lagos) == datetime(2026, 11, 30, 16, 0, tzinfo=UTC)
    # "today" is the local day: at 23:30 UTC it is already the 23rd in Lagos
    late = datetime(2026, 11, 22, 23, 30, tzinfo=UTC)
    assert deadlines.parse("today 18:00", late, lagos) == datetime(2026, 11, 23, 17, 0, tzinfo=UTC)
    assert deadlines.format_moment(datetime(2026, 11, 30, 16, 0, tzinfo=UTC), lagos, NOW) == "Mon 30 Nov, 17:00 WAT"


@pytest.mark.parametrize("text, why", [
    ("yesterday", "can't read"), ("soon", "can't read"), ("2026-11-30 25:00", "not a time of day"),
    ("today 09:00", "not in the future"), ("2m", "not in the future"), ("2026-01-01", "not in the future"),
    ("400d", "more than a year"), ("2d and potatoes", "can't read"),
])
def test_parse_deadline_refuses(text, why):
    with pytest.raises(deadlines.DeadlineError, match=why):
        deadlines.parse(text, NOW, UTC)


def test_format_deadline():
    assert deadlines.format_moment(datetime(2027, 1, 2, 8, 5, tzinfo=UTC), UTC, NOW) == "Sat 2 Jan 2027, 08:05 UTC"
    assert deadlines.time_left(NOW + timedelta(days=2, hours=3, minutes=7), NOW) == "in 2 days 3 h"
    assert deadlines.time_left(NOW + timedelta(hours=1, minutes=30), NOW) == "in 1 h 30 min"
    assert deadlines.time_left(NOW + timedelta(seconds=20), NOW) == "in 1 min"
    assert deadlines.time_left(NOW - timedelta(seconds=1), NOW) == "passed"
    assert deadlines.describe(None, UTC) == "none"


def test_settings_timezone_and_reminder_hours(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    base = "TELEGRAM_BOT_TOKEN=1:abc\nAI_API_KEY=k\n"
    for key in ("TIMEZONE", "REMINDER_HOURS"):
        monkeypatch.delenv(key, raising=False)
    env.write_text(base)
    s = load_settings(env)
    assert (s.timezone, s.reminder_hours) == ("UTC", 24)
    monkeypatch.setenv("TIMEZONE", "Africa/Lagos")
    monkeypatch.setenv("REMINDER_HOURS", "0")
    s = load_settings(env)
    assert (s.tz, s.reminder_hours) == (ZoneInfo("Africa/Lagos"), 0)
    monkeypatch.setenv("TIMEZONE", "Mars/Olympus")
    with pytest.raises(ConfigError, match="TIMEZONE"):
        load_settings(env)


# ── /deadline ────────────────────────────────────────────────────────────
async def test_deadline_on_a_draft_is_shown_to_students_when_published(harness):
    h = harness
    await draft_with_students(h, [STUDENT_ID])
    text, buttons = await command(h, ADMIN_ID, "/deadline 1 2d")
    assert "now closes" in text and "(in 2 days)" in text and "reminded 24 h before" in text
    assert "still a draft" in text and buttons == ["cmd:settings:1"]
    assert h.db.get_assessment(1)["deadline"] is not None

    await h.press(ADMIN_ID, "pub:1")
    message = [t for t in h.tg.texts(STUDENT_ID) if "Which organelle" in t][0]
    assert "⏰ Deadline: <b>" in message and "(in 2 days)" in message and "You can submit once." in message
    await h.text(STUDENT_ID, "/pending")
    markup = h.tg.calls[-1][1]["reply_markup"]
    keyboard = (json.loads(markup) if isinstance(markup, str) else markup)["inline_keyboard"]
    assert keyboard[0][0]["text"].startswith("⏰ ") and keyboard[0][0]["callback_data"] == "show:1"

    text, _ = await command(h, ADMIN_ID, "/assessments")
    assert "#1</b>" in text and ", closes " in text


async def test_deadline_without_a_time_shows_it_with_real_examples(harness):
    h = harness
    await draft_with_students(h, [STUDENT_ID])
    text, _ = await command(h, ADMIN_ID, "/deadline 1")
    assert "Deadline of #1: none" in text
    assert "<code>/deadline 1 2d</code>" in text and "<code>/deadline 1 tomorrow 17:00</code>" in text
    assert "/deadline 1 off" not in text  # nothing to remove
    text, _ = await command(h, ADMIN_ID, "/deadline")  # no id: the assessments it can act on
    assert "<code>/deadline 1</code>" in text


async def test_deadline_that_cannot_be_read_is_an_input_problem(harness):
    h = harness
    await draft_with_students(h, [STUDENT_ID])
    for typed, why in (("/deadline 1 soonish", "can't read"), ("/deadline 1 2026-01-01", "not in the future")):
        text, _ = await command(h, ADMIN_ID, typed)
        assert text.startswith("✏️") and why in text and "Times are UTC" in text
        assert "<code>/deadline 1 2d</code>" in text
    assert h.db.get_assessment(1)["deadline"] is None


async def test_deadline_cannot_change_once_closed(harness):
    h = harness
    await published_with_deadline(h, students=[STUDENT_ID])
    await h.text(ADMIN_ID, "/close 1")
    text, buttons = await command(h, ADMIN_ID, "/deadline 1 3d")
    assert text.startswith("ℹ️") and "is closed" in text and buttons == ["cmd:results:1"]

    # Its deadline passed but the background work has not closed it yet: the same answer.
    await draft_with_students(h, [STUDENT_ID])
    await h.text(ADMIN_ID, "/deadline 2 2d")
    await h.press(ADMIN_ID, "pub:2")
    move_deadline(h, 2, "-1 second")
    text, _ = await command(h, ADMIN_ID, "/deadline 2 3d")
    assert "is closed" in text and h.db.get_assessment(2)["deadline"] < datetime.now(UTC)


async def test_deadline_can_be_removed(harness):
    h = harness
    await published_with_deadline(h, students=[STUDENT_ID])
    text, _ = await command(h, ADMIN_ID, "/deadline 1")
    assert "<code>/deadline 1 off</code>" in text
    text, _ = await command(h, ADMIN_ID, "/deadline 1 off")
    assert "no deadline now" in text and h.db.get_assessment(1)["deadline"] is None


async def test_a_draft_whose_deadline_passed_is_not_published(harness):
    h = harness
    await draft_with_students(h, [STUDENT_ID])
    await h.text(ADMIN_ID, "/deadline 1 2d")
    move_deadline(h, 1, "-1 minute")
    await h.press(ADMIN_ID, "pub:1")
    assert h.db.get_assessment(1)["status"] == "draft" and received(h, STUDENT_ID) == 0
    text, _ = last(h, ADMIN_ID)
    assert "has passed, so it was not sent" in text
    assert "<code>/deadline 1 2d</code>" in text and "<code>/deadline 1 off</code>" in text
    # The database refuses too, whatever the handler checked (a second process, a race).
    assert h.db.publish_assessment(1, ADMIN_ID) is None


# ── the deadline passes ──────────────────────────────────────────────────
async def test_answers_stop_at_the_deadline_before_the_assessment_is_closed(harness):
    h = harness
    await published_with_deadline(h, students=[STUDENT_ID])
    await h.text(STUDENT_ID, "1. B")
    move_deadline(h, 1, "-1 second")  # the background work has not run yet: status is 'published'
    assert h.db.get_assessment(1)["status"] == "published"
    await h.press(STUDENT_ID, "ans:submit:1")
    text = h.tg.texts(STUDENT_ID, method="editMessageText")[-1]
    assert "deadline for this assessment passed" in text and "discarded" in text
    assert h.db.latest_submissions(1) == [] and h.db.draft_parts(STUDENT_ID, 1) == []
    await h.text(STUDENT_ID, "/pending")
    assert "all caught up" in h.tg.texts(STUDENT_ID)[-1]


async def test_the_deadline_closes_the_assessment_and_tells_the_publisher_once(make_harness):
    a = await make_harness()
    b = await make_harness()  # a second bot process on the same database
    await published_with_deadline(a)
    await a.text(STUDENT_ID, "/start")
    await a.text(STUDENTS[0], "1. B")
    await a.press(STUDENTS[0], "ans:submit:1")
    move_deadline(a, 1, "-1 second")

    await asyncio.gather(work.run_once(a.app.bot, svc(a)), work.run_once(b.app.bot, svc(b)))
    assert a.db.get_assessment(1)["status"] == "closed" and a.db.get_assessment(1)["closed_at"]
    notices = [(t, k) for h in (a, b) for t, k in sent(h, ADMIN_ID) if "closed at its deadline" in t]
    assert len(notices) == 1
    text, buttons = notices[0]
    assert "1 of the 6 users it was sent to submitted" in text and buttons == ["cmd:results:1"]

    await work.run_once(a.app.bot, svc(a))  # nothing more to say
    assert sum("closed at its deadline" in t for h in (a, b) for t in h.tg.texts(ADMIN_ID)) == 1


async def test_messages_not_sent_by_the_deadline_are_cancelled(harness):
    h = harness
    await draft_with_students(h)
    await h.text(ADMIN_ID, "/deadline 1 2d")
    h.tg.fail = lambda method, params: (429, "Too Many Requests: retry after 0") \
        if method == "sendMessage" and "Which organelle" in params.get("text", "") else None
    await h.press(ADMIN_ID, "pub:1")  # every send fails for a reason that passes: all stay queued
    h.tg.fail = None
    move_deadline(h, 1, "-1 second")
    h.db._run("UPDATE deliveries SET claimed_until = NULL")
    await work.close_due(h.app.bot, svc(h))  # closing cancels them, in the same transaction
    assert h.db.delivery_counts(1).get("pending", 0) == 0 and h.db.delivery_counts(1)["cancelled"] >= 5
    await work.run_once(h.app.bot, svc(h))
    assert sum(received(h, u) for u in STUDENTS) == 0


# ── reminders ────────────────────────────────────────────────────────────
async def test_students_who_have_not_submitted_are_reminded_once(make_harness):
    a = await make_harness()
    b = await make_harness()
    await published_with_deadline(a)
    done, saving, idle = STUDENTS[0], STUDENTS[1], STUDENTS[2]
    await a.text(done, "1. B")
    await a.press(done, "ans:submit:1")
    await a.text(saving, "1. B")

    await work.run_once(a.app.bot, svc(a))  # two days away: not yet
    assert not any(reminders_to(a, u) for u in STUDENTS)

    move_deadline(a, 1, "10 hours", published_ago="1 day")
    await asyncio.gather(work.run_once(a.app.bot, svc(a)), work.run_once(b.app.bot, svc(b)))
    await work.run_once(b.app.bot, svc(b))
    count = lambda u: len(reminders_to(a, u)) + len(reminders_to(b, u))  # noqa: E731
    assert count(done) == 0
    for user_id in [saving, idle, *STUDENTS[3:], ADMIN_ID]:
        assert count(user_id) == 1, user_id

    (text, buttons), = [(t, k) for h in (a, b) for t, k in sent(h, saving) if "Reminder" in t]
    assert "closes in 9 h" in text or "closes in 10 h" in text
    assert "1 answer message saved" in text and buttons == ["ans:submit:1", "ans:undo:1", "ans:clear:0"]
    (text, buttons), = [(t, k) for h in (a, b) for t, k in sent(h, idle) if "Reminder" in t]
    assert "Tap below to see the questions" in text and buttons == ["show:1"]
    assert a.db.reminder_counts(1) == {"sent": 5}  # 5 students and the admin, less the one who submitted

    # The reminder's button opens the assessment; the reminded student can still answer.
    await a.press(idle, "show:1")
    await a.text(idle, "1. B")
    await a.press(idle, "ans:submit:1")
    assert a.db.get_submission(2)["user_id"] == idle


async def test_a_student_who_submits_before_the_reminder_is_sent_is_skipped(harness):
    h = harness
    await published_with_deadline(h, students=[STUDENT_ID])
    move_deadline(h, 1, "10 hours", published_ago="1 day")
    assert h.db.queue_due_reminders(24 * 3600) == [1]
    await h.text(STUDENT_ID, "1. B")
    await h.press(STUDENT_ID, "ans:submit:1")  # after queueing, before sending
    await work.send_reminders(h.app.bot, svc(h))
    assert reminders_to(h, STUDENT_ID) == [] and len(reminders_to(h, ADMIN_ID)) == 1
    assert h.db.reminder_counts(1) == {"skipped": 1, "sent": 1}


async def test_no_reminder_when_published_close_to_the_deadline(harness):
    h = harness
    await published_with_deadline(h, when="10h", students=[STUDENT_ID])
    message = [t for t in h.tg.texts(STUDENT_ID) if "Which organelle" in t][0]
    assert "⏰ Deadline" in message  # they were just told
    await work.run_once(h.app.bot, svc(h))
    assert reminders_to(h, STUDENT_ID) == [] and h.db.reminder_counts(1) == {}


async def test_moving_the_deadline_reminds_again_before_the_new_one(harness):
    h = harness
    await published_with_deadline(h, students=[STUDENT_ID])
    move_deadline(h, 1, "10 hours", published_ago="1 day")
    await work.run_once(h.app.bot, svc(h))
    assert len(reminders_to(h, STUDENT_ID)) == 1
    await h.text(ADMIN_ID, "/deadline 1 3d")  # extended: no second reminder now
    await work.run_once(h.app.bot, svc(h))
    assert len(reminders_to(h, STUDENT_ID)) == 1
    move_deadline(h, 1, "5 hours")  # time passes: the new deadline is close
    await work.run_once(h.app.bot, svc(h))
    assert len(reminders_to(h, STUDENT_ID)) == 2


async def test_reminders_can_be_switched_off(make_harness):
    h = await make_harness(reminder_hours=0)
    await published_with_deadline(h, students=[STUDENT_ID])
    move_deadline(h, 1, "1 hour", published_ago="1 day")
    await work.run_once(h.app.bot, svc(h))
    assert reminders_to(h, STUDENT_ID) == []


async def test_a_student_who_blocked_the_bot_is_not_reminded_again(harness):
    h = harness
    await published_with_deadline(h, students=[STUDENT_ID])
    move_deadline(h, 1, "10 hours", published_ago="1 day")
    h.tg.fail = lambda method, params: (403, "Forbidden: bot was blocked by the user") \
        if int(params.get("chat_id", 0)) == STUDENT_ID else None
    await work.run_once(h.app.bot, svc(h))
    assert h.db.reminder_counts(1) == {"blocked": 1, "sent": 1}
    assert STUDENT_ID not in h.db.active_user_ids()


# ── per-assessment options ───────────────────────────────────────────────
async def test_settings_shows_the_options_and_their_buttons(harness):
    h = harness
    await draft_with_students(h, [STUDENT_ID])
    preview = [t for t in h.tg.texts(ADMIN_ID) if "Draft #1" in t][0]
    assert "⏰ Deadline: none" in preview and "Reference answers after grading: shown" in preview
    await h.press(ADMIN_ID, "cmd:settings:1")  # the button under the draft preview
    text, buttons = last(h, ADMIN_ID)
    assert "Settings of #1" in text and "shown (bot default)" in text and "not allowed" in text
    assert "<code>/deadline 1 2d</code>" in text
    assert buttons == ["opt:1:show_answers:0", "opt:1:allow_resubmission:1"]


async def test_hiding_reference_answers_for_one_assessment(harness):
    h = harness
    await draft_with_students(h, [STUDENT_ID])
    await h.press(ADMIN_ID, "opt:1:show_answers:0")
    text = h.tg.texts(ADMIN_ID, method="editMessageText")[-1]
    assert "Reference answers in results: hidden" in text and "(bot default)" not in text.split("hidden")[1][:20]
    await h.press(ADMIN_ID, "opt:1:show_answers:0")  # a double tap sets the same value
    assert h.db.get_assessment(1)["show_answers"] is False
    await h.press(ADMIN_ID, "pub:1")
    await h.text(STUDENT_ID, "1. B")
    h.ai.replies.append(PARTLY_WRONG)
    await h.press(STUDENT_ID, "ans:submit:1")
    result = h.tg.texts(STUDENT_ID)[-1]
    assert "Score: <b>1 / 4</b>" in result and "Reference answer" not in result

    # A second assessment, with the bot default (shown), does show it.
    await h.document(ADMIN_ID, "notes.txt", b"Mitochondria produce ATP. " * 10)
    await h.press(ADMIN_ID, "pub:2")
    await h.text(STUDENT_ID, "1. B")
    h.ai.replies.append(PARTLY_WRONG)
    await h.press(STUDENT_ID, "ans:submit:2")
    assert "Reference answer" in h.tg.texts(STUDENT_ID)[-1]


async def test_resubmission_allowed_for_one_assessment_only(harness):
    h = harness  # ALLOW_RESUBMISSION=false for the bot
    await draft_with_students(h, [STUDENT_ID])
    await h.press(ADMIN_ID, "opt:1:allow_resubmission:1")
    await h.press(ADMIN_ID, "pub:1")
    assert "You may submit more than once" in [t for t in h.tg.texts(STUDENT_ID) if "Which organelle" in t][0]
    await h.document(ADMIN_ID, "notes.txt", b"Mitochondria produce ATP. " * 10)
    await h.press(ADMIN_ID, "pub:2")

    for aid in (1, 2):
        await h.press(STUDENT_ID, f"show:{aid}")
        await h.text(STUDENT_ID, "1. B")
        await h.press(STUDENT_ID, f"ans:submit:{aid}")
    open_ids = [a["id"] for a in h.db.open_assessments_for(STUDENT_ID, default_resubmission=False)]
    assert open_ids == [1]  # #2 was submitted and allows one attempt

    await h.press(STUDENT_ID, "show:1")
    await h.text(STUDENT_ID, "1. A")
    await h.press(STUDENT_ID, "ans:submit:1")
    assert [s["raw_text"] for s in h.db.user_submissions(STUDENT_ID) if s["assessment_id"] == 1] == ["1. A", "1. B"]
    await h.press(STUDENT_ID, "show:2")
    assert "can be submitted only once" in h.tg.texts(STUDENT_ID, method="editMessageText")[-1]

    # Switched off again: the student who already submitted #1 cannot submit it any more.
    await h.press(ADMIN_ID, "opt:1:allow_resubmission:0")
    assert h.db.open_assessments_for(STUDENT_ID, default_resubmission=False) == []


async def test_option_buttons_are_for_admins_only(harness):
    h = harness
    await draft_with_students(h, [STUDENT_ID])
    await h.press(STUDENT_ID, "opt:1:show_answers:0")
    assert h.db.get_assessment(1)["show_answers"] is None
    answers = [p.get("text", "") for m, p in h.tg.calls if m == "answerCallbackQuery"]
    assert "Administrators only." in answers[-1]
    await h.press(STUDENT_ID, "cmd:settings:1")
    assert h.db.get_assessment(1)["show_answers"] is None


async def test_resubmission_cannot_be_changed_once_closed(harness):
    h = harness
    await draft_with_students(h, [STUDENT_ID])
    await h.press(ADMIN_ID, "pub:1")
    await h.text(ADMIN_ID, "/close 1")
    text, buttons = await command(h, ADMIN_ID, "/settings 1")
    assert buttons == ["opt:1:show_answers:0"] and "/deadline" not in text
    await h.press(ADMIN_ID, "opt:1:allow_resubmission:1")
    assert h.db.get_assessment(1)["allow_resubmission"] is None
