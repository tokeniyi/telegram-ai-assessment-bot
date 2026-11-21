"""Stage 3: sending and grading survive a restart, and a published assessment reaches each user once.

A "restart" is two harnesses on one database: the first one stops mid-work (a Crash raised from
the fake Telegram API or AI provider stands for the process dying), and the second one, a fresh
process, runs the background work that the bot runs on start. Leases are expired with SQL to
stand for the time that passes before the new process may take abandoned work.
"""

import asyncio
import json
import threading

import psycopg
import pytest

from bot.ai import AIError
from bot.db import Database
from bot.handlers import work
from tests.conftest import ADMIN_ID, STUDENT_ID, Crash
from tests.test_flow import MATERIAL, publish

STUDENTS = [3001, 3002, 3003, 3004, 3005]
QUESTION = "Which organelle"  # appears in the assessment message every user receives


def received(h, user_id):
    """How many times this user was sent the published assessment (not the admin's draft preview)."""
    return sum(QUESTION in t and "Draft #" not in t for t in h.tg.texts(user_id))


def received_anywhere(harnesses, user_id):
    return sum(received(h, user_id) for h in harnesses)


async def draft_with_students(h, students=STUDENTS):
    await h.text(ADMIN_ID, "/start")
    for user_id in students:
        await h.text(user_id, "/start")
    await h.document(ADMIN_ID, "notes.txt", MATERIAL)


def expire_delivery_leases(db):
    db._run("UPDATE deliveries SET claimed_until = now() - interval '1 second' WHERE claimed_until IS NOT NULL")


def expire_grading_leases(db):
    db._run("UPDATE submissions SET grading_claimed_until = now() - interval '1 second' "
            "WHERE grading_claimed_until IS NOT NULL")


def crash_on_nth_send(n, to=None):
    """A fail hook that kills the 'process' at its n-th assessment message (to `to` if given)."""
    seen = {"n": 0}

    def fail(method, params):
        text = params.get("text", "")
        if method == "sendMessage" and QUESTION in text and "Draft #" not in text \
                and (to is None or int(params["chat_id"]) == to):
            seen["n"] += 1
            if seen["n"] == n:
                raise Crash()
        return None
    return fail


# ── the double tap on "Send to all users" ────────────────────────────────
async def test_two_simultaneous_publish_taps_send_once(harness):
    h = harness
    await draft_with_students(h)
    await asyncio.gather(h.press(ADMIN_ID, "pub:1"), h.press(ADMIN_ID, "pub:1"))
    for user_id in STUDENTS + [ADMIN_ID]:
        assert received(h, user_id) == 1, user_id
    reports = [t for t in h.tg.texts(ADMIN_ID) if "sent to" in t]
    assert reports == [reports[0]] and "sent to 6 users" in reports[0]
    assert any("already published" in p.get("text", "") for m, p in h.tg.calls if m == "answerCallbackQuery")


async def test_publish_taps_in_two_processes_send_once(make_harness):
    a = await make_harness()
    await draft_with_students(a)
    b = await make_harness()  # a second bot version running during a deploy
    await asyncio.gather(a.press(ADMIN_ID, "pub:1"), b.press(ADMIN_ID, "pub:1"))
    for user_id in STUDENTS + [ADMIN_ID]:
        assert received_anywhere([a, b], user_id) == 1, user_id
    reports = [t for h in (a, b) for t in h.tg.texts(ADMIN_ID) if "sent to" in t]
    assert len(reports) == 1


async def test_discard_after_publish_does_not_delete_the_published_assessment(harness):
    h = harness
    await draft_with_students(h)
    await h.press(ADMIN_ID, "pub:1")
    assert h.db.discard_draft_assessment(1) is False  # a Discard tap racing the Send tap
    assert h.db.get_assessment(1)["status"] == "published"


async def test_regenerate_that_finishes_after_publish_does_not_change_the_questions(harness):
    h = harness
    await draft_with_students(h)
    original = [q["text"] for q in h.db.get_questions(1)]
    complete = h.ai.complete

    async def publish_meanwhile(system, user):
        h.db.publish_assessment(1, ADMIN_ID)  # Send was tapped while the AI was writing
        reply = json.loads(await complete(system, user))
        reply["questions"][0]["question"] = "A different question?"
        return json.dumps(reply)

    h.ai.complete = publish_meanwhile
    await h.press(ADMIN_ID, "regen:1")
    assert [q["text"] for q in h.db.get_questions(1)] == original
    assert "were not used" in h.tg.texts(ADMIN_ID)[-1]


# ── sending survives a restart ───────────────────────────────────────────
async def test_a_restart_mid_send_is_finished_by_the_next_process(make_harness):
    old = await make_harness()
    await draft_with_students(old)
    old.tg.fail = crash_on_nth_send(3)
    with pytest.raises(Crash):
        await old.press(ADMIN_ID, "pub:1")
    got_it = [u for u in [ADMIN_ID] + STUDENTS if received(old, u)]
    assert len(got_it) == 2  # the process died while sending the third message

    new = await make_harness()
    await work.run_once(new.app.bot, new.app.bot_data["services"])
    # the message the old process was sending is still leased to it: nobody else sends it yet
    assert sum(received(new, u) for u in [ADMIN_ID] + STUDENTS) == 3

    expire_delivery_leases(new.db)
    await work.run_once(new.app.bot, new.app.bot_data["services"])
    for user_id in [ADMIN_ID] + STUDENTS:
        assert received_anywhere([old, new], user_id) == 1, user_id
    # the publisher is told once, by the process that finished
    assert [t for t in new.tg.texts(ADMIN_ID) if "sent to" in t] == [work.delivery_report(1, {"sent": 6})]
    assert not [t for t in old.tg.texts(ADMIN_ID) if "sent to" in t]

    await work.run_once(new.app.bot, new.app.bot_data["services"])  # nothing left: nothing more is sent
    assert sum(received(new, u) for u in [ADMIN_ID] + STUDENTS) == 4


async def test_two_processes_sending_the_same_assessment_split_the_work(make_harness):
    many = list(range(4001, 4021))
    a = await make_harness()
    await draft_with_students(a, many)
    a.db.publish_assessment(1, ADMIN_ID)
    b = await make_harness()
    svc_a, svc_b = a.app.bot_data["services"], b.app.bot_data["services"]
    await asyncio.gather(work.deliver(a.app.bot, svc_a), work.deliver(b.app.bot, svc_b))
    for user_id in many + [ADMIN_ID]:
        assert received_anywhere([a, b], user_id) == 1, user_id
    assert received(a, many[0]) + received(b, many[0]) == 1
    assert a.db.delivery_counts(1) == {"sent": 21}


async def test_a_user_who_blocked_the_bot_is_skipped_and_reported(harness):
    h = harness
    await draft_with_students(h)
    h.tg.fail = lambda m, p: (403, "Forbidden: bot was blocked by the user") \
        if m == "sendMessage" and int(p["chat_id"]) == STUDENTS[1] else None
    await h.press(ADMIN_ID, "pub:1")
    report = h.tg.texts(ADMIN_ID)[-1]
    assert "sent to 5 users" in report and "1 blocked the bot" in report
    assert STUDENTS[1] not in h.db.active_user_ids()


async def test_a_failed_send_is_retried_later(harness):
    h = harness
    await draft_with_students(h)
    flaky = {"left": 1}

    def fail(method, params):
        if method == "sendMessage" and int(params["chat_id"]) == STUDENTS[0] and flaky["left"]:
            flaky["left"] -= 1
            return 500, "Internal Server Error"
        return None

    h.tg.fail = fail
    await h.press(ADMIN_ID, "pub:1")
    assert received(h, STUDENTS[0]) == 0
    assert not [t for t in h.tg.texts(ADMIN_ID) if "sent to" in t]  # not finished: one is waiting

    svc = h.app.bot_data["services"]
    await work.run_once(h.app.bot, svc)  # too early: the retry waits DELIVERY_RETRY_DELAY
    assert received(h, STUDENTS[0]) == 0
    expire_delivery_leases(h.db)
    await work.run_once(h.app.bot, svc)
    assert received(h, STUDENTS[0]) == 1
    assert "sent to 6 users" in [t for t in h.tg.texts(ADMIN_ID) if "sent to" in t][-1]


async def test_a_send_that_keeps_failing_gives_up_after_the_limit(harness):
    h = harness
    await draft_with_students(h)
    h.tg.fail = lambda m, p: (400, "Bad Request: chat not found") \
        if m == "sendMessage" and int(p["chat_id"]) == STUDENTS[0] else None
    await h.press(ADMIN_ID, "pub:1")
    svc = h.app.bot_data["services"]
    for _ in range(work.MAX_DELIVERY_ATTEMPTS + 2):
        expire_delivery_leases(h.db)
        await work.run_once(h.app.bot, svc)
    counts = h.db.delivery_counts(1)
    assert counts == {"sent": 5, "failed": 1}
    attempts = h.db._one("SELECT attempts FROM deliveries WHERE user_id = %s", (STUDENTS[0],))["attempts"]
    assert attempts == work.MAX_DELIVERY_ATTEMPTS
    assert "1 could not be reached" in h.tg.texts(ADMIN_ID)[-1]


async def test_closing_during_sending_stops_the_rest(make_harness):
    old = await make_harness()
    await draft_with_students(old)
    old.tg.fail = crash_on_nth_send(2)
    with pytest.raises(Crash):
        await old.press(ADMIN_ID, "pub:1")
    new = await make_harness()
    await new.text(ADMIN_ID, "/close 1")
    expire_delivery_leases(new.db)
    await work.run_once(new.app.bot, new.app.bot_data["services"])
    assert sum(received(new, u) for u in STUDENTS) == 0
    report = new.tg.texts(ADMIN_ID)[-1]
    assert "sent to 1 user." in report and "5 were not sent because the assessment was closed" in report


async def test_users_who_join_after_publishing_are_not_sent_it_twice(harness):
    h = harness
    await draft_with_students(h)
    await h.press(ADMIN_ID, "pub:1")
    await h.text(9999, "/start")  # joins later: sees it in /pending, is not sent it
    await work.run_once(h.app.bot, h.app.bot_data["services"])
    assert received(h, 9999) == 0
    await h.text(9999, "/pending")
    assert h.tg.last_keyboard(9999) == ["show:1"]


# ── grading survives a restart ───────────────────────────────────────────
async def submit_and_crash_while_grading(make_harness):
    old = await make_harness()
    await publish(old)
    await old.text(STUDENT_ID, "1. B")
    await old.text(STUDENT_ID, "2) water moving")
    old.ai.error = Crash()  # the process dies while waiting for the AI
    with pytest.raises(Crash):
        await old.press(STUDENT_ID, "ans:submit:1")
    assert old.db.get_submission(1)["status"] == "pending"
    return old


async def test_grading_interrupted_by_a_restart_is_finished_automatically(make_harness):
    old = await submit_and_crash_while_grading(make_harness)
    new = await make_harness()
    svc = new.app.bot_data["services"]

    await work.run_once(new.app.bot, svc)
    assert new.db.get_submission(1)["status"] == "pending"  # still leased to the old process
    assert new.ai.calls == []

    expire_grading_leases(new.db)
    await work.run_once(new.app.bot, svc)
    submission = new.db.get_submission(1)
    assert submission["status"] == "graded" and submission["score"] == 4
    assert submission["grading_attempts"] == 2 and submission["result_sent_at"] is not None
    results = [t for t in new.tg.texts(STUDENT_ID) if "Result" in t]
    assert len(results) == 1 and "4 / 4" in results[0]
    assert "1. B\n2) water moving" in new.ai.calls[-1][1]  # graded on what the student submitted

    await work.run_once(new.app.bot, svc)
    assert len([t for t in new.tg.texts(STUDENT_ID) if "Result" in t]) == 1
    assert not any("/regrade" in t for t in new.tg.texts(ADMIN_ID))  # no admin action needed


async def test_grading_that_keeps_being_interrupted_is_left_for_the_admin(make_harness):
    old = await submit_and_crash_while_grading(make_harness)
    for _ in range(work.MAX_GRADING_ATTEMPTS - 1):  # each new process dies while grading too
        expire_grading_leases(old.db)
        h = await make_harness()
        h.ai.error = Crash()
        with pytest.raises(Crash):
            await work.recover_grading(h.app.bot, h.app.bot_data["services"])
    assert old.db.get_submission(1)["grading_attempts"] == work.MAX_GRADING_ATTEMPTS

    last = await make_harness()
    expire_grading_leases(last.db)
    await work.run_once(last.app.bot, last.app.bot_data["services"])
    assert last.db.get_submission(1)["status"] == "failed"
    assert last.ai.calls == []  # not tried a fourth time
    assert "/regrade 1" in last.tg.texts(ADMIN_ID)[-1]
    assert "grading is temporarily unavailable" in last.tg.texts(STUDENT_ID)[-1]

    await last.text(ADMIN_ID, "/regrade 1")  # the admin can still grade it
    assert last.db.get_submission(1)["status"] == "graded"
    assert "Result" in last.tg.texts(STUDENT_ID)[-1]


async def test_recovered_grading_that_fails_is_reported_like_any_failure(make_harness):
    await submit_and_crash_while_grading(make_harness)
    new = await make_harness()
    new.ai.error = AIError("provider down")
    expire_grading_leases(new.db)
    await work.run_once(new.app.bot, new.app.bot_data["services"])
    assert new.db.get_submission(1)["status"] == "failed"
    assert "provider down" in new.tg.texts(ADMIN_ID)[-1] and "/regrade 1" in new.tg.texts(ADMIN_ID)[-1]
    assert "temporarily unavailable" in new.tg.texts(STUDENT_ID)[-1]


async def test_a_result_lost_by_a_restart_is_sent_later_once(make_harness):
    old = await make_harness()
    await publish(old)
    await old.text(STUDENT_ID, "1. B")

    def crash_on_result(method, params):
        if method == "sendMessage" and "Result" in params.get("text", ""):
            raise Crash()
    old.tg.fail = crash_on_result
    with pytest.raises(Crash):
        await old.press(STUDENT_ID, "ans:submit:1")
    assert old.db.get_submission(1)["status"] == "graded"
    assert old.db.get_submission(1)["result_sent_at"] is None

    new = await make_harness()
    svc = new.app.bot_data["services"]
    await work.run_once(new.app.bot, svc)
    assert not [t for t in new.tg.texts(STUDENT_ID) if "Result" in t]  # within the grace period
    new.db._run("UPDATE submissions SET graded_at = '2000-01-01T00:00:00+00:00'")
    await work.run_once(new.app.bot, svc)
    await work.run_once(new.app.bot, svc)
    results = [t for t in new.tg.texts(STUDENT_ID) if "Result" in t]
    assert len(results) == 1 and "4 / 4" in results[0]
    assert new.ai.calls == []  # the stored grade is sent; nothing is graded again


async def test_regrade_leaves_a_submission_that_is_being_graded_alone(make_harness):
    old = await submit_and_crash_while_grading(make_harness)
    await old.text(ADMIN_ID, "/regrade 1")
    assert "were being graded already" in old.tg.texts(ADMIN_ID)[-1]
    assert old.db.get_submission(1)["status"] == "pending"

    expire_grading_leases(old.db)  # abandoned: the admin may take it
    old.ai.error = None
    await old.text(ADMIN_ID, "/regrade 1")
    assert old.db.get_submission(1)["status"] == "graded"
    assert len([t for t in old.tg.texts(STUDENT_ID) if "Result" in t]) == 1


async def test_a_normal_submission_is_graded_once_and_not_picked_up_again(harness):
    h = harness
    await publish(h)
    await h.text(STUDENT_ID, "1. B")
    await h.press(STUDENT_ID, "ans:submit:1")
    calls = len(h.ai.calls)
    submission = h.db.get_submission(1)
    assert submission["status"] == "graded" and submission["result_sent_at"] is not None
    h.db._run("UPDATE submissions SET graded_at = '2000-01-01T00:00:00+00:00'")
    await work.run_once(h.app.bot, h.app.bot_data["services"])
    assert len(h.ai.calls) == calls
    assert len([t for t in h.tg.texts(STUDENT_ID) if "Result" in t]) == 1


async def test_background_work_survives_an_error_and_stops_cleanly(harness, monkeypatch, caplog):
    h = harness
    calls = []

    async def broken(*args):
        calls.append("deliver")
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(work, "deliver", broken)
    work.start(h.app)
    for _ in range(50):
        if calls:
            break
        await asyncio.sleep(0.01)
    task = h.app.bot_data["work_task"]
    assert calls and not task.done()  # the error was logged, the loop goes on
    assert "Background sending failed" in caplog.text
    await work.stop(h.app)
    assert task.cancelled() and "work_task" not in h.app.bot_data


# ── found in review: edge cases ──────────────────────────────────────────
async def test_an_assessment_closed_by_the_old_version_is_still_reported(harness):
    """During a deploy the old version's /close only changes the status; the pending rows it
    leaves must not block the publisher's report forever."""
    h = harness
    await draft_with_students(h)
    h.db.publish_assessment(1, ADMIN_ID)
    h.db.set_status(1, "closed")  # what the previous version's /close does
    await work.run_once(h.app.bot, h.app.bot_data["services"])
    assert sum(received(h, u) for u in STUDENTS) == 0
    assert h.db.delivery_counts(1) == {"cancelled": 6}
    assert "6 were not sent because the assessment was closed" in h.tg.texts(ADMIN_ID)[-1]


async def test_a_message_being_sent_when_the_assessment_closes_counts_as_sent(harness):
    h = harness
    await draft_with_students(h, STUDENTS[:1])

    def close_meanwhile(method, params):
        if method == "sendMessage" and int(params["chat_id"]) == STUDENTS[0] and QUESTION in params["text"]:
            h.db.close_assessment(1)  # /close lands while this message is on its way
        return None

    h.tg.fail = close_meanwhile
    await h.press(ADMIN_ID, "pub:1")
    assert h.db._one("SELECT status FROM deliveries WHERE user_id = %s", (STUDENTS[0],))["status"] == "sent"


async def test_regrade_claims_each_submission_only_when_it_grades_it(make_harness):
    """A long /regrade batch must not hold leases on submissions it has not reached yet, or they
    run out and the background work grades (or gives up on) them at the same time."""
    h = await make_harness(allow_resubmission=True)
    await publish(h)
    h.ai.error = AIError("provider down")
    for answer in ("1. B", "1. A"):
        await h.text(STUDENT_ID, answer)
        await h.press(STUDENT_ID, "ans:submit:1")
    assert [h.db.get_submission(i)["status"] for i in (1, 2)] == ["failed", "failed"]
    h.ai.error = None
    seen = []
    complete = h.ai.complete

    async def watch(system, user):
        if not system.startswith("You are the chat assistant"):
            seen.append(h.db.get_submission(2)["status"])
        return await complete(system, user)

    h.ai.complete = watch
    await h.text(ADMIN_ID, "/regrade 1")
    assert seen[0] == "failed"  # while grading the first, the second is not claimed yet
    assert [h.db.get_submission(i)["status"] for i in (1, 2)] == ["graded", "graded"]
    assert "2 user(s) received their result" in h.tg.texts(ADMIN_ID)[-1]


async def test_a_result_sent_by_the_old_version_is_not_sent_again(harness):
    """The previous version's /regrade saves a grade and sends it without knowing about
    result_due or result_sent_at; the new version must not send it a second time."""
    h = harness
    await publish(h)
    h.ai.error = AIError("provider down")
    await h.text(STUDENT_ID, "1. B")
    await h.press(STUDENT_ID, "ans:submit:1")
    h.db._run("""UPDATE submissions SET status = 'graded', score = 1, max_score = 4, feedback = '',
                 details = '[]', graded_at = '2000-01-01T00:00:00+00:00' WHERE id = 1""")  # old save_grade
    await work.run_once(h.app.bot, h.app.bot_data["services"])
    assert not [t for t in h.tg.texts(STUDENT_ID) if "Result" in t]


async def test_a_timed_out_send_is_not_repeated(harness):
    """Telegram may have delivered a message whose request timed out: no second copy."""
    from telegram.error import TimedOut
    h = harness
    await draft_with_students(h, STUDENTS[:1])

    def time_out(method, params):
        if method == "sendMessage" and int(params["chat_id"]) == STUDENTS[0] and QUESTION in params["text"]:
            raise TimedOut()
        return None

    h.tg.fail = time_out
    await h.press(ADMIN_ID, "pub:1")
    h.tg.fail = None
    expire_delivery_leases(h.db)
    await work.run_once(h.app.bot, h.app.bot_data["services"])
    assert received(h, STUDENTS[0]) == 0
    assert h.db.delivery_counts(1) == {"sent": 1, "failed": 1}
    assert "1 could not be reached" in h.tg.texts(ADMIN_ID)[-1]


async def test_a_partly_sent_long_assessment_is_not_repeated(harness):
    h = harness
    for user_id in (ADMIN_ID, STUDENTS[0]):
        await h.text(user_id, "/start")
    long_questions = [{"type": "short", "question": f"{QUESTION} {i}? " + "x" * 900, "answer": "a", "points": 1}
                      for i in range(8)]
    aid = h.db.create_assessment("Long", None, "src", None, ADMIN_ID, long_questions)
    sends = {"n": 0}

    def fail_second_part(method, params):
        if method == "sendMessage" and int(params["chat_id"]) == STUDENTS[0]:
            sends["n"] += 1
            if sends["n"] == 2:
                return 500, "Internal Server Error"
        return None

    h.tg.fail = fail_second_part
    before = len(h.tg.texts(STUDENTS[0]))
    await h.press(ADMIN_ID, f"pub:{aid}")
    first_part = len(h.tg.texts(STUDENTS[0]))
    assert first_part == before + 1  # only the first of several messages arrived
    h.tg.fail = None
    expire_delivery_leases(h.db)
    await work.run_once(h.app.bot, h.app.bot_data["services"])
    assert len(h.tg.texts(STUDENTS[0])) == first_part  # the first part is not sent again
    assert h.db._one("SELECT status FROM deliveries WHERE user_id = %s", (STUDENTS[0],))["status"] == "failed"


async def test_publishing_to_nobody_still_reports(harness, monkeypatch):
    """Rare in practice (the admin pressing Send is an active user), but the admin must still get
    a report when the queue is empty."""
    h = harness
    await h.document(ADMIN_ID, "notes.txt", MATERIAL)
    publish_for_real = h.db.publish_assessment

    def publish_to_nobody(assessment_id, published_by):
        publish_for_real(assessment_id, published_by)
        h.db._run("DELETE FROM deliveries")
        return 0

    monkeypatch.setattr(h.db, "publish_assessment", publish_to_nobody)
    await h.press(ADMIN_ID, "pub:1")
    assert "sent to 0 users" in h.tg.texts(ADMIN_ID)[-1]
    await work.run_once(h.app.bot, h.app.bot_data["services"])
    assert sum("sent to" in t for t in h.tg.texts(ADMIN_ID)) == 1


async def test_shutdown_stops_the_background_work_even_without_stop(harness):
    """If start-up fails after post_init, PTB skips post_stop; post_shutdown must still stop it."""
    h = harness
    work.start(h.app)
    task = h.app.bot_data["work_task"]
    await h.app.post_shutdown(h.app)
    assert task.cancelled()


# ── the database side ────────────────────────────────────────────────────
QUESTIONS = [{"type": "short", "question": "Q?", "answer": "A", "points": 1}]


@pytest.fixture
def db(pg_url):
    database = Database(pg_url)
    yield database
    database.close()


def test_publishing_queues_every_active_user_once(db):
    for user_id in (1, 2, 3):
        db.upsert_user(user_id, None, None)
    db.set_user_active(3, False)
    aid = db.create_assessment("T", None, "src", None, 1, QUESTIONS)
    assert db.publish_assessment(aid, 1) == 2
    assert db.publish_assessment(aid, 1) is None
    assert db.get_assessment(aid)["published_by"] == 1
    assert db.delivery_counts(aid) == {"pending": 2}


def test_concurrent_publishes_have_one_winner(db):
    for user_id in range(1, 11):
        db.upsert_user(user_id, None, None)
    aid = db.create_assessment("T", None, "src", None, 1, QUESTIONS)
    results, barrier = [], threading.Barrier(6)

    def go():
        barrier.wait()
        results.append(db.publish_assessment(aid, 1))

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results, key=str) == [10] + [None] * 5
    assert db.delivery_counts(aid) == {"pending": 10}


def test_concurrent_claims_never_hand_out_the_same_delivery(db):
    for user_id in range(1, 41):
        db.upsert_user(user_id, None, None)
    aid = db.create_assessment("T", None, "src", None, 1, QUESTIONS)
    db.publish_assessment(aid, 1)
    claimed, lock, barrier = [], threading.Lock(), threading.Barrier(8)

    def worker():
        barrier.wait()
        while (row := db.claim_delivery(300)) is not None:
            with lock:
                claimed.append(row["user_id"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == list(range(1, 41))


def test_the_report_is_claimed_once_and_only_when_sending_is_done(db):
    db.upsert_user(1, None, None)
    aid = db.create_assessment("T", None, "src", None, 1, QUESTIONS)
    db.publish_assessment(aid, 1)
    assert db.claim_delivery_reports() == []
    row = db.claim_delivery(300)
    db.finish_delivery(aid, row["user_id"], "sent")
    reports = db.claim_delivery_reports()
    assert [(r["id"], r["counts"]) for r in reports] == [(aid, {"sent": 1})]
    assert db.claim_delivery_reports() == []


def test_a_closed_assessment_is_not_claimed(db):
    db.upsert_user(1, None, None)
    aid = db.create_assessment("T", None, "src", None, 1, QUESTIONS)
    db.publish_assessment(aid, 1)
    assert db.close_assessment(aid) is True
    assert db.close_assessment(aid) is False
    assert db.claim_delivery(300) is None
    assert db.delivery_counts(aid) == {"cancelled": 1}


def test_stale_grading_is_claimed_once_with_a_limit(db):
    aid = db.create_assessment("T", None, "src", None, 1, QUESTIONS)
    db.set_status(aid, "published")
    db.add_draft_part(5, aid, "1. A", 1000)
    _, sid = db.submit_draft(5, aid, False, lease_seconds=600)
    assert db.claim_stale_grading(600, 3) is None  # leased to the submitter
    db._run("UPDATE submissions SET grading_claimed_until = now() - interval '1 second'")
    outcome, row = db.claim_stale_grading(600, 3)
    assert (outcome, row["id"], row["grading_attempts"]) == ("claimed", sid, 2)
    assert db.claim_stale_grading(600, 3) is None  # now leased to the recoverer
    db._run("UPDATE submissions SET grading_claimed_until = now() - interval '1 second'")
    assert db.claim_stale_grading(600, 3)[0] == "claimed"
    db._run("UPDATE submissions SET grading_claimed_until = now() - interval '1 second'")
    outcome, row = db.claim_stale_grading(600, 3)
    assert outcome == "gave_up" and db.get_submission(sid)["status"] == "failed"
    assert db.claim_stale_grading(600, 3) is None


def test_a_grade_is_saved_once(db):
    aid = db.create_assessment("T", None, "src", None, 1, QUESTIONS)
    sid = db.create_submission(aid, 5, "x")
    assert db.save_grade(sid, 1, 1, "ok", []) is True
    assert db.save_grade(sid, 0, 1, "late copy", []) is False
    assert db.get_submission(sid)["feedback"] == "ok"
    db.mark_submission_failed(sid)  # a late failure does not undo a grade
    assert db.get_submission(sid)["status"] == "graded"


def test_upgrading_a_database_from_before_stage_3(pg_url):
    """An existing database gets the new columns, and nothing an older version did is redone:
    its sent results are not resent and its published assessments are not reported again."""
    Database(pg_url).close()
    with psycopg.connect(pg_url, autocommit=True) as conn:  # back to the Stage 2 schema
        conn.execute("DROP TABLE deliveries")
        for table, column in (("assessments", "published_by"), ("assessments", "delivery_reported_at"),
                              ("submissions", "grading_attempts"), ("submissions", "grading_claimed_until"),
                              ("submissions", "result_sent_at")):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        aid = conn.execute(
            "INSERT INTO assessments (title, source_text, status, created_by, created_at) "
            "VALUES ('Old', 'src', 'published', 1, '2026-01-01T00:00:00+00:00') RETURNING id").fetchone()[0]
        conn.execute("INSERT INTO submissions (assessment_id, user_id, raw_text, status, created_at, graded_at, details) "
                     "VALUES (%s, 5, 'x', 'graded', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '[]')", (aid,))
        old_pending = conn.execute(
            "INSERT INTO submissions (assessment_id, user_id, raw_text, created_at) "
            "VALUES (%s, 6, 'y', '2026-01-01T00:00:00+00:00') RETURNING id", (aid,)).fetchone()[0]
        recent_pending = conn.execute(
            "INSERT INTO submissions (assessment_id, user_id, raw_text, created_at) "
            "VALUES (%s, 7, 'z', to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS+00:00')) RETURNING id",
            (aid,)).fetchone()[0]

    db = Database(pg_url)
    try:
        assert db.claim_unsent_result(0) is None  # graded by the old version: already sent by it
        assert db.claim_delivery_reports() == []
        outcome, row = db.claim_stale_grading(600, 3)  # stuck since January: recovered
        assert (outcome, row["id"]) == ("claimed", old_pending)
        assert db.claim_stale_grading(600, 3) is None  # recent one may still be graded by the old version
        assert db.get_submission(recent_pending)["status"] == "pending"
    finally:
        db.close()


def test_schema_can_be_applied_by_two_processes_at_once(pg_url):
    errors, barrier = [], threading.Barrier(4)

    def start():
        barrier.wait()
        try:
            Database(pg_url).close()
        except Exception as exc:  # pragma: no cover - the failure being tested for
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
