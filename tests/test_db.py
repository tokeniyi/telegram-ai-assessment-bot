"""Direct tests of the PostgreSQL storage layer."""

import threading

import pytest

from bot.db import Database

QUESTIONS = [
    {"type": "mcq", "question": "Q1?", "options": ["A) x", "B) y"], "answer": "B", "points": 1},
    {"type": "short", "question": "Q2?", "answer": "because", "points": 2.5},
]
BIG_TELEGRAM_ID = 9_876_543_210  # does not fit in 32 bits


@pytest.fixture
def db(pg_url):
    database = Database(pg_url)
    yield database
    database.close()


def make_assessment(db, status=None, user=BIG_TELEGRAM_ID):
    aid = db.create_assessment("Title", "doc.pdf", "source text", "3 questions", user, QUESTIONS)
    if status:
        db.set_status(aid, status)
    return aid


def test_schema_creation_is_idempotent(pg_url):
    Database(pg_url).close()
    Database(pg_url).close()


def test_users_upsert_activate_and_count(db):
    assert db.count_users() == (0, 0)
    db.upsert_user(BIG_TELEGRAM_ID, "ann", "Ann A")
    db.upsert_user(2, None, None)
    assert db.count_users() == (2, 2)
    db.set_user_active(2, False)
    assert db.active_user_ids() == [BIG_TELEGRAM_ID] and db.count_users() == (2, 1)
    db.upsert_user(2, "bob", "Bob B")  # returning user is reactivated and updated
    assert db.count_users() == (2, 2)
    db.upsert_user(BIG_TELEGRAM_ID, "ann2", "Ann")
    assert db.count_users() == (2, 2)


def test_assessment_roundtrip_with_questions(db):
    aid = make_assessment(db)
    a = db.get_assessment(aid)
    assert a["title"] == "Title" and a["status"] == "draft" and a["created_by"] == BIG_TELEGRAM_ID
    assert a["published_at"] is None and a["source_text"] == "source text"
    qs = db.get_questions(aid)
    assert [q["position"] for q in qs] == [1, 2]
    assert qs[0]["options"] == ["A) x", "B) y"] and qs[1]["options"] == []
    assert qs[1]["points"] == 2.5 and qs[0]["qtype"] == "mcq"
    assert db.get_assessment(999) is None


def test_ids_increment(db):
    assert [make_assessment(db), make_assessment(db)] == [1, 2]


def test_create_assessment_is_atomic(db):
    with pytest.raises(Exception):
        db.create_assessment("T", None, "s", None, 1, [{"type": "mcq"}])  # malformed question
    assert db.list_assessments() == []


def test_replace_questions_and_title(db):
    aid = make_assessment(db)
    db.replace_questions(aid, "New", QUESTIONS[:1])
    assert db.get_assessment(aid)["title"] == "New" and len(db.get_questions(aid)) == 1


def test_status_changes_and_published_at(db):
    aid = make_assessment(db)
    db.set_status(aid, "published")
    a = db.get_assessment(aid)
    assert a["status"] == "published" and a["published_at"]
    db.set_status(aid, "closed")
    assert db.get_assessment(aid)["status"] == "closed"


def test_delete_cascades_to_questions_and_submissions(db):
    aid = make_assessment(db)
    db.create_submission(aid, 5, "answers")
    db.delete_assessment(aid)
    assert db.get_assessment(aid) is None and db.get_questions(aid) == []
    assert db.get_submission(1) is None


def test_list_assessments_counts(db):
    a1, a2 = make_assessment(db), make_assessment(db)
    db.create_submission(a2, 5, "x")
    db.create_submission(a2, 5, "y")
    db.create_submission(a2, 6, "z")
    rows = db.list_assessments()
    assert [r["id"] for r in rows] == [a2, a1]
    assert rows[0]["num_questions"] == 2 and rows[0]["num_submitters"] == 2
    assert rows[1]["num_submitters"] == 0
    assert len(db.list_assessments(limit=1)) == 1


def test_open_assessments_for_user(db):
    draft = make_assessment(db)
    first = make_assessment(db, "published")
    second = make_assessment(db, "published")
    closed = make_assessment(db, "closed")
    ids = lambda rows: [r["id"] for r in rows]
    assert ids(db.open_assessments_for(7, default_resubmission=False)) == [second, first]
    db.create_submission(first, 7, "mine")
    assert ids(db.open_assessments_for(7, default_resubmission=False)) == [second]
    assert ids(db.open_assessments_for(7, default_resubmission=True)) == [second, first]
    assert ids(db.open_assessments_for(8, default_resubmission=False)) == [second, first]
    assert draft not in ids(db.open_assessments_for(7, True)) and closed not in ids(db.open_assessments_for(7, True))


def test_submission_lifecycle(db):
    aid = make_assessment(db)
    sid = db.create_submission(aid, BIG_TELEGRAM_ID, "1. B")
    s = db.get_submission(sid)
    assert s["status"] == "pending" and s["score"] is None and s["user_id"] == BIG_TELEGRAM_ID
    assert [x["id"] for x in db.ungraded_submissions(aid)] == [sid]
    db.mark_submission_failed(sid)
    assert db.get_submission(sid)["status"] == "failed"
    db.save_grade(sid, 3.5, 4.0, "nice", [{"number": 1, "awarded": 1}])
    s = db.get_submission(sid)
    assert s["status"] == "graded" and s["score"] == 3.5 and s["max_score"] == 4.0
    assert s["feedback"] == "nice" and '"awarded": 1' in s["details"] and s["graded_at"]
    assert db.ungraded_submissions(aid) == []


def test_latest_submissions_one_per_user_best_first_nulls_last(db):
    aid = make_assessment(db)
    db.upsert_user(1, "one", "One")
    old = db.create_submission(aid, 1, "old")
    db.save_grade(old, 9, 10, "f", [])
    new = db.create_submission(aid, 1, "new")
    db.save_grade(new, 2, 10, "f", [])
    pending = db.create_submission(aid, 2, "pending")  # no score yet, user has no profile
    top = db.create_submission(aid, 3, "top")
    db.save_grade(top, 8, 10, "f", [])
    rows = db.latest_submissions(aid)
    assert [r["id"] for r in rows] == [top, new, pending]
    assert rows[1]["username"] == "one" and rows[2]["username"] is None


def test_user_submissions_newest_first_with_title(db):
    a1, a2 = make_assessment(db), make_assessment(db)
    s1 = db.create_submission(a1, 5, "a")
    s2 = db.create_submission(a2, 5, "b")
    db.create_submission(a1, 6, "other user")
    rows = db.user_submissions(5)
    assert [r["id"] for r in rows] == [s2, s1] and rows[0]["title"] == "Title"
    assert len(db.user_submissions(5, limit=1)) == 1


def test_concurrent_writers_from_threads(db):
    aid = make_assessment(db)
    errors = []

    def work(n):
        try:
            for i in range(10):
                db.create_submission(aid, n, f"{n}-{i}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(db.ungraded_submissions(aid)) == 60
    assert len({s["id"] for s in db.ungraded_submissions(aid)}) == 60
