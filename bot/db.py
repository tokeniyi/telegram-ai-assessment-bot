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
-- Every Telegram update the bot has started to handle. Telegram delivers an update again when
-- the bot did not confirm it (a crash or redeploy right after fetching, or two bot processes
-- polling at once during a deploy); the primary key lets exactly one process claim it.
CREATE TABLE IF NOT EXISTS processed_updates (
    bot_id       BIGINT NOT NULL,
    update_id    BIGINT NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (bot_id, update_id)
);
CREATE INDEX IF NOT EXISTS idx_processed_updates_received ON processed_updates(received_at);
-- One row per user who should receive a published assessment (an outbox). Publishing writes
-- every row in the same transaction that changes the status, and each row is marked once its
-- message is sent, so a restart in the middle of sending leaves a list of who is still owed it.
CREATE TABLE IF NOT EXISTS deliveries (
    assessment_id  BIGINT NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    user_id        BIGINT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | blocked | failed | cancelled
    attempts       INTEGER NOT NULL DEFAULT 0,
    claimed_until  TIMESTAMPTZ,                      -- a process is sending it until then
    last_error     TEXT,
    sent_at        TIMESTAMPTZ,
    PRIMARY KEY (assessment_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_pending ON deliveries(assessment_id, user_id) WHERE status = 'pending';
-- Stage 3 columns, added in place so that an existing database is upgraded on start.
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS published_by BIGINT;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS delivery_reported_at TIMESTAMPTZ;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS grading_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS grading_claimed_until TIMESTAMPTZ;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS result_sent_at TIMESTAMPTZ;
-- Set by this version when it saves a grade, cleared once the result is sent. An older version
-- running during a deploy never sets it, so a result it sent itself is never sent again.
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS result_due BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_submissions_pending ON submissions(id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_questions_assessment ON questions(assessment_id);
CREATE INDEX IF NOT EXISTS idx_submissions_lookup ON submissions(assessment_id, user_id);
CREATE INDEX IF NOT EXISTS idx_draft_answers_user ON draft_answers(user_id, id);
"""


SCHEMA_LOCK = 0x7465_6C65  # any constant shared by every bot process


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
            # Two bot versions start at once during a deploy; creating and altering the same
            # tables concurrently can fail, so the schema is applied by one process at a time.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
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

    def ping(self, timeout: float = 3) -> bool:
        """Whether the database answers (for the health check), within about `timeout`
        seconds: both getting a connection and the query itself are bounded."""
        with self._pool.connection(timeout=timeout) as conn:
            conn.execute("SELECT set_config('statement_timeout', %s, true)", (f"{int(timeout * 1000)}ms",))
            return conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1

    # ── processed Telegram updates ───────────────────────────────────────
    def claim_update(self, bot_id: int, update_id: int) -> bool:
        """Record that this update is being handled. Returns False if it was already claimed,
        by this process or another one, so the caller must not handle it again."""
        row = self._one(
            """INSERT INTO processed_updates (bot_id, update_id) VALUES (%s, %s)
               ON CONFLICT DO NOTHING RETURNING update_id""",
            (bot_id, update_id),
        )
        return row is not None

    def prune_processed_updates(self, older_than_hours: float) -> int:
        """Forget updates claimed more than `older_than_hours` ago. Returns how many."""
        with self._pool.connection() as conn:
            return conn.execute(
                "DELETE FROM processed_updates WHERE received_at < now() - make_interval(secs => %s)",
                (older_than_hours * 3600,),
            ).rowcount

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

    def replace_questions(self, assessment_id: int, title: str, questions: list[dict[str, Any]]) -> bool:
        """Swap a draft's questions. Returns False, changing nothing, if it is no longer a draft
        (published or discarded while the new questions were being generated)."""
        with self._pool.connection() as conn:
            draft = conn.execute(
                "UPDATE assessments SET title = %s WHERE id = %s AND status = 'draft' RETURNING id",
                (title, assessment_id),
            ).fetchone()
            if draft is None:
                return False
            conn.execute("DELETE FROM questions WHERE assessment_id = %s", (assessment_id,))
            self._insert_questions(conn, assessment_id, questions)
            return True

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

    def discard_draft_assessment(self, assessment_id: int) -> bool:
        """Delete the assessment if it is still a draft. Returns whether it was."""
        return self._one(
            "DELETE FROM assessments WHERE id = %s AND status = 'draft' RETURNING id", (assessment_id,)
        ) is not None

    def publish_assessment(self, assessment_id: int, published_by: int) -> int | None:
        """Publish a draft and queue it for every active user, exactly once.

        Returns the number of users queued, or None if the assessment is not a draft any more
        (a second tap, a copy from another process, or it was discarded). The status change and
        the queue are written in one transaction, so a restart cannot leave one without the other.
        """
        with self._pool.connection() as conn:
            published = conn.execute(
                """UPDATE assessments SET status = 'published', published_at = %s, published_by = %s
                   WHERE id = %s AND status = 'draft' RETURNING id""",
                (_now(), published_by, assessment_id),
            ).fetchone()
            if published is None:
                return None
            return conn.execute(
                """INSERT INTO deliveries (assessment_id, user_id)
                   SELECT %s, user_id FROM users WHERE is_active ORDER BY user_id
                   ON CONFLICT DO NOTHING""",
                (assessment_id,),
            ).rowcount

    def close_assessment(self, assessment_id: int) -> bool:
        """Stop accepting answers. Users not sent the assessment yet will not be. Returns False
        if it was not open."""
        with self._pool.connection() as conn:
            closed = conn.execute(
                "UPDATE assessments SET status = 'closed' WHERE id = %s AND status = 'published' RETURNING id",
                (assessment_id,),
            ).fetchone()
            if closed is None:
                return False
            conn.execute(
                "UPDATE deliveries SET status = 'cancelled', claimed_until = NULL "
                "WHERE assessment_id = %s AND status = 'pending'",
                (assessment_id,),
            )
            return True

    # ── deliveries: the published assessment, sent to each user once ────
    def claim_delivery(self, lease_seconds: float, assessment_id: int | None = None) -> dict[str, Any] | None:
        """Take the next message to send, so that no other process sends it while it is leased.

        Rows whose lease ran out (the process sending them stopped) can be taken again.
        """
        only = "AND d.assessment_id = %s" if assessment_id is not None else ""
        params = (lease_seconds,) + ((assessment_id,) if assessment_id is not None else ())
        return self._one(
            f"""UPDATE deliveries d SET attempts = d.attempts + 1,
                       claimed_until = now() + make_interval(secs => %s)
                FROM (SELECT d.assessment_id, d.user_id FROM deliveries d
                      JOIN assessments a ON a.id = d.assessment_id AND a.status = 'published'
                      WHERE d.status = 'pending' AND (d.claimed_until IS NULL OR d.claimed_until < now()) {only}
                      ORDER BY d.assessment_id, d.user_id LIMIT 1
                      FOR UPDATE OF d SKIP LOCKED) next
                WHERE d.assessment_id = next.assessment_id AND d.user_id = next.user_id
                RETURNING d.*""",
            params,
        )

    def finish_delivery(self, assessment_id: int, user_id: int, status: str, error: str | None = None) -> None:
        """Record the outcome of a claimed delivery: 'sent', 'blocked' or 'failed'. A row cancelled
        by /close while its message was being sent still records what happened to it."""
        self._run(
            """UPDATE deliveries SET status = %s, last_error = %s, claimed_until = NULL,
                      sent_at = CASE WHEN %s = 'sent' THEN now() END
               WHERE assessment_id = %s AND user_id = %s AND status IN ('pending', 'cancelled')""",
            (status, error, status, assessment_id, user_id),
        )

    def retry_delivery_later(
        self, assessment_id: int, user_id: int, delay_seconds: float, max_attempts: int, error: str
    ) -> str:
        """A send failed for a reason that may pass. Try again after `delay_seconds`, unless it
        has been tried `max_attempts` times. Returns the new status ('pending' or 'failed')."""
        row = self._one(
            """UPDATE deliveries SET last_error = %s,
                      status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END,
                      claimed_until = now() + make_interval(secs => %s)
               WHERE assessment_id = %s AND user_id = %s AND status = 'pending' RETURNING status""",
            (error, max_attempts, delay_seconds, assessment_id, user_id),
        )
        return row["status"] if row else "cancelled"

    def delivery_counts(self, assessment_id: int) -> dict[str, int]:
        rows = self._all(
            "SELECT status, COUNT(*) AS n FROM deliveries WHERE assessment_id = %s GROUP BY status",
            (assessment_id,),
        )
        return {r["status"]: r["n"] for r in rows}

    def claim_delivery_reports(self, assessment_id: int | None = None) -> list[dict[str, Any]]:
        """Assessments whose sending has finished and whose publisher has not been told yet.

        Each is returned once, to one caller, with its delivery counts.
        """
        only = "AND a.id = %s" if assessment_id is not None else ""
        with self._pool.connection() as conn:
            # An assessment closed by an older version (during a deploy) still has pending rows;
            # they will never be sent, so they count as cancelled.
            conn.execute(
                """UPDATE deliveries d SET status = 'cancelled' FROM assessments a
                   WHERE a.id = d.assessment_id AND a.status != 'published' AND d.status = 'pending'"""
            )
            rows = conn.execute(
                f"""UPDATE assessments a SET delivery_reported_at = now()
                    WHERE a.delivery_reported_at IS NULL {only}
                      AND EXISTS (SELECT 1 FROM deliveries d WHERE d.assessment_id = a.id)
                      AND NOT EXISTS (SELECT 1 FROM deliveries d
                                      WHERE d.assessment_id = a.id AND d.status = 'pending')
                    RETURNING a.id, a.title, a.published_by, a.created_by""",
                (assessment_id,) if assessment_id is not None else (),
            ).fetchall()
        for row in rows:
            row["counts"] = self.delivery_counts(row["id"])
        return rows

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
    ) -> bool:
        """Store the grade. Returns False, changing nothing, if the submission was already graded
        (by another process that picked it up too), so only one result is ever sent."""
        return self._one(
            """UPDATE submissions SET status = 'graded', score = %s, max_score = %s, feedback = %s,
                      details = %s, graded_at = %s, grading_claimed_until = NULL, result_due = TRUE
               WHERE id = %s AND status != 'graded' RETURNING id""",
            (score, max_score, feedback, json.dumps(details), _now(), submission_id),
        ) is not None

    def mark_report_sent(self, assessment_id: int) -> None:
        """For an assessment published to nobody: its (empty) report has been sent."""
        self._run("UPDATE assessments SET delivery_reported_at = now() WHERE id = %s", (assessment_id,))

    def mark_submission_failed(self, submission_id: int) -> None:
        self._run(
            "UPDATE submissions SET status = 'failed', grading_claimed_until = NULL "
            "WHERE id = %s AND status != 'graded'",
            (submission_id,),
        )

    # ── grading that survives a restart ──────────────────────────────────
    # A submission being graded is leased: grading_claimed_until says until when the process that
    # took it is expected to finish. A 'pending' submission whose lease ran out was abandoned (the
    # process stopped) and may be taken again. Rows written before leases existed have none; their
    # creation time plus one lease stands in for it.
    _STALE = "COALESCE(grading_claimed_until, created_at::timestamptz + make_interval(secs => %s)) < now()"

    def claim_stale_grading(self, lease_seconds: float, max_attempts: int) -> tuple[str, dict[str, Any]] | None:
        """Take the next abandoned 'pending' submission.

        Returns ("claimed", row) when it should be graded now (its lease is renewed), ("gave_up",
        row) when it has already been tried `max_attempts` times (it is marked 'failed' for
        /regrade), or None when there is nothing to recover.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                f"""SELECT * FROM submissions WHERE status = 'pending' AND {self._STALE}
                    ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED""",
                (lease_seconds,),
            ).fetchone()
            if row is None:
                return None
            if row["grading_attempts"] >= max_attempts:
                conn.execute("UPDATE submissions SET status = 'failed', grading_claimed_until = NULL "
                             "WHERE id = %s", (row["id"],))
                return "gave_up", row
            row = conn.execute(
                """UPDATE submissions SET grading_attempts = grading_attempts + 1,
                          grading_claimed_until = now() + make_interval(secs => %s)
                   WHERE id = %s RETURNING *""",
                (lease_seconds, row["id"]),
            ).fetchone()
            return "claimed", row

    def claim_for_regrade(self, submission_id: int, lease_seconds: float) -> bool:
        """Take a submission for /regrade: a failed one, or a pending one nobody is grading.
        Returns False if it is graded or another process is grading it right now."""
        return self._one(
            f"""UPDATE submissions SET status = 'pending', grading_attempts = grading_attempts + 1,
                       grading_claimed_until = now() + make_interval(secs => %s)
                WHERE id = %s AND (status = 'failed' OR (status = 'pending' AND {self._STALE}))
                RETURNING id""",
            (lease_seconds, submission_id, lease_seconds),
        ) is not None

    def mark_result_sent(self, submission_id: int) -> None:
        """Record that the user has the result of this graded submission."""
        self._run("UPDATE submissions SET result_sent_at = now(), result_due = FALSE "
                  "WHERE id = %s AND result_due", (submission_id,))

    def claim_unsent_result(self, grace_seconds: float) -> dict[str, Any] | None:
        """A graded submission whose result was never sent (the process stopped or Telegram
        failed between saving the grade and sending it), graded more than `grace_seconds` ago.
        It is marked as sent before it is returned, so this retry happens at most once.

        Only grades saved by this version (result_due) are considered, so results that an older
        version sent itself are not sent again.
        """
        return self._one(
            """UPDATE submissions SET result_sent_at = now(), result_due = FALSE WHERE id = (
                   SELECT id FROM submissions
                   WHERE status = 'graded' AND result_due
                     AND graded_at::timestamptz < now() - make_interval(secs => %s)
                   ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
               RETURNING *""",
            (grace_seconds,),
        )

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

    def submit_draft(
        self, user_id: int, assessment_id: int, allow_resubmission: bool, lease_seconds: float = 600,
    ) -> tuple[str, int | None]:
        """Turn the user's draft for `assessment_id` into a submission, exactly once.

        Returns ("submitted", submission_id), ("empty", None) when there is no draft for it
        (already submitted, discarded, or never written), or ("closed", None) when the assessment
        no longer accepts this user's answers; the draft is then discarded. Checking, inserting
        the submission and deleting the draft happen in one transaction under the user's lock,
        so repeated or concurrent submits create at most one submission.

        The new submission is leased to the caller for `lease_seconds` (see claim_stale_grading):
        if the caller stops before grading it, another process grades it once the lease runs out.
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
                """INSERT INTO submissions (assessment_id, user_id, raw_text, created_at,
                                            grading_attempts, grading_claimed_until)
                   VALUES (%s, %s, %s, %s, 1, now() + make_interval(secs => %s)) RETURNING id""",
                (assessment_id, user_id, "\n".join(parts), _now(), lease_seconds),
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
