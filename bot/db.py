"""PostgreSQL storage: users, assessments, questions and graded submissions."""

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
CREATE INDEX IF NOT EXISTS idx_questions_assessment ON questions(assessment_id);
CREATE INDEX IF NOT EXISTS idx_submissions_lookup ON submissions(assessment_id, user_id);
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
