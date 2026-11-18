"""Unfinished work survives a restart: drafts, the chosen assessment and chat history live in
PostgreSQL, and a draft becomes at most one submission however often Submit arrives.

A "restart" here builds a second application on the same database, as a redeploy does: the new
process starts with empty memory (no context.user_data) and must carry on from the database.
"""

import asyncio
import threading
import time

import psycopg

import pytest

from bot.db import Database
from tests.conftest import ADMIN_ID, STUDENT_ID
from tests.test_db import QUESTIONS
from tests.test_flow import MATERIAL, publish


async def restart(make_harness, **overrides):
    """A fresh bot process on the same database. The old one receives no more updates."""
    return await make_harness(**overrides)


def edits(h, chat_id=STUDENT_ID):
    return h.tg.texts(chat_id, "editMessageText")


# ── restart in the middle of an assessment ───────────────────────────────
async def test_answers_survive_a_restart_and_can_be_submitted(make_harness):
    h = await make_harness()
    await publish(h)
    await h.text(STUDENT_ID, "1. B")
    await h.text(STUDENT_ID, "2) water moving")

    h2 = await restart(make_harness)
    await h2.text(STUDENT_ID, "3. one more line")
    assert "3 messages so far" in h2.tg.texts(STUDENT_ID)[-1]
    await h2.press(STUDENT_ID, "ans:submit:1")

    submission = h2.db.get_submission(1)
    assert submission["raw_text"] == "1. B\n2) water moving\n3. one more line"
    assert submission["status"] == "graded" and "Result" in h2.tg.texts(STUDENT_ID)[-1]


async def test_submit_button_from_before_the_restart_still_works(make_harness):
    h = await make_harness()
    await publish(h)
    await h.text(STUDENT_ID, "1. B")
    assert h.tg.last_keyboard(STUDENT_ID) == ["ans:submit:1", "ans:undo:1", "ans:clear:0"]

    h2 = await restart(make_harness)
    await h2.press(STUDENT_ID, "ans:submit:1")  # the button sent by the old process
    assert h2.db.get_submission(1)["raw_text"] == "1. B"


async def test_undo_after_a_restart(make_harness):
    h = await make_harness()
    await publish(h)
    await h.text(STUDENT_ID, "1. B")
    await h.text(STUDENT_ID, "2) not really an answer")

    h2 = await restart(make_harness)
    await h2.press(STUDENT_ID, "ans:undo:1")
    assert "1 left" in edits(h2)[-1]
    await h2.press(STUDENT_ID, "ans:submit:1")
    assert h2.db.get_submission(1)["raw_text"] == "1. B"


async def test_cancel_and_discard_after_a_restart(make_harness):
    h = await make_harness()
    await publish(h)
    await h.text(STUDENT_ID, "1. B")

    h2 = await restart(make_harness)
    await h2.text(STUDENT_ID, "/cancel")
    assert h2.tg.texts(STUDENT_ID)[-1] == "🗑 Your unsent answers have been discarded."
    await h2.text(STUDENT_ID, "/cancel")
    assert h2.tg.texts(STUDENT_ID)[-1] == "There are no unsent answers to discard."

    await h2.text(STUDENT_ID, "1. A")
    h3 = await restart(make_harness)
    await h3.press(STUDENT_ID, "ans:clear:0")
    assert edits(h3)[-1] == "🗑 Your unsent answers have been discarded."
    await h3.press(STUDENT_ID, "ans:submit:1")
    assert h3.db.get_submission(1) is None


async def test_resuming_through_pending_keeps_the_saved_answers(make_harness):
    h = await make_harness()
    await publish(h)
    await h.text(STUDENT_ID, "1. B")
    await h.text(STUDENT_ID, "2) water moving")

    h2 = await restart(make_harness)
    await h2.text(STUDENT_ID, "/pending")
    await h2.press(STUDENT_ID, "show:1")  # the same assessment: resume, do not wipe
    assert "already have 2 messages saved" in h2.tg.texts(STUDENT_ID)[-1]
    assert h2.tg.last_keyboard(STUDENT_ID) == ["ans:submit:1", "ans:undo:1", "ans:clear:0"]
    await h2.press(STUDENT_ID, "ans:submit:1")
    assert h2.db.get_submission(1)["raw_text"] == "1. B\n2) water moving"


async def test_chosen_assessment_survives_a_restart(make_harness):
    h = await make_harness()
    await publish(h)                                     # assessment 1
    await h.document(ADMIN_ID, "notes.txt", MATERIAL)
    await h.press(ADMIN_ID, "pub:2")                     # assessment 2, now the newest
    await h.text(STUDENT_ID, "/pending")
    await h.press(STUDENT_ID, "show:1")                  # the student picks the older one

    h2 = await restart(make_harness)
    await h2.text(STUDENT_ID, "1. B")
    assert h2.tg.last_keyboard(STUDENT_ID) == ["ans:submit:1", "ans:undo:1", "ans:clear:0"]
    await h2.press(STUDENT_ID, "ans:submit:1")
    assert h2.db.get_submission(1)["assessment_id"] == 1


async def test_switching_assessments_discards_the_other_draft(make_harness):
    h = await make_harness()
    await publish(h)
    await h.document(ADMIN_ID, "notes.txt", MATERIAL)
    await h.press(ADMIN_ID, "pub:2")
    await h.text(STUDENT_ID, "1. B")                     # saved for the newest, assessment 2
    await h.text(STUDENT_ID, "/pending")
    await h.press(STUDENT_ID, "show:1")
    assert "already have" not in h.tg.texts(STUDENT_ID)[-1]

    h2 = await restart(make_harness)
    await h2.press(STUDENT_ID, "ans:submit:2")           # the old draft is gone
    assert h2.db.get_submission(1) is None
    await h2.text(STUDENT_ID, "1. A")
    await h2.press(STUDENT_ID, "ans:submit:1")
    assert h2.db.get_submission(1)["assessment_id"] == 1


async def test_chat_history_survives_a_restart(make_harness):
    h = await make_harness()
    await h.text(STUDENT_ID, "Hey")

    h2 = await restart(make_harness)
    await h2.text(STUDENT_ID, "what is ATP?")
    user = [c for c in h2.ai.calls if c[0].startswith("You are the chat assistant")][-1][1]
    assert "Student: Hey" in user and "Assistant: Chat reply to: Hey" in user


# ── submitting exactly once ──────────────────────────────────────────────
async def test_repeated_submit_creates_one_submission(make_harness):
    h = await make_harness()
    await publish(h)
    await h.text(STUDENT_ID, "1. B")
    await h.press(STUDENT_ID, "ans:submit:1")
    await h.press(STUDENT_ID, "ans:submit:1")  # a double tap, or Telegram redelivering the update
    h2 = await restart(make_harness)
    await h2.press(STUDENT_ID, "ans:submit:1")  # and again from the new process
    assert len(h2.db.user_submissions(STUDENT_ID)) == 1
    assert "already submitted" in h2.tg.texts(method="answerCallbackQuery")[-1]


async def test_submit_from_two_processes_at_once_creates_one_submission(make_harness):
    old = await make_harness()
    await publish(old)
    await old.text(STUDENT_ID, "1. B")
    new = await restart(make_harness)  # during a deploy both versions may briefly be running
    await asyncio.gather(old.press(STUDENT_ID, "ans:submit:1"), new.press(STUDENT_ID, "ans:submit:1"))
    assert len(new.db.user_submissions(STUDENT_ID)) == 1


# ── the storage layer directly ───────────────────────────────────────────
@pytest.fixture
def db(pg_url):
    database = Database(pg_url)
    yield database
    database.close()


def published(db):
    aid = db.create_assessment("T", None, "s", None, 1, QUESTIONS)
    db.set_status(aid, "published")
    return aid


def test_draft_parts_are_kept_in_order_and_per_user(db):
    aid = published(db)
    assert db.add_draft_part(7, aid, "one", 100) == 1
    assert db.add_draft_part(7, aid, "two", 100) == 2
    assert db.add_draft_part(8, aid, "other user", 100) == 1
    assert db.draft_parts(7, aid) == ["one", "two"] and db.draft_parts(8, aid) == ["other user"]


def test_a_user_has_one_draft_at_a_time(db):
    first, second = published(db), published(db)
    db.add_draft_part(7, first, "for the first", 100)
    assert db.add_draft_part(7, second, "for the second", 100) == 1
    assert db.draft_parts(7, first) == [] and db.draft_parts(7, second) == ["for the second"]


def test_draft_size_limit_saves_nothing(db):
    aid = published(db)
    db.add_draft_part(7, aid, "x" * 8, 10)
    assert db.add_draft_part(7, aid, "yyy", 10) is None
    assert db.draft_parts(7, aid) == ["x" * 8]


def test_undo_and_discard(db):
    aid = published(db)
    assert db.undo_draft_part(7, aid) is None
    db.add_draft_part(7, aid, "a", 100)
    db.add_draft_part(7, aid, "b", 100)
    assert db.undo_draft_part(7, aid) == 1 and db.draft_parts(7, aid) == ["a"]
    assert db.discard_draft(7) is True and db.discard_draft(7) is False


def test_choose_assessment_keeps_a_matching_draft(db):
    first, second = published(db), published(db)
    assert db.get_target(7) is None
    db.add_draft_part(7, first, "a", 100)
    assert db.choose_assessment(7, first) == 1 and db.get_target(7) == first
    assert db.choose_assessment(7, second) == 0 and db.draft_parts(7, first) == []


def test_submit_draft_outcomes(db):
    aid = published(db)
    assert db.submit_draft(7, aid, allow_resubmission=False) == ("empty", None)
    db.choose_assessment(7, aid)
    db.add_draft_part(7, aid, "1. B", 100)
    db.add_draft_part(7, aid, "2. because", 100)
    outcome, sid = db.submit_draft(7, aid, allow_resubmission=False)
    assert outcome == "submitted" and db.get_submission(sid)["raw_text"] == "1. B\n2. because"
    assert db.draft_parts(7, aid) == [] and db.get_target(7) is None
    assert db.submit_draft(7, aid, allow_resubmission=False) == ("empty", None)

    db.add_draft_part(7, aid, "again", 100)  # a second attempt is refused without resubmission...
    assert db.submit_draft(7, aid, allow_resubmission=False) == ("closed", None)
    assert db.draft_parts(7, aid) == []
    db.add_draft_part(7, aid, "again", 100)  # ...and allowed with it
    assert db.submit_draft(7, aid, allow_resubmission=True)[0] == "submitted"

    db.set_status(aid, "closed")
    db.add_draft_part(8, aid, "late", 100)
    assert db.submit_draft(8, aid, allow_resubmission=True) == ("closed", None)
    assert len(db.user_submissions(7)) == 2 and db.user_submissions(8) == []


def test_concurrent_submits_create_exactly_one_submission(db):
    aid = published(db)
    db.add_draft_part(7, aid, "1. B", 100)
    outcomes, barrier = [], threading.Barrier(8)

    def submit():
        barrier.wait()
        outcomes.append(db.submit_draft(7, aid, allow_resubmission=True)[0])

    threads = [threading.Thread(target=submit) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes) == ["empty"] * 7 + ["submitted"]
    assert len(db.user_submissions(7)) == 1


def test_submits_wait_for_the_users_lock_and_then_find_nothing_left(db, pg_url):
    """Deterministic version of the race: a submit is in flight (holding the user's lock) while
    two retries arrive. They must wait for it, then see the draft already claimed."""
    aid = published(db)
    db.add_draft_part(7, aid, "1. B", 100)
    with psycopg.connect(pg_url) as conn:  # the in-flight submit, paused inside its transaction
        Database._lock_session(conn, 7)
        conn.execute("DELETE FROM draft_answers WHERE user_id = 7")
        conn.execute("INSERT INTO submissions (assessment_id, user_id, raw_text, created_at) "
                     "VALUES (%s, 7, '1. B', 'now')", (aid,))
        outcomes = []
        retries = [threading.Thread(target=lambda: outcomes.append(db.submit_draft(7, aid, True)[0]))
                   for _ in range(2)]
        [t.start() for t in retries]
        time.sleep(0.3)
        assert outcomes == []  # blocked behind the in-flight submit
        conn.commit()
    [t.join() for t in retries]
    assert outcomes == ["empty", "empty"] and len(db.user_submissions(7)) == 1


def test_concurrent_appends_all_land(db):
    aid = published(db)

    def work(n):
        for i in range(10):
            db.add_draft_part(7, aid, f"{n}-{i}", 10_000)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(db.draft_parts(7, aid)) == 50


def test_deleting_an_assessment_removes_its_drafts_and_target(db):
    aid = published(db)
    db.choose_assessment(7, aid)
    db.add_draft_part(7, aid, "a", 100)
    db.delete_assessment(aid)
    assert db.draft_parts(7, aid) == [] and db.get_target(7) is None


def test_chat_history_is_trimmed_and_survives_reconnecting(db, pg_url):
    assert db.chat_history(7) == []
    db.append_chat_history(7, [("Student", "one"), ("Assistant", "r1")], keep=4)
    db.append_chat_history(7, [("Student", "two"), ("Assistant", "r2")], keep=4)
    db.append_chat_history(7, [("Student", "three"), ("Assistant", "r3")], keep=4)
    again = Database(pg_url)
    try:
        assert again.chat_history(7) == [("Student", "two"), ("Assistant", "r2"),
                                         ("Student", "three"), ("Assistant", "r3")]
    finally:
        again.close()
    db.append_chat_history(8, [("Student", "x"), ("Assistant", "y")], keep=0)
    assert db.chat_history(8) == []
