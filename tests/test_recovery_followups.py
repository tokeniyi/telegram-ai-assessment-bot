"""Stage 8: recovering from failures without the admin doing the work.

* /resend sends an open assessment again to the users whose message failed.
* Failed grading is retried by the bot itself once the AI provider answers again.

Driven through the bot with the fake Telegram API and AI provider. Time passing is simulated by
moving `failed_at` with SQL; the background work is run with work.run_once, as the bot does every
30 seconds. Two harnesses on one database stand for two bot processes.
"""

import asyncio
import threading

from telegram.error import TimedOut

from bot import alerts
from bot.ai import AIUnavailable
from bot.db import Database
from bot.handlers import work
from tests.conftest import ADMIN_ID, STUDENT_ID
from tests.test_guidance import command, sent
from tests.test_recovery import STUDENTS, draft_with_students, received

DOWN = AIUnavailable("Could not reach the AI provider.")


def svc(h):
    return h.app.bot_data["services"]


def time_passes(h, interval="1 hour"):
    """Make every failed grading, and every admin notice, look `interval` older."""
    h.db._run("UPDATE submissions SET failed_at = failed_at - %s::interval WHERE failed_at IS NOT NULL", (interval,))
    h.db._run("UPDATE admin_notices SET notified_at = notified_at - %s::interval", (interval,))


def results_to(h, user_id):
    return [t for t in h.tg.texts(user_id) if t.startswith("📊 <b>Result")]


def admin_notices(h, start):
    return [t for t in h.tg.texts(ADMIN_ID) if t.startswith(start)]


# ── /resend ──────────────────────────────────────────────────────────────
def timing_out_for(*users):
    """A fail hook: the assessment message to these users times out (Telegram may have it)."""
    def fail(method, params):
        if method == "sendMessage" and "Which organelle" in params.get("text", "") \
                and "Draft #" not in params["text"] and int(params["chat_id"]) in users:
            raise TimedOut()
        return None
    return fail


async def published_with_failures(h, failing=(STUDENTS[0],)):
    h.tg.fail = timing_out_for(*failing)
    await draft_with_students(h)
    await h.press(ADMIN_ID, "pub:1")
    h.tg.fail = None
    assert h.db.delivery_counts(1)["failed"] == len(failing)


async def test_the_publish_report_and_status_suggest_resend(harness):
    h = harness
    await published_with_failures(h)
    report = [t for t in h.tg.texts(ADMIN_ID) if t.startswith("📤 Assessment #1 sent to")][0]
    assert "1 could not be reached." in report and "Try them again: <code>/resend 1</code>" in report
    text, _ = await command(h, ADMIN_ID, "/status")
    assert "<code>/resend 1</code>" in text and "it could not reach" in text


async def test_resend_sends_again_only_to_those_who_failed(harness):
    h = harness
    await published_with_failures(h)
    before = {u: received(h, u) for u in STUDENTS}
    text, _ = await command(h, ADMIN_ID, "/resend 1")
    assert "sent again to 1 of 1" in text
    assert any("may receive it twice" in t for t in h.tg.texts(ADMIN_ID))
    assert received(h, STUDENTS[0]) == before[STUDENTS[0]] + 1
    assert all(received(h, u) == before[u] for u in STUDENTS[1:])
    assert h.db.delivery_counts(1).get("failed", 0) == 0

    text, _ = await command(h, ADMIN_ID, "/resend 1")  # nothing left
    assert text.startswith("ℹ️") and "nothing to resend" in text
    assert received(h, STUDENTS[0]) == before[STUDENTS[0]] + 1


async def test_resend_twice_at_once_sends_one_copy(make_harness):
    a = await make_harness()
    b = await make_harness()  # another bot process
    await published_with_failures(a, failing=tuple(STUDENTS[:3]))
    before = {u: received(a, u) for u in STUDENTS[:3]}
    await asyncio.gather(a.text(ADMIN_ID, "/resend 1"), b.text(ADMIN_ID, "/resend 1"))
    for user_id in STUDENTS[:3]:
        assert received(a, user_id) + received(b, user_id) == before[user_id] + 1, user_id


async def test_resend_that_fails_again_says_so(harness):
    h = harness
    await published_with_failures(h)
    h.tg.fail = timing_out_for(STUDENTS[0])
    text, _ = await command(h, ADMIN_ID, "/resend 1")
    assert "sent again to 0 of 1" in text and "1 still failed" in text
    assert h.db.delivery_counts(1)["failed"] == 1


async def test_resend_refuses_what_it_cannot_send(harness):
    h = harness
    await draft_with_students(h, [STUDENT_ID])
    text, _ = await command(h, ADMIN_ID, "/resend 1")
    assert text.startswith("ℹ️") and "still a draft" in text
    text, _ = await command(h, ADMIN_ID, "/resend")
    assert "nothing to resend" in text  # no id, and nothing failed: no invented example

    await h.press(ADMIN_ID, "pub:1")
    h.db._run("UPDATE deliveries SET status = 'failed' WHERE user_id = %s", (STUDENT_ID,))
    text, _ = await command(h, ADMIN_ID, "/resend")
    assert "<code>/resend 1</code>" in text  # the real id

    h.db._run("UPDATE assessments SET deadline = now() - interval '1 second' WHERE id = 1")
    text, buttons = await command(h, ADMIN_ID, "/resend 1")
    assert "deadline of #1 passed" in text and buttons == ["cmd:results:1"]
    assert h.db.delivery_counts(1)["failed"] == 1  # nothing queued

    await work.close_due(h.app.bot, svc(h))
    text, _ = await command(h, ADMIN_ID, "/resend 1")
    assert "is closed" in text


async def test_resend_skips_users_who_blocked_the_bot(harness):
    h = harness
    blocked = STUDENTS[1]

    def fail(method, params):
        if method == "sendMessage" and "Which organelle" in params.get("text", "") and "Draft #" not in params["text"]:
            if int(params["chat_id"]) == blocked:
                return 403, "Forbidden: bot was blocked by the user"
            if int(params["chat_id"]) == STUDENTS[0]:
                raise TimedOut()
        return None
    h.tg.fail = fail
    await draft_with_students(h)
    await h.press(ADMIN_ID, "pub:1")
    h.tg.fail = None
    assert h.db.requeue_failed_deliveries(1) == [STUDENTS[0]]


async def test_resend_is_for_admins_only(harness):
    h = harness
    await published_with_failures(h)
    text, _ = await command(h, STUDENT_ID, "/resend 1")
    assert "administrators only" in text and "/resend" not in text
    assert h.db.delivery_counts(1)["failed"] == 1


# ── grading retried by the bot ───────────────────────────────────────────
async def failed_during_outage(h, students=(STUDENT_ID,)):
    """Students submit while the AI provider is down: their grading fails."""
    await draft_with_students(h, list(students))
    await h.press(ADMIN_ID, "pub:1")
    h.ai.error = DOWN
    for user_id in students:
        await h.text(user_id, "1. B")
        await h.press(user_id, "ans:submit:1")
    assert all(s["status"] == "failed" for s in h.db.latest_submissions(1))


async def test_failed_grading_is_retried_once_the_ai_answers_again(make_harness):
    h = await make_harness(allow_chat=True)
    await failed_during_outage(h)
    assert "as soon as grading is retried" in h.tg.texts(STUDENT_ID)[-1]
    assert alerts.AUTO_RETRY_NOTE in admin_notices(h, "⚠️ Grading failed")[0]
    time_passes(h)

    calls = len(h.ai.calls)
    await work.run_once(h.app.bot, svc(h))  # still down (no request has succeeded): no retry
    assert len(h.ai.calls) == calls and results_to(h, STUDENT_ID) == []
    text, _ = await command(h, ADMIN_ID, "/status")
    assert "🔁 1 of them retried automatically" in text

    h.ai.error = None
    await h.text(STUDENTS[0], "hello")  # any request that gets a reply shows the provider is back
    await work.run_once(h.app.bot, svc(h))
    submission = h.db.latest_submissions(1)[0]
    assert submission["status"] == "graded" and submission["auto_regrades"] == 1
    assert len(results_to(h, STUDENT_ID)) == 1
    (notice,) = admin_notices(h, "🔁 Grading that had failed was retried automatically")
    assert "1 submission graded (#1: 1)" in notice and "No action needed" in notice

    await work.run_once(h.app.bot, svc(h))  # nothing more to do or say
    assert len(results_to(h, STUDENT_ID)) == 1
    assert len(admin_notices(h, "🔁 Grading that had failed")) == 1


async def test_not_retried_right_after_the_failure(harness):
    h = harness
    await failed_during_outage(h)
    h.ai.error = None
    h.db.record_ai_success()
    await work.run_once(h.app.bot, svc(h))
    assert h.db.latest_submissions(1)[0]["status"] == "failed"  # waits AUTO_REGRADE_DELAY first
    time_passes(h, "6 minutes")
    await work.run_once(h.app.bot, svc(h))
    assert h.db.latest_submissions(1)[0]["status"] == "graded"


async def test_retries_are_limited_and_only_the_last_failure_is_reported(harness):
    h = harness
    await failed_during_outage(h)
    h.ai.error = None
    h.ai.replies = ["not json"] * 100  # replies arrive (the provider is up) but cannot be used
    h.db.record_ai_success()
    failure_notices = len(admin_notices(h, "⚠️ Grading failed"))
    for retry in (1, 2, 3):
        time_passes(h, "1 day")  # longer than any backoff
        await work.run_once(h.app.bot, svc(h))
        assert h.db.latest_submissions(1)[0]["auto_regrades"] == retry
        if retry < 3:
            assert len(admin_notices(h, "⚠️ Grading failed")) == failure_notices  # already told
    time_passes(h, "1 day")
    await work.run_once(h.app.bot, svc(h))
    submission = h.db.latest_submissions(1)[0]
    assert submission["status"] == "failed" and submission["auto_regrades"] == 3  # waits for /regrade
    last = admin_notices(h, "⚠️ Grading failed")
    assert len(last) == failure_notices + 1 and alerts.AUTO_RETRY_NOTE not in last[-1]
    assert "<code>/regrade 1</code>" in last[-1]
    assert results_to(h, STUDENT_ID) == []
    assert h.tg.texts(STUDENT_ID).count(work.GRADING_DELAYED) == 1  # the student was told once


async def test_two_processes_retry_each_submission_once(make_harness):
    a = await make_harness()
    b = await make_harness()
    await failed_during_outage(a, students=STUDENTS)
    a.ai.error = None
    a.db.record_ai_success()
    time_passes(a)
    await asyncio.gather(work.run_once(a.app.bot, svc(a)), work.run_once(b.app.bot, svc(b)))
    for user_id in STUDENTS:
        assert len(results_to(a, user_id)) + len(results_to(b, user_id)) == 1, user_id
    assert all(s["auto_regrades"] == 1 for s in a.db.latest_submissions(1))
    notices = admin_notices(a, "🔁 Grading that had failed") + admin_notices(b, "🔁 Grading that had failed")
    assert len(notices) == 1  # the first success is reported at once; the rest batch for later


async def test_a_retry_does_not_take_over_a_manual_regrade(harness):
    h = harness
    await failed_during_outage(h)
    h.ai.error = None
    h.db.record_ai_success()
    time_passes(h)
    sid = h.db.latest_submissions(1)[0]["id"]
    assert h.db.claim_for_regrade(sid, 600)  # an admin's /regrade is grading it right now
    assert h.db.claim_auto_regrade(600, 3, 300) is None


async def test_automatic_regrading_can_be_switched_off(make_harness):
    h = await make_harness(auto_regrade_attempts=0)
    await failed_during_outage(h)
    assert alerts.AUTO_RETRY_NOTE not in admin_notices(h, "⚠️ Grading failed")[0]
    h.ai.error = None
    h.db.record_ai_success()
    time_passes(h)
    await work.run_once(h.app.bot, svc(h))
    assert h.db.latest_submissions(1)[0]["status"] == "failed"


async def test_the_back_message_says_the_bot_retries(make_harness):
    h = await make_harness(allow_chat=True)
    await h.text(ADMIN_ID, "/start")
    await failed_during_outage(h)
    for _ in range(alerts.AI_DOWN_AFTER):
        await h.text(STUDENTS[0], "hello")  # the admins are told it is down
    h.ai.error = None
    await h.text(STUDENTS[0], "hello")
    (back,) = admin_notices(h, "✅ The AI provider is answering again")
    assert "grades them again by itself" in back and "<code>/regrade 1</code>" in back


async def test_successful_retries_are_reported_together_after_the_first(make_harness):
    h = await make_harness(allow_chat=True)
    first, second = STUDENTS[:2]
    await failed_during_outage(h, students=(first,))
    h.ai.error = None
    h.db.record_ai_success()
    time_passes(h)
    await work.run_once(h.app.bot, svc(h))
    assert len(admin_notices(h, "🔁 Grading that had failed")) == 1  # the first, at once

    # A second failure, retried a few minutes later: graded and sent, but reported later.
    h.ai.error = DOWN
    await h.text(second, "/start")
    await h.press(second, "show:1")
    await h.text(second, "1. B")
    await h.press(second, "ans:submit:1")
    h.ai.error = None
    h.db.record_ai_success()
    h.db._run("UPDATE submissions SET failed_at = now() - interval '10 minutes' WHERE status = 'failed'")
    await work.run_once(h.app.bot, svc(h))
    assert len(results_to(h, second)) == 1
    assert len(admin_notices(h, "🔁 Grading that had failed")) == 1
    time_passes(h, f"{alerts.GRADING_NOTICE_INTERVAL} seconds")
    await work.run_once(h.app.bot, svc(h))
    notices = admin_notices(h, "🔁 Grading that had failed")
    assert len(notices) == 2 and "1 submission graded (#1: 1)" in notices[1]


def test_concurrent_automatic_retries_claim_each_submission_once(pg_url):
    """Real concurrency (threads, one connection each), as between bot processes."""
    db = Database(pg_url)
    try:
        aid = db.create_assessment("t", None, "x", None, ADMIN_ID,
                                   [{"type": "short", "question": "q", "answer": "a", "points": 1}])
        for user_id in range(30):
            db.mark_submission_failed(db.create_submission(aid, user_id, "a"), "down")
        db._run("UPDATE submissions SET failed_at = now() - interval '1 hour'")
        dbs = [Database(pg_url, max_size=2) for _ in range(6)]
        barrier = threading.Barrier(len(dbs))
        claimed = []

        def claim(other):
            barrier.wait()
            while (row := other.claim_auto_regrade(600, 3, 300)) is not None:
                claimed.append(row["id"])

        threads = [threading.Thread(target=claim, args=(other,)) for other in dbs]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(claimed) == list(range(1, 31))  # every submission, none twice
        for other in dbs:
            other.close()
    finally:
        db.close()
