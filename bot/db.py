"""SQLite storage: users, assessments, questions and graded submissions."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    full_name  TEXT,
    is_active  INTEGER NOT NULL DEFAULT 1,
    joined_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assessments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    source_name     TEXT,
    source_text     TEXT NOT NULL,
    instructions    TEXT,
    status          TEXT NOT NULL DEFAULT 'draft',   -- draft | published | closed
    created_by      INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    published_at    TEXT
);
CREATE TABLE IF NOT EXISTS questions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id  INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    position       INTEGER NOT NULL,
    qtype          TEXT NOT NULL,                    -- mcq | short
    text           TEXT NOT NULL,
    options        TEXT NOT NULL DEFAULT '[]',       -- JSON list, for mcq
    answer         TEXT NOT NULL,
    points         REAL NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS submissions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id  INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    user_id        INTEGER NOT NULL,
    raw_text       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending | graded | failed
    score          REAL,
    max_score      REAL,
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
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    def _run(self, sql: str, params: tuple = ()) -> int:
        with self._lock, self._conn:
            return self._conn.execute(sql, params).lastrowid

    # ── users ────────────────────────────────────────────────────────────
    def upsert_user(self, user_id: int, username: str | None, full_name: str | None) -> None:
        self._run(
            """INSERT INTO users (user_id, username, full_name, is_active, joined_at)
               VALUES (?, ?, ?, 1, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username = excluded.username, full_name = excluded.full_name, is_active = 1""",
            (user_id, username, full_name, _now()),
        )

    def set_user_active(self, user_id: int, active: bool) -> None:
        self._run("UPDATE users SET is_active = ? WHERE user_id = ?", (int(active), user_id))

    def active_user_ids(self) -> list[int]:
        return [r["user_id"] for r in self._all("SELECT user_id FROM users WHERE is_active = 1")]

    def count_users(self) -> tuple[int, int]:
        row = self._one("SELECT COUNT(*) AS total, COALESCE(SUM(is_active), 0) AS active FROM users")
        return row["total"], row["active"]

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
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO assessments (title, source_name, source_text, instructions, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (title, source_name, source_text, instructions, created_by, _now()),
            )
            assessment_id = cur.lastrowid
            self._insert_questions(assessment_id, questions)
        return assessment_id

    def replace_questions(self, assessment_id: int, title: str, questions: list[dict[str, Any]]) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM questions WHERE assessment_id = ?", (assessment_id,))
            self._conn.execute("UPDATE assessments SET title = ? WHERE id = ?", (title, assessment_id))
            self._insert_questions(assessment_id, questions)

    def _insert_questions(self, assessment_id: int, questions: list[dict[str, Any]]) -> None:
        self._conn.executemany(
            """INSERT INTO questions (assessment_id, position, qtype, text, options, answer, points)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (assessment_id, i, q["type"], q["question"], json.dumps(q.get("options", [])),
                 q["answer"], q["points"])
                for i, q in enumerate(questions, start=1)
            ],
        )

    def get_assessment(self, assessment_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM assessments WHERE id = ?", (assessment_id,))

    def get_questions(self, assessment_id: int) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM questions WHERE assessment_id = ? ORDER BY position", (assessment_id,)
        )
        for row in rows:
            row["options"] = json.loads(row["options"])
        return rows

    def set_status(self, assessment_id: int, status: str) -> None:
        if status == "published":
            self._run(
                "UPDATE assessments SET status = ?, published_at = ? WHERE id = ?",
                (status, _now(), assessment_id),
            )
        else:
            self._run("UPDATE assessments SET status = ? WHERE id = ?", (status, assessment_id))

    def delete_assessment(self, assessment_id: int) -> None:
        self._run("DELETE FROM assessments WHERE id = ?", (assessment_id,))

    def list_assessments(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._all(
            """SELECT a.id, a.title, a.status, a.created_at,
                      (SELECT COUNT(*) FROM questions q WHERE q.assessment_id = a.id) AS num_questions,
                      (SELECT COUNT(DISTINCT s.user_id) FROM submissions s WHERE s.assessment_id = a.id)
                          AS num_submitters
               FROM assessments a ORDER BY a.id DESC LIMIT ?""",
            (limit,),
        )

    def open_assessments_for(self, user_id: int, include_submitted: bool) -> list[dict[str, Any]]:
        """Published assessments the user can still answer, newest first."""
        sql = "SELECT a.* FROM assessments a WHERE a.status = 'published'"
        params: tuple = ()
        if not include_submitted:
            sql += " AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.assessment_id = a.id AND s.user_id = ?)"
            params = (user_id,)
        return self._all(sql + " ORDER BY a.published_at DESC, a.id DESC", params)

    # ── submissions ──────────────────────────────────────────────────────
    def create_submission(self, assessment_id: int, user_id: int, raw_text: str) -> int:
        return self._run(
            "INSERT INTO submissions (assessment_id, user_id, raw_text, created_at) VALUES (?, ?, ?, ?)",
            (assessment_id, user_id, raw_text, _now()),
        )

    def save_grade(
        self, submission_id: int, score: float, max_score: float, feedback: str, details: list[dict]
    ) -> None:
        self._run(
            """UPDATE submissions SET status = 'graded', score = ?, max_score = ?, feedback = ?,
                      details = ?, graded_at = ? WHERE id = ?""",
            (score, max_score, feedback, json.dumps(details), _now(), submission_id),
        )

    def mark_submission_failed(self, submission_id: int) -> None:
        self._run("UPDATE submissions SET status = 'failed' WHERE id = ?", (submission_id,))

    def get_submission(self, submission_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM submissions WHERE id = ?", (submission_id,))

    def ungraded_submissions(self, assessment_id: int) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM submissions WHERE assessment_id = ? AND status != 'graded' ORDER BY id",
            (assessment_id,),
        )

    def latest_submissions(self, assessment_id: int) -> list[dict[str, Any]]:
        """Each user's most recent submission for an assessment, best score first."""
        return self._all(
            """SELECT s.*, u.username, u.full_name
               FROM submissions s LEFT JOIN users u ON u.user_id = s.user_id
               WHERE s.assessment_id = ?
                 AND s.id = (SELECT MAX(id) FROM submissions x
                             WHERE x.assessment_id = s.assessment_id AND x.user_id = s.user_id)
               ORDER BY s.score IS NULL, s.score DESC, s.id""",
            (assessment_id,),
        )

    def user_submissions(self, user_id: int, limit: int = 15) -> list[dict[str, Any]]:
        return self._all(
            """SELECT s.*, a.title FROM submissions s JOIN assessments a ON a.id = s.assessment_id
               WHERE s.user_id = ? ORDER BY s.id DESC LIMIT ?""",
            (user_id, limit),
        )
