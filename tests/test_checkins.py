"""Study check-ins (side feature): offered once, opt-in, sent at most once per due time, written by
the AI from what the bot knows about the student, with a built-in message when the AI is down.

Driven through the bot with the fake Telegram API and AI provider. Time is simulated by moving
`next_at` and `last_active_at` with SQL; the background work is run with work.run_once, as the bot
does every 30 seconds. Two harnesses on one database stand for two bot processes (a deploy).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bot import checkins
from bot.ai import AIUnavailable
from bot.config import ConfigError, load_settings
from bot.handlers import work
from tests.conftest import ADMIN_ID, STUDENT_ID, Crash, make_settings
from tests.test_assessment_features import PARTLY_WRONG, svc
from tests.test_flow import MATERIAL, publish
from tests.test_guidance import command, sent

UTC = timezone.utc
LAGOS = ZoneInfo("Africa/Lagos")  # UTC+1
NOW = datetime(2026, 11, 22, 16, 0, tzinfo=UTC)


def checkins_to(h, user_id):
    return [(t, k) for t, k in sent(h, user_id) if t.startswith("💬")]


def checkin_calls(h):
    return [user for system, user in h.ai.calls if system.startswith("You are a friendly study companion")]


def make_due(h, user_id, active_ago="2 days"):
    """As if the check-in's time has come, and the student last used the bot `active_ago`."""
    h.db._run("UPDATE study_checkins SET next_at = now() - interval '1 minute' WHERE user_id = %s", (user_id,))
    h.db._run("UPDATE users SET last_active_at = now() - %s::interval WHERE user_id = %s", (active_ago, user_id))


async def opted_in(h, user_id=STUDENT_ID):
    await h.text(user_id, "/start")
    await h.press(user_id, "chk:on")
    assert h.db.get_checkins(user_id)["status"] == "on"


# ── scheduling (pure) ────────────────────────────────────────────────────
def test_slot_is_the_hour_in_the_bots_time_zone():
    assert checkins.slot(NOW, UTC, 17, 0) == datetime(2026, 11, 22, 17, 0, tzinfo=UTC)  # later today
    assert checkins.slot(NOW, UTC, 9, 0) == datetime(2026, 11, 23, 9, 0, tzinfo=UTC)   # passed: tomorrow
    assert checkins.slot(NOW, UTC, 17, 3) == datetime(2026, 11, 25, 17, 0, tzinfo=UTC)
    assert checkins.slot(NOW, LAGOS, 17, 1) == datetime(2026, 11, 23, 16, 0, tzinfo=UTC)  # 17:00 WAT
    assert checkins.day_start(datetime(2026, 11, 22, 23, 30, tzinfo=UTC), LAGOS) == datetime(2026, 11, 22, 23, 0, tzinfo=UTC)


def test_the_gap_grows_when_check_ins_are_ignored():
    assert [checkins.gap_days(3, n) for n in range(5)] == [3, 3, 6, 12, 12]


def due_row(**fields):
    row = dict(user_active=True, next_at=NOW - timedelta(minutes=1), last_active_at=NOW - timedelta(days=2),
               last_sent_at=None, ignored=0, reminded_today=False)
    return {**row, **fields}


@pytest.mark.parametrize("fields, action", [
    (dict(user_active=False), "blocked"),
    (dict(next_at=NOW - timedelta(hours=4)), "missed"),            # no bot ran at the hour: next hour
    (dict(last_active_at=NOW - timedelta(hours=2)), "recent"),     # was just using the bot
    (dict(reminded_today=True), "reminded"),                       # got a deadline reminder today
    (dict(), "send"),
])
def test_decide(fields, action):
    settings = make_settings("postgresql://x", checkin_days=3, checkin_hour=16)
    got, next_at, _ = checkins.decide(due_row(**fields), NOW, settings)
    assert got == action
    if action in ("recent", "reminded"):
        assert next_at == datetime(2026, 11, 23, 16, 0, tzinfo=UTC)
    if action == "missed":
        assert next_at == datetime(2026, 11, 23, 16, 0, tzinfo=UTC)
    if action == "send":
        assert next_at == datetime(2026, 11, 25, 16, 0, tzinfo=UTC)


def test_decide_counts_unanswered_check_ins_and_resets_on_a_reply():
    settings = make_settings("postgresql://x", checkin_days=3, checkin_hour=16)
    sent_at = NOW - timedelta(days=3)
    ignored = dict(last_sent_at=sent_at, last_active_at=sent_at - timedelta(hours=1))
    assert checkins.decide(due_row(**ignored, ignored=1), NOW, settings)[1:] == (
        datetime(2026, 11, 28, 16, 0, tzinfo=UTC), 2)   # second ignored: 6 days
    assert checkins.decide(due_row(**ignored, ignored=2), NOW, settings)[1:] == (
        datetime(2026, 12, 4, 16, 0, tzinfo=UTC), 3)    # third: 12 days
    replied = dict(last_sent_at=sent_at, last_active_at=sent_at + timedelta(hours=1))
    assert checkins.decide(due_row(**replied, ignored=3), NOW, settings)[1:] == (
        datetime(2026, 11, 25, 16, 0, tzinfo=UTC), 1)   # they answered: back to 3 days


def test_settings(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=1:x\nAI_API_KEY=k\n")
    for key in ("CHECKIN_DAYS", "CHECKIN_HOUR"):
        monkeypatch.delenv(key, raising=False)
    s = load_settings(env)
    assert (s.checkin_days, s.checkin_hour) == (3, 17)
    monkeypatch.setenv("CHECKIN_DAYS", "0")
    monkeypatch.setenv("CHECKIN_HOUR", "8")
    s = load_settings(env)
    assert (s.checkin_days, s.checkin_hour) == (0, 8)
    monkeypatch.setenv("CHECKIN_HOUR", "24")
    with pytest.raises(ConfigError, match="CHECKIN_HOUR"):
        load_settings(env)


# ── opting in ────────────────────────────────────────────────────────────
async def test_students_are_offered_check_ins_once_and_nothing_is_sent_without_a_yes(make_harness):
    a = await make_harness()
    b = await make_harness()
    await a.text(ADMIN_ID, "/start")
    assert "Would you like me to message you" not in a.tg.texts(ADMIN_ID)[-1]  # admins are not asked

    await a.text(STUDENT_ID, "/start")
    text, buttons = sent(a, STUDENT_ID)[-1]
    assert "Would you like me to message you every 3 days" in text and "every 3 days" in text and buttons == ["chk:on", "chk:off"]
    await b.text(STUDENT_ID, "/start")  # again, through the other process: not asked twice
    assert "Would you like me to message you" not in b.tg.texts(STUDENT_ID)[-1]

    # Offered but not answered: no check-in, however long it has been.
    a.db._run("UPDATE study_checkins SET next_at = now() - interval '1 minute'")
    make_due(a, STUDENT_ID)
    await work.run_once(a.app.bot, svc(a))
    assert checkins_to(a, STUDENT_ID) == [] and a.db.get_checkins(STUDENT_ID)["status"] == "offered"

    await a.press(STUDENT_ID, "chk:off")
    assert a.db.get_checkins(STUDENT_ID)["status"] == "off"
    assert "Study check-ins are off" in a.tg.texts(STUDENT_ID)[-1]
    assert [m for m, _ in a.tg.calls].count("editMessageReplyMarkup") == 1  # the offer's buttons are removed


async def test_saying_yes_schedules_the_first_check_in_for_tomorrow(harness):
    h = harness
    await opted_in(h)
    row = h.db.get_checkins(STUDENT_ID)
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    assert row["next_at"] == datetime.combine(tomorrow, datetime.min.time().replace(hour=17), tzinfo=UTC)
    text = h.tg.texts(STUDENT_ID)[-1]
    assert "Study check-ins are on" in text and "every 3 days around 17:00 UTC" in text
    assert "<code>/checkins off</code>" in text


async def test_the_checkins_command(harness):
    h = harness
    await h.text(STUDENT_ID, "/start")
    text, buttons = await command(h, STUDENT_ID, "/checkins")
    assert "are off" in text and buttons == ["chk:on"]
    text, _ = await command(h, STUDENT_ID, "/checkins pause")
    assert text.startswith("ℹ️") and "off already" in text
    text, _ = await command(h, STUDENT_ID, "/checkins sometimes")
    assert text.startswith("✏️") and "<code>/checkins on</code>" in text and "<code>/checkins pause</code>" in text

    await command(h, STUDENT_ID, "/checkins on")
    text, buttons = await command(h, STUDENT_ID, "/checkins")
    assert "are on" in text and "The next one" in text and buttons == ["chk:pause", "chk:off"]

    text, _ = await command(h, STUDENT_ID, "/checkins pause")
    assert "Paused for a week" in text
    row = h.db.get_checkins(STUDENT_ID)
    assert row["status"] == "on" and row["next_at"] - datetime.now(UTC) > timedelta(days=6)
    text, buttons = await command(h, STUDENT_ID, "/checkins")
    assert "paused until" in text and buttons == ["chk:on", "chk:off"]

    await command(h, STUDENT_ID, "/checkins stop")
    assert h.db.get_checkins(STUDENT_ID)["status"] == "off"


async def test_switched_off_bot_wide(make_harness):
    h = await make_harness(checkin_days=0)
    await h.text(STUDENT_ID, "/start")
    assert "Would you like me to message you" not in h.tg.texts(STUDENT_ID)[-1]
    text, _ = await command(h, STUDENT_ID, "/checkins on")
    assert "switched off on this bot" in text
    # Choices made while it was on are kept, but nothing is sent.
    h.db.set_checkins(STUDENT_ID, "on", datetime.now(UTC))
    make_due(h, STUDENT_ID)
    await work.run_once(h.app.bot, svc(h))
    assert checkins_to(h, STUDENT_ID) == []


# ── sending ──────────────────────────────────────────────────────────────
async def test_a_due_check_in_is_personal_and_sent_once_by_two_processes(make_harness):
    a = await make_harness()
    b = await make_harness()
    await publish(a)  # assessment #1, and the student's /start
    await a.text(STUDENT_ID, "1. B")
    a.ai.replies.append(PARTLY_WRONG)
    await a.press(STUDENT_ID, "ans:submit:1")  # loses points on the osmosis question
    await a.text(STUDENT_ID, "what is a semi-permeable membrane?")  # chat
    await a.document(ADMIN_ID, "more.txt", MATERIAL)
    await a.press(ADMIN_ID, "pub:2")  # still to answer
    await a.press(STUDENT_ID, "chk:on")
    make_due(a, STUDENT_ID)

    await asyncio.gather(work.run_once(a.app.bot, svc(a)), work.run_once(b.app.bot, svc(b)))
    await work.run_once(b.app.bot, svc(b))
    got = checkins_to(a, STUDENT_ID) + checkins_to(b, STUDENT_ID)
    assert len(got) == 1
    (text, buttons), = got
    assert "How is your revision going? &lt;b&gt;Reply&lt;/b&gt;" in text  # the AI's words, escaped
    assert "/checkins" in text and buttons == ["cmd:pending", "chk:pause"]

    (context,), = [checkin_calls(h) for h in (a, b) if checkin_calls(h)]
    assert "Student's first name: User2002" in context
    assert 'Assessment still to answer: "Cell Biology Basics"' in context
    assert "Latest result" in context and "Lost points on: Explain what osmosis is." in context
    assert "They recently wrote: what is a semi-permeable membrane?" in context
    assert "They recently wrote: 1. B" not in context  # saved answers are not "asked"
    assert "Movement of water" not in context  # no reference answers

    row = a.db.get_checkins(STUDENT_ID)
    assert row["ignored"] == 1 and row["next_at"] - datetime.now(UTC) > timedelta(days=2)
    assert a.db.checkin_overview()["sent"] == 1 and a.db.checkin_overview()["by_ai"] == 1
    # It is in the chat history, so the AI knows what a reply answers.
    assert a.db.chat_history(STUDENT_ID)[-1] == ("Assistant", "How is your revision going? <b>Reply</b> if you're stuck.")
    await a.text(STUDENT_ID, "yes, I'm stuck on osmosis")
    assert "How is your revision going?" in a.ai.calls[-1][1].split("<history>", 1)[1]


async def test_no_check_in_to_a_student_who_was_just_active_or_reminded_today(harness):
    h = harness
    await opted_in(h)
    make_due(h, STUDENT_ID, active_ago="1 hour")
    await work.run_once(h.app.bot, svc(h))
    assert checkins_to(h, STUDENT_ID) == []
    assert h.db.get_checkins(STUDENT_ID)["next_at"] > datetime.now(UTC)  # put off to tomorrow

    make_due(h, STUDENT_ID)
    h.db._run("INSERT INTO assessments (title, source_text, created_by, created_at) VALUES ('x', 'x', 1, 'x')")
    h.db._run("INSERT INTO reminders (assessment_id, user_id, status, sent_at) VALUES (1, %s, 'sent', now())",
              (STUDENT_ID,))
    await work.run_once(h.app.bot, svc(h))
    assert checkins_to(h, STUDENT_ID) == []

    make_due(h, STUDENT_ID)
    h.db._run("UPDATE reminders SET sent_at = now() - interval '2 days'")
    await work.run_once(h.app.bot, svc(h))
    (text, buttons), = checkins_to(h, STUDENT_ID)
    assert buttons == ["chk:pause"]  # nothing open: no /pending button


async def test_unanswered_check_ins_come_less_often_until_the_student_replies(harness):
    h = harness
    await opted_in(h)
    for expected_gap in (3, 6, 12):
        make_due(h, STUDENT_ID)
        h.db._run("UPDATE study_checkins SET last_sent_at = now() - interval '1 day' "
                  "WHERE user_id = %s AND last_sent_at IS NOT NULL", (STUDENT_ID,))
        await work.run_once(h.app.bot, svc(h))
        gap = h.db.get_checkins(STUDENT_ID)["next_at"] - datetime.now(UTC)
        assert timedelta(days=expected_gap - 1) < gap <= timedelta(days=expected_gap + 1), expected_gap
    assert len(checkins_to(h, STUDENT_ID)) == 3

    await h.text(STUDENT_ID, "thanks, I'll revise tonight")  # a reply
    h.db._run("UPDATE users SET last_active_at = now() - interval '13 hours'")
    h.db._run("UPDATE study_checkins SET next_at = now() - interval '1 minute', "
              "last_sent_at = now() - interval '2 days'")
    await work.run_once(h.app.bot, svc(h))
    row = h.db.get_checkins(STUDENT_ID)
    assert row["ignored"] == 1 and row["next_at"] - datetime.now(UTC) <= timedelta(days=4)


async def test_a_built_in_message_when_the_ai_is_down(harness):
    h = harness
    await opted_in(h)
    make_due(h, STUDENT_ID)
    h.ai.error = AIUnavailable("Could not reach the AI provider.")
    await work.send_checkins(h.app.bot, svc(h))
    (text, _), = checkins_to(h, STUDENT_ID)
    assert "Hi User2002! A quick check-in" in text and "reply here" in text
    assert h.db.checkin_overview()["fallback"] == 1

    # Once Stage 5 counts the provider as down, check-ins do not even try it.
    h.db._run("UPDATE ai_health SET consecutive_failures = 5")
    h.ai.error = None
    calls = len(h.ai.calls)
    make_due(h, STUDENT_ID)
    await work.send_checkins(h.app.bot, svc(h))
    assert len(checkins_to(h, STUDENT_ID)) == 2 and len(h.ai.calls) == calls


async def test_a_student_who_blocked_the_bot_is_not_messaged_again(harness):
    h = harness
    await opted_in(h)
    make_due(h, STUDENT_ID)
    h.tg.fail = lambda method, params: (403, "Forbidden: bot was blocked by the user") \
        if method == "sendMessage" else None
    await work.send_checkins(h.app.bot, svc(h))
    assert h.db.get_checkins(STUDENT_ID)["status"] == "blocked"
    h.tg.fail = None
    h.db._run("UPDATE study_checkins SET next_at = now() - interval '1 minute'")
    calls = len(h.ai.calls)
    await work.send_checkins(h.app.bot, svc(h))
    assert checkins_to(h, STUDENT_ID) == [] and len(h.ai.calls) == calls

    text, _ = await command(h, ADMIN_ID, "/status")
    assert "<b>Study check-ins</b>" in text and "1 stopped because the student blocked the bot" in text


async def test_a_check_in_cut_off_by_a_crash_is_not_sent_twice(make_harness):
    a = await make_harness()
    await opted_in(a)
    make_due(a, STUDENT_ID)

    def die(method, params):
        if method == "sendMessage" and params.get("text", "").startswith("💬"):
            raise Crash()
    a.tg.fail = die
    with pytest.raises(Crash):
        await work.send_checkins(a.app.bot, svc(a))

    b = await make_harness()  # the next process
    await work.run_once(b.app.bot, svc(b))
    assert checkins_to(b, STUDENT_ID) == []  # at most once: that one is lost, not repeated
    assert b.db.get_checkins(STUDENT_ID)["next_at"] > datetime.now(UTC) + timedelta(days=2)


async def test_status_shows_check_ins(harness):
    h = harness
    await opted_in(h)
    make_due(h, STUDENT_ID)
    await work.send_checkins(h.app.bot, svc(h))
    text, _ = await command(h, ADMIN_ID, "/status")
    assert "1 student(s) receive them" in text and "📨 1 sent in the last 7 days" in text
    assert "built-in" not in text


def test_two_processes_claiming_at_the_same_moment_send_one_check_in(pg_url):
    """A real race: two connection pools in two threads, each holding the row while it decides."""
    import threading
    import time

    from bot.db import Database
    first, second = Database(pg_url), Database(pg_url)
    try:
        first.upsert_user(STUDENT_ID, "u", "Student")
        first.set_checkins(STUDENT_ID, "on", datetime.now(UTC) - timedelta(minutes=1))
        first._run("UPDATE users SET last_active_at = now() - interval '2 days'")
        settings = make_settings(pg_url)
        start = threading.Barrier(2)
        results = []

        def decide(row):
            time.sleep(0.3)  # both are now inside their transaction
            return checkins.decide(row, datetime.now(UTC), settings)

        def claim(db):
            start.wait()
            results.append(db.claim_checkin(decide, checkins.day_start(datetime.now(UTC), UTC)))

        threads = [threading.Thread(target=claim, args=(db,)) for db in (first, second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(r["action"] if r else "nothing due" for r in results) == ["nothing due", "send"]
        assert first.checkin_overview()["sent"] == 1
    finally:
        first.close()
        second.close()


# ── the one-time announcement to students who were never asked ──────────
ANNOUNCED = "New: study check-ins"
EARLIER = [4001, 4002, 4003]


def announcements_to(harnesses, user_id):
    return sum(ANNOUNCED in t for h in harnesses for t in h.tg.texts(user_id))


def registered_before_check_ins(h, user_ids):
    """Users the bot knew before the feature existed: no study_checkins row."""
    for user_id in user_ids:
        h.db.upsert_user(user_id, f"u{user_id}", f"Early {user_id}")


async def test_students_registered_before_check_ins_are_asked_once(make_harness):
    a = await make_harness()
    b = await make_harness()
    registered_before_check_ins(a, [*EARLIER, ADMIN_ID, 4004, 4005])
    a.db.set_user_active(4004, False)              # blocked the bot earlier: not messaged
    a.db.set_checkins(4005, "off")                 # already chose: not asked again
    await opted_in(a, STUDENT_ID)                  # asked at /start: not asked again

    await asyncio.gather(work.run_once(a.app.bot, svc(a)), work.run_once(b.app.bot, svc(b)))
    await work.run_once(a.app.bot, svc(a))
    await work.run_once(b.app.bot, svc(b))         # later passes, and the other process: nothing new
    for user_id in EARLIER:
        assert announcements_to((a, b), user_id) == 1, user_id
        assert a.db.get_checkins(user_id)["status"] == "offered"
    for user_id in (ADMIN_ID, 4004, 4005, STUDENT_ID):
        assert announcements_to((a, b), user_id) == 0, user_id

    (text, buttons), = [(t, k) for h in (a, b) for t, k in sent(h, EARLIER[0]) if ANNOUNCED in t]
    assert "every 3 days" in text and buttons == ["chk:on", "chk:off"]
    harness = a if any(ANNOUNCED in t for t in a.tg.texts(EARLIER[0])) else b
    await harness.press(EARLIER[0], "chk:on")
    assert a.db.get_checkins(EARLIER[0])["status"] == "on"


async def test_the_announcement_is_spread_over_passes(harness):
    h = harness
    many = list(range(5000, 5000 + work.CHECKIN_OFFERS_PER_PASS + 5))
    registered_before_check_ins(h, many)
    await work.announce_checkins(h.app.bot, svc(h))
    assert sum(announcements_to([h], u) for u in many) == work.CHECKIN_OFFERS_PER_PASS
    await work.announce_checkins(h.app.bot, svc(h))
    assert all(announcements_to([h], u) == 1 for u in many)


async def test_a_student_who_blocked_the_bot_is_marked_by_the_announcement(harness):
    h = harness
    registered_before_check_ins(h, [EARLIER[0]])
    h.tg.fail = lambda method, params: (403, "Forbidden: bot was blocked by the user") \
        if method == "sendMessage" else None
    await work.announce_checkins(h.app.bot, svc(h))
    assert h.db.get_checkins(EARLIER[0])["status"] == "blocked"
    assert h.db.active_user_ids() == []


async def test_no_announcement_when_check_ins_are_switched_off(make_harness):
    h = await make_harness(checkin_days=0)
    registered_before_check_ins(h, EARLIER)
    await work.run_once(h.app.bot, svc(h))
    assert all(announcements_to([h], u) == 0 for u in EARLIER)
    assert h.db.get_checkins(EARLIER[0]) is None  # still to be asked once they are switched on


def test_two_processes_announcing_at_once_take_each_student_once(pg_url):
    import threading

    from bot.db import Database
    first, second = Database(pg_url), Database(pg_url)
    try:
        users = list(range(6000, 6200))
        for user_id in users:
            first.upsert_user(user_id, None, None)
        start = threading.Barrier(2)
        taken = {}

        def claim(name, db):
            start.wait()
            got = []
            for _ in range(20):  # 200 users, 25 per claim: bounded so a repeating claim fails, not hangs
                ids = db.claim_checkin_offers(25, [ADMIN_ID])
                if not ids:
                    break
                got += ids
            taken[name] = got

        threads = [threading.Thread(target=claim, args=(n, db)) for n, db in (("a", first), ("b", second))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(taken["a"] + taken["b"]) == users  # everyone once, nobody twice
    finally:
        first.close()
        second.close()
