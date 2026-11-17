"""PostgreSQL storage: users, assessments, questions, graded submissions, and each user's
unfinished work (the answers typed so far, the chosen assessment and recent chat), so that a
restart or redeploy does not lose it."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    BIGINT PRIMARY KEY,
    username   TEXT,
    full_name  TEXT,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    joined_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assessments (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title           TEXT NOT NULL,
    source_name     TEXT,
    source_text     TEXT NOT NULL,
    instructions    TEXT,
    status          TEXT NOT NULL DEFAULT 'draft',   -- draft | published | closed
    created_by      BIGINT NOT NULL,
    created_at      TEXT NOT NULL,
    published_at    TEXT
);
CREATE TABLE IF NOT EXISTS questions (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    assessment_id  BIGINT NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    position       INTEGER NOT NULL,
    qtype          TEXT NOT NULL,                    -- mcq | short
    text           TEXT NOT NULL,
    options        TEXT NOT NULL DEFAULT '[]',       -- JSON list, for mcq
    answer         TEXT NOT NULL,
    points         DOUBLE PRECISION NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS submissions (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    assessment_id  BIGINT NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    user_id        BIGINT NOT NULL,
    raw_text       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending | graded | failed
    score          DOUBLE PRECISION,
    max_score      DOUBLE PRECISION,
    feedback       TEXT,
    details        TEXT,                             -- JSON: per-question results
    created_at     TEXT NOT NULL,
    graded_at      TEXT
);
-- One row per user who has talked to the bot. Writes that change a user's unfinished work
-- lock this row first, which serialises them across every bot process sharing the database.
CREATE TABLE IF NOT EXISTS user_sessions (
    user_id               BIGINT PRIMARY KEY,
    target_assessment_id  BIGINT REFERENCES assessments(id) ON DELETE SET NULL,  -- picked with /pending
    chat_history          TEXT NOT NULL DEFAULT '[]',   -- JSON list of [role, text], oldest first
    updated_at            TEXT NOT NULL
);
-- The answer messages a user has saved but not submitted. A user has at most one draft at a
-- time: every row of a user belongs to the same assessment.
CREATE TABLE IF NOT EXISTS draft_answers (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        BIGINT NOT NULL,
    assessment_id  BIGINT NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    text           TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_questions_assessment ON questions(assessment_id);
CREATE INDEX IF NOT EXISTS idx_submissions_lookup ON submissions(assessment_id, user_id);
CREATE INDEX IF NOT EXISTS idx_draft_answers_user ON draft_answers(user_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thin wrapper over a psycopg connection pool.

    `dsn` is a libpq URL such as postgresql://user:pass@host:5432/dbname.
    """

    def __init__(self, dsn: str, max_size: int = 10):
        self._pool = ConnectionPool(
            dsn, min_size=1, max_size=max_size, kwargs={"row_factory": dict_row}, open=True
        )
        self._pool.wait()
        with self._pool.connection() as conn:
            conn.execute(SCHEMA)

    def close(self) -> None:
        self._pool.close()

    def _all(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            return conn.execute(query, params).fetchall()

    def _one(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(query, params)
        return rows[0] if rows else None

    def _run(self, query: str, params: tuple = ()) -> None:
        with self._pool.connection() as conn:
            conn.execute(query, params)

# ── users ────────────────────────────────────────────────────────────
    def upsert_user(self, user_id: int, username: str | None, full_name: str | None) -> None:
        self._run(
            """INSERT INTO users (user_id, username, full_name, is_active, joined_at)
               VALUES (%s, %s, %s, TRUE, %s)
               ON CONFLICT (user_id) DO UPDATE SET
                   username = EXCLUDED.username, full_name = EXCLUDED.full_name, is_active = TRUE""",
            (user_id, username, full_name, _now()),
        )

    def set_user_active(self, user_id: int, active: bool) -> None:
        self._run("UPDATE users SET is_active = %s WHERE user_id = %s", (bool(active), user_id))

    def active_user_ids(self) -> list[int]:
        return [r["user_id"] for r in self._all("SELECT user_id FROM users WHERE is_active")]

    def count_users(self) -> tuple[int, int]:
        row = self._one("SELECT COUNT(*) AS total, COALESCE(SUM(is_active::int), 0) AS active FROM users")
        return int(row["total"]), int(row["active"])

    # ── assessments ──────────────────────────────────────────────────────
    def create_assessment(
        self,
        title: str,
        source_name: str | None,
        source_text: str,
        instructions: str | None,
        created_by: int,
        questions: list[dict[str, Any]],
    ) -> int:
        with self._pool.connection() as conn:  # one transaction: the assessment and its questions
            assessment_id = conn.execute(
                """INSERT INTO assessments (title, source_name, source_text, instructions, created_by, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (title, source_name, source_text, instructions, created_by, _now()),
            ).fetchone()["id"]
            self._insert_questions(conn, assessment_id, questions)
        return assessment_id

    def replace_questions(self, assessment_id: int, title: str, questions: list[dict[str, Any]]) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM questions WHERE assessment_id = %s", (assessment_id,))
            conn.execute("UPDATE assessments SET title = %s WHERE id = %s", (title, assessment_id))
            self._insert_questions(conn, assessment_id, questions)

    @staticmethod
    def _insert_questions(conn, assessment_id: int, questions: list[dict[str, Any]]) -> None:
        conn.cursor().executemany(
            """INSERT INTO questions (assessment_id, position, qtype, text, options, answer, points)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            [
                (assessment_id, i, q["type"], q["question"], json.dumps(q.get("options", [])),
                 q["answer"], q["points"])
                for i, q in enumerate(questions, start=1)
            ],
        )

    def get_assessment(self, assessment_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM assessments WHERE id = %s", (assessment_id,))

    def get_questions(self, assessment_id: int) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM questions WHERE assessment_id = %s ORDER BY position", (assessment_id,)
        )
        for row in rows:
            row["options"] = json.loads(row["options"])
        return rows

    def set_status(self, assessment_id: int, status: str) -> None:
        if status == "published":
            self._run(
                "UPDATE assessments SET status = %s, published_at = %s WHERE id = %s",
                (status, _now(), assessment_id),
            )
        else:
            self._run("UPDATE assessments SET status = %s WHERE id = %s", (status, assessment_id))

    def delete_assessment(self, assessment_id: int) -> None:
        self._run("DELETE FROM assessments WHERE id = %s", (assessment_id,))

    def list_assessments(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._all(
            """SELECT a.id, a.title, a.status, a.created_at,
                      (SELECT COUNT(*) FROM questions q WHERE q.assessment_id = a.id) AS num_questions,
                      (SELECT COUNT(DISTINCT s.user_id) FROM submissions s WHERE s.assessment_id = a.id)
                          AS num_submitters
               FROM assessments a ORDER BY a.id DESC LIMIT %s""",
            (limit,),
        )

    def open_assessments_for(self, user_id: int, include_submitted: bool) -> list[dict[str, Any]]:
        """Published assessments the user can still answer, newest first."""
        sql = "SELECT a.* FROM assessments a WHERE a.status = 'published'"
        params: tuple = ()
        if not include_submitted:
            sql += " AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.assessment_id = a.id AND s.user_id = %s)"
            params = (user_id,)
        return self._all(sql + " ORDER BY a.published_at DESC, a.id DESC", params)

    # ── submissions ──────────────────────────────────────────────────────
    def create_submission(self, assessment_id: int, user_id: int, raw_text: str) -> int:
        row = self._one(
            """INSERT INTO submissions (assessment_id, user_id, raw_text, created_at)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (assessment_id, user_id, raw_text, _now()),
        )
        return row["id"]

    def save_grade(
        self, submission_id: int, score: float, max_score: float, feedback: str, details: list[dict]
    ) -> None:
        self._run(
            """UPDATE submissions SET status = 'graded', score = %s, max_score = %s, feedback = %s,
                      details = %s, graded_at = %s WHERE id = %s""",
            (score, max_score, feedback, json.dumps(details), _now(), submission_id),
        )

    def mark_submission_failed(self, submission_id: int) -> None:
        self._run("UPDATE submissions SET status = 'failed' WHERE id = %s", (submission_id,))

    def get_submission(self, submission_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM submissions WHERE id = %s", (submission_id,))

    def ungraded_submissions(self, assessment_id: int) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM submissions WHERE assessment_id = %s AND status != 'graded' ORDER BY id",
            (assessment_id,),
        )

    def latest_submissions(self, assessment_id: int) -> list[dict[str, Any]]:
        """Each user's most recent submission for an assessment, best score first."""
        return self._all(
            """SELECT s.*, u.username, u.full_name
               FROM submissions s LEFT JOIN users u ON u.user_id = s.user_id
               WHERE s.assessment_id = %s
                 AND s.id = (SELECT MAX(id) FROM submissions x
                             WHERE x.assessment_id = s.assessment_id AND x.user_id = s.user_id)
               ORDER BY s.score DESC NULLS LAST, s.id""",
            (assessment_id,),
        )

    def user_submissions(self, user_id: int, limit: int = 15) -> list[dict[str, Any]]:
        return self._all(
            """SELECT s.*, a.title FROM submissions s JOIN assessments a ON a.id = s.assessment_id
               WHERE s.user_id = %s ORDER BY s.id DESC LIMIT %s""",
            (user_id, limit),
        )

    # ── unfinished work: sessions and drafts ─────────────────────────────
    # Every method below that changes a user's draft runs in one transaction that first locks
    # that user's session row, so two updates for the same user (a double tap, a retried
    # update, or two bot processes during a deploy) are applied one after the other.
    @staticmethod
    def _lock_session(conn, user_id: int) -> None:
        conn.execute(
            """INSERT INTO user_sessions (user_id, updated_at) VALUES (%s, %s)
               ON CONFLICT (user_id) DO UPDATE SET updated_at = EXCLUDED.updated_at""",
            (user_id, _now()),
        )

    def get_target(self, user_id: int) -> int | None:
        row = self._one("SELECT target_assessment_id FROM user_sessions WHERE user_id = %s", (user_id,))
        return row["target_assessment_id"] if row else None

    def choose_assessment(self, user_id: int, assessment_id: int) -> int:
        """Make `assessment_id` the one the user is answering.

        A draft for a different assessment is discarded (a user has one draft at a time); a
        draft for this one is kept. Returns the number of answer messages still saved.
        """
        with self._pool.connection() as conn:
            self._lock_session(conn, user_id)
            conn.execute("UPDATE user_sessions SET target_assessment_id = %s WHERE user_id = %s",
                         (assessment_id, user_id))
            conn.execute("DELETE FROM draft_answers WHERE user_id = %s AND assessment_id != %s",
                         (user_id, assessment_id))
            return conn.execute("SELECT COUNT(*) AS n FROM draft_answers WHERE user_id = %s",
                                (user_id,)).fetchone()["n"]

    def draft_parts(self, user_id: int, assessment_id: int) -> list[str]:
        """The answer messages saved for this assessment, in the order they were sent."""
        rows = self._all(
            "SELECT text FROM draft_answers WHERE user_id = %s AND assessment_id = %s ORDER BY id",
            (user_id, assessment_id),
        )
        return [r["text"] for r in rows]

    def add_draft_part(self, user_id: int, assessment_id: int, text: str, max_chars: int) -> int | None:
        """Save one more answer message. A draft for another assessment is replaced.

        Returns how many messages the draft now has, or None (nothing saved) if the draft would
        grow beyond `max_chars`.
        """
        with self._pool.connection() as conn:
            self._lock_session(conn, user_id)
            conn.execute("DELETE FROM draft_answers WHERE user_id = %s AND assessment_id != %s",
                         (user_id, assessment_id))
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(text)), 0) AS chars "
                "FROM draft_answers WHERE user_id = %s", (user_id,),
            ).fetchone()
            if row["chars"] + len(text) > max_chars:
                return None
            conn.execute(
                "INSERT INTO draft_answers (user_id, assessment_id, text, created_at) VALUES (%s, %s, %s, %s)",
                (user_id, assessment_id, text, _now()),
            )
            return row["n"] + 1

    def undo_draft_part(self, user_id: int, assessment_id: int) -> int | None:
        """Remove the last saved message. Returns how many are left, or None if there was none."""
        with self._pool.connection() as conn:
            self._lock_session(conn, user_id)
            removed = conn.execute(
                """DELETE FROM draft_answers WHERE id = (
                       SELECT MAX(id) FROM draft_answers WHERE user_id = %s AND assessment_id = %s)
                   RETURNING id""",
                (user_id, assessment_id),
            ).fetchone()
            if removed is None:
                return None
            return conn.execute("SELECT COUNT(*) AS n FROM draft_answers WHERE user_id = %s",
                                (user_id,)).fetchone()["n"]

    def discard_draft(self, user_id: int) -> bool:
        """Delete the user's draft. Returns whether there was one."""
        with self._pool.connection() as conn:
            self._lock_session(conn, user_id)
            return conn.execute("DELETE FROM draft_answers WHERE user_id = %s", (user_id,)).rowcount > 0

    def submit_draft(self, user_id: int, assessment_id: int, allow_resubmission: bool) -> tuple[str, int | None]:
        """Turn the user's draft for `assessment_id` into a submission, exactly once.

        Returns ("submitted", submission_id), ("empty", None) when there is no draft for it
        (already submitted, discarded, or never written), or ("closed", None) when the assessment
        no longer accepts this user's answers; the draft is then discarded. Checking, inserting
        the submission and deleting the draft happen in one transaction under the user's lock,
        so repeated or concurrent submits create at most one submission.
        """
        with self._pool.connection() as conn:
            self._lock_session(conn, user_id)
            # Claiming the draft by deleting it is what makes this exactly-once even without the
            # lock: a competing transaction blocks on these rows and then finds them gone.
            claimed = conn.execute(
                "DELETE FROM draft_answers WHERE user_id = %s AND assessment_id = %s RETURNING id, text",
                (user_id, assessment_id),
            ).fetchall()
            if not claimed:
                return "empty", None
            parts = [r["text"] for r in sorted(claimed, key=lambda r: r["id"])]
            conn.execute("UPDATE user_sessions SET target_assessment_id = NULL WHERE user_id = %s", (user_id,))
            assessment = conn.execute("SELECT status FROM assessments WHERE id = %s FOR SHARE",
                                      (assessment_id,)).fetchone()
            already = not allow_resubmission and conn.execute(
                "SELECT 1 FROM submissions WHERE assessment_id = %s AND user_id = %s LIMIT 1",
                (assessment_id, user_id),
            ).fetchone() is not None
            if assessment is None or assessment["status"] != "published" or already:
                return "closed", None
            submission_id = conn.execute(
                """INSERT INTO submissions (assessment_id, user_id, raw_text, created_at)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (assessment_id, user_id, "\n".join(parts), _now()),
            ).fetchone()["id"]
            return "submitted", submission_id

    def chat_history(self, user_id: int) -> list[tuple[str, str]]:
        row = self._one("SELECT chat_history FROM user_sessions WHERE user_id = %s", (user_id,))
        return [tuple(item) for item in json.loads(row["chat_history"])] if row else []

    def append_chat_history(self, user_id: int, turns: list[tuple[str, str]], keep: int) -> None:
        """Add messages to the user's chat history, keeping only the newest `keep`."""
        with self._pool.connection() as conn:
            self._lock_session(conn, user_id)
            row = conn.execute("SELECT chat_history FROM user_sessions WHERE user_id = %s",
                               (user_id,)).fetchone()
            history = json.loads(row["chat_history"]) + [list(t) for t in turns]
            history = history[max(len(history) - keep, 0):] if keep > 0 else []
            conn.execute("UPDATE user_sessions SET chat_history = %s WHERE user_id = %s",
                         (json.dumps(history), user_id))
