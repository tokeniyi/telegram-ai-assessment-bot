"""Every Telegram update is handled at most once, even when Telegram delivers it again: to the
same process (it was not confirmed before a crash) or to a second one (two bot versions polling
during a deploy)."""

import asyncio
import threading

import pytest

from bot.db import Database
from bot.handlers import dedupe
from tests.conftest import ADMIN_ID, STUDENT_ID
from tests.test_flow import publish


async def test_a_resent_answer_is_saved_once(harness):
    h = harness
    await publish(h)
    await h.text(STUDENT_ID, "1. B")
    replies = len(h.tg.texts(STUDENT_ID))

    await h.deliver(h.last_update)  # Telegram sends the same update again
    assert len(h.tg.texts(STUDENT_ID)) == replies  # no second reply
    await h.text(STUDENT_ID, "2) water moving")
    assert "2 messages so far" in h.tg.texts(STUDENT_ID)[-1]
    assert h.db.draft_parts(STUDENT_ID, 1) == ["1. B", "2) water moving"]


async def test_a_resent_publish_button_sends_the_assessment_once(harness):
    h = harness
    await h.text(ADMIN_ID, "/start")
    await h.text(STUDENT_ID, "/start")
    await h.document(ADMIN_ID, "notes.txt", b"Osmosis is the movement of water. " * 10)
    await h.press(ADMIN_ID, "pub:1")
    received = len(h.tg.texts(STUDENT_ID))

    await h.deliver(h.last_update)
    assert len(h.tg.texts(STUDENT_ID)) == received
    assert sum("sent to 2 users" in t for t in h.tg.texts(ADMIN_ID)) == 1


async def test_a_resent_upload_is_not_turned_into_a_second_draft(harness):
    h = harness
    await h.text(ADMIN_ID, "/start")
    await h.document(ADMIN_ID, "notes.txt", b"Osmosis is the movement of water. " * 10)
    calls = len(h.ai.calls)

    await h.deliver(h.last_update)
    assert len(h.ai.calls) == calls  # no second AI request
    assert len(h.db.list_assessments()) == 1


async def test_two_processes_receiving_the_same_update_handle_it_once(make_harness):
    old = await make_harness()
    await publish(old)
    new = await make_harness()  # the new version, polling while the old one still runs

    await old.text(STUDENT_ID, "1. B")
    await new.deliver(old.last_update)
    assert new.tg.texts(STUDENT_ID) == []
    assert old.db.draft_parts(STUDENT_ID, 1) == ["1. B"]


async def test_two_processes_racing_for_one_update(make_harness):
    a = await make_harness()
    await publish(a)
    b = await make_harness()
    before = len(a.tg.texts(STUDENT_ID))
    update = {"update_id": 900, "message": a._message(STUDENT_ID, text="1. B")}

    await asyncio.gather(a.deliver(update), b.deliver(update))
    assert a.db.draft_parts(STUDENT_ID, 1) == ["1. B"]
    assert (len(a.tg.texts(STUDENT_ID)) - before) + len(b.tg.texts(STUDENT_ID)) == 1  # one reply in all


async def test_new_updates_after_a_restart_are_handled(make_harness):
    h = await make_harness()
    await publish(h)
    await h.text(STUDENT_ID, "1. B")
    h2 = await make_harness()
    await h2.text(STUDENT_ID, "2) water moving")  # a new update id continues the sequence
    assert h2.db.draft_parts(STUDENT_ID, 1) == ["1. B", "2) water moving"]


async def test_updates_are_still_handled_when_they_cannot_be_recorded(harness, monkeypatch):
    h = harness
    await h.text(STUDENT_ID, "/start")

    def broken(*args):
        raise OSError("database unreachable")

    monkeypatch.setattr(h.db, "claim_update", broken)
    await h.text(STUDENT_ID, "/myid")
    assert "Your Telegram ID" in h.tg.texts(STUDENT_ID)[-1]


async def test_old_records_are_pruned_every_so_often(harness, monkeypatch):
    h = harness
    pruned = []
    monkeypatch.setattr(dedupe, "PRUNE_EVERY", 3)
    monkeypatch.setattr(h.db, "prune_processed_updates", lambda hours: pruned.append(hours) or 0)
    for _ in range(7):
        await h.text(STUDENT_ID, "/myid")
    assert pruned == [dedupe.RETENTION_HOURS] * 2


# ── the database side ────────────────────────────────────────────────────
@pytest.fixture
def db(pg_url):
    database = Database(pg_url)
    yield database
    database.close()


def test_an_update_can_be_claimed_once_per_bot(db):
    assert db.claim_update(1, 10) is True
    assert db.claim_update(1, 10) is False
    assert db.claim_update(1, 11) is True
    assert db.claim_update(2, 10) is True  # another bot (a new token) has its own numbering


def test_concurrent_claims_have_one_winner(db):
    results, barrier = [], threading.Barrier(8)

    def claim():
        barrier.wait()
        results.append(db.claim_update(1, 42))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False] * 7 + [True]


def test_pruning_forgets_only_old_records(db):
    db.claim_update(1, 1)
    db.claim_update(1, 2)
    db._run("UPDATE processed_updates SET received_at = now() - interval '3 days' WHERE update_id = 1")
    assert db.prune_processed_updates(48) == 1
    assert db.claim_update(1, 1) is True   # forgotten
    assert db.claim_update(1, 2) is False  # still remembered


def test_schema_with_update_records_can_be_created_twice(pg_url):
    Database(pg_url).close()
    Database(pg_url).close()
