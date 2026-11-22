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
-- Stage 5: why and when grading failed, and whether the admins still have to be told about it.
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS grading_error TEXT;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS failed_at TIMESTAMPTZ;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS failure_notice_due BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_submissions_failed ON submissions(assessment_id) WHERE status = 'failed';
-- Stage 5: how the AI provider's recent requests went, shared by every bot process (one row).
-- Locking it decides, across processes, who tells the admins that the provider is down or back.
CREATE TABLE IF NOT EXISTS ai_health (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    failing_since         TIMESTAMPTZ,
    last_failure_at       TIMESTAMPTZ,
    last_error            TEXT,
    last_success_at       TIMESTAMPTZ,
    down_notified_at      TIMESTAMPTZ      -- the admins were told it is down; cleared once told it is back
);
INSERT INTO ai_health (id) VALUES (1) ON CONFLICT DO NOTHING;
-- Stage 5: when the admins were last sent a notice of each kind (to batch repeated ones).
CREATE TABLE IF NOT EXISTS admin_notices (
    kind         TEXT PRIMARY KEY,
    notified_at  TIMESTAMPTZ NOT NULL
);
-- Stage 7: deadlines, reminders and per-assessment options. NULL options follow the bot-wide
-- settings (SHOW_CORRECT_ANSWERS, ALLOW_RESUBMISSION), as every assessment did before.
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS deadline TIMESTAMPTZ;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS reminders_queued_at TIMESTAMPTZ;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS show_answers BOOLEAN;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS allow_resubmission BOOLEAN;
CREATE INDEX IF NOT EXISTS idx_assessments_deadline ON assessments(deadline)
    WHERE status = 'published' AND deadline IS NOT NULL;
-- Stage 7: one row per student to remind before a deadline. Written in the same transaction that
-- marks the assessment's reminders as queued, and each row is taken by one process (at most once).
CREATE TABLE IF NOT EXISTS reminders (
    assessment_id  BIGINT NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    user_id        BIGINT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | skipped | blocked | failed
    sent_at        TIMESTAMPTZ,
    last_error     TEXT,
    PRIMARY KEY (assessment_id, user_id)
);
-- Stage 8: failed grading retried by the bot itself, and whether the admins are still to be told
-- that it worked (batched like failure notices).
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS auto_regrades INTEGER NOT NULL DEFAULT 0;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS regraded_notice_due BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_submissions_pending ON submissions(id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_questions_assessment ON questions(assessment_id);
CREATE INDEX IF NOT EXISTS idx_submissions_lookup ON submissions(assessment_id, user_id);
CREATE INDEX IF NOT EXISTS idx_draft_answers_user ON draft_answers(user_id, id);
-- Study check-ins (a side feature, outside the staged roadmap). When a user last sent the bot
-- anything: a check-in is not sent to someone who was just using the bot, and a reply resets the
-- count of check-ins ignored in a row. An older version running during a deploy does not set it.
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMPTZ;
-- One row per user who was offered check-ins or chose them. Each due row is taken by one process
-- (FOR UPDATE SKIP LOCKED) and moved to its next time in the same transaction: at most once.
CREATE TABLE IF NOT EXISTS study_checkins (
    user_id       BIGINT PRIMARY KEY,
    status        TEXT NOT NULL,                  -- offered | on | off | blocked
    next_at       TIMESTAMPTZ,                    -- when the next check-in is due (status 'on')
    paused_until  TIMESTAMPTZ,
    ignored       INTEGER NOT NULL DEFAULT 0,     -- check-ins in a row the user did not answer
    last_sent_at  TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_study_checkins_due ON study_checkins(next_at) WHERE status = 'on';
-- Every check-in taken for sending, for /status. Written when it is taken, completed after sending.
CREATE TABLE IF NOT EXISTS checkin_log (
    id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id  BIGINT NOT NULL,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome  TEXT NOT NULL DEFAULT 'sent',        -- sent | failed | blocked
    source   TEXT,                                -- ai | fallback (NULL: stopped before sending)
    error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_checkin_log_sent ON checkin_log(sent_at);
"""


SCHEMA_LOCK = 0x7465_6C65  # any constant shared by every bot process
# An assessment takes answers while it is published and its deadline (if any) has not passed. The
# deadline is checked here, not only when the background work closes it, so that answers stop at
# the deadline itself rather than up to one work interval later.
OPEN = "(a.status = 'published' AND (a.deadline IS NULL OR a.deadline > now()))"


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
            """INSERT INTO users (user_id, username, full_name, is_active, joined_at, last_active_at)
               VALUES (%s, %s, %s, TRUE, %s, now())
               ON CONFLICT (user_id) DO UPDATE SET
                   username = EXCLUDED.username, full_name = EXCLUDED.full_name, is_active = TRUE,
                   last_active_at = now()""",
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
        (a second tap, a copy from another process, or it was discarded) or its deadline has passed. The status change and
        the queue are written in one transaction, so a restart cannot leave one without the other.
        """
        with self._pool.connection() as conn:
            published = conn.execute(
                """UPDATE assessments SET status = 'published', published_at = %s, published_by = %s
                   WHERE id = %s AND status = 'draft' AND (deadline IS NULL OR deadline > now())
                   RETURNING id""",
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
                "UPDATE assessments SET status = 'closed', closed_at = now() "
                "WHERE id = %s AND status = 'published' RETURNING id",
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

    # ── deadlines and per-assessment options (Stage 7) ───────────────────
    def set_deadline(self, assessment_id: int, deadline: datetime | None) -> dict[str, Any] | None:
        """Set or remove the deadline of a draft or an open assessment. Returns the updated row,
        or None (nothing changed) if it is closed or its deadline has already passed.

        Reminders already queued or sent for the old deadline are forgotten, so students who have
        still not submitted are reminded again before the new one.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                f"""UPDATE assessments a SET deadline = %s, reminders_queued_at = NULL
                    WHERE a.id = %s AND (a.status = 'draft' OR {OPEN}) RETURNING a.*""",
                (deadline, assessment_id),
            ).fetchone()
            if row is not None:
                conn.execute("DELETE FROM reminders WHERE assessment_id = %s", (assessment_id,))
            return row

    OPTIONS = ("show_answers", "allow_resubmission")

    def set_option(self, assessment_id: int, option: str, value: bool | None) -> dict[str, Any] | None:
        """Set one per-assessment option (None: follow the bot-wide setting). Returns the row."""
        if option not in self.OPTIONS:
            raise ValueError(f"unknown option {option!r}")
        return self._one(f"UPDATE assessments SET {option} = %s WHERE id = %s RETURNING *",
                         (value, assessment_id))

    def close_due_assessments(self) -> list[dict[str, Any]]:
        """Close every open assessment whose deadline has passed. Each is returned once, to one
        caller (across processes), with how many students were sent it and how many submitted, so
        that exactly one process tells its publisher. Unsent messages are cancelled, as by /close."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """UPDATE assessments SET status = 'closed', closed_at = now()
                   WHERE status = 'published' AND deadline IS NOT NULL AND deadline <= now()
                   RETURNING id, title, deadline, published_by, created_by"""
            ).fetchall()
            for row in rows:
                conn.execute("UPDATE deliveries SET status = 'cancelled', claimed_until = NULL "
                             "WHERE assessment_id = %s AND status = 'pending'", (row["id"],))
                conn.execute("UPDATE reminders SET status = 'skipped' "
                             "WHERE assessment_id = %s AND status = 'pending'", (row["id"],))
                counts = conn.execute(
                    """SELECT (SELECT COUNT(*) FROM deliveries WHERE assessment_id = %s AND status = 'sent') AS sent,
                              (SELECT COUNT(DISTINCT user_id) FROM submissions WHERE assessment_id = %s) AS submitted""",
                    (row["id"], row["id"]),
                ).fetchone()
                row.update(counts)
        return sorted(rows, key=lambda r: r["id"])

    # ── reminders before a deadline (Stage 7) ────────────────────────────
    def queue_due_reminders(self, before_seconds: float) -> list[int]:
        """Queue reminders for open assessments whose deadline is less than `before_seconds` away.

        Each assessment is queued once (until its deadline changes), by one process, in one
        transaction with its rows: one per student who was sent it, can still be reached and has
        not submitted. An assessment published when its deadline was already that close is not
        queued: its students were just sent it, deadline included. Returns the assessments queued.
        """
        with self._pool.connection() as conn:
            ids = [r["id"] for r in conn.execute(
                f"""UPDATE assessments a SET reminders_queued_at = now()
                    WHERE {OPEN} AND a.deadline IS NOT NULL AND a.reminders_queued_at IS NULL
                      AND a.deadline - make_interval(secs => %s) <= now()
                      AND a.published_at::timestamptz < a.deadline - make_interval(secs => %s)
                    RETURNING a.id""",
                (before_seconds, before_seconds),
            ).fetchall()]
            if ids:
                conn.execute(
                    """INSERT INTO reminders (assessment_id, user_id)
                       SELECT d.assessment_id, d.user_id FROM deliveries d
                       JOIN users u ON u.user_id = d.user_id AND u.is_active
                       WHERE d.assessment_id = ANY(%s) AND d.status = 'sent'
                         AND NOT EXISTS (SELECT 1 FROM submissions s
                                         WHERE s.assessment_id = d.assessment_id AND s.user_id = d.user_id)
                       ON CONFLICT DO NOTHING""",
                    (ids,),
                )
            return sorted(ids)

    def claim_reminder(self) -> dict[str, Any] | None:
        """Take the next reminder to send, marking it sent first, so that it is sent at most once
        even with several processes (a process stopping right after this loses that one reminder).

        Reminders for students who have submitted meanwhile, or for an assessment no longer open,
        are marked 'skipped' instead. Returns the reminder with its assessment's title, deadline and
        how many answer messages the student has saved for it.
        """
        with self._pool.connection() as conn:
            conn.execute(
                f"""UPDATE reminders r SET status = 'skipped' FROM assessments a
                    WHERE a.id = r.assessment_id AND r.status = 'pending'
                      AND (NOT {OPEN} OR EXISTS (SELECT 1 FROM submissions s
                                                 WHERE s.assessment_id = r.assessment_id AND s.user_id = r.user_id))"""
            )
            return conn.execute(
                """UPDATE reminders r SET status = 'sent', sent_at = now()
                   FROM (SELECT assessment_id, user_id FROM reminders WHERE status = 'pending'
                         ORDER BY assessment_id, user_id LIMIT 1 FOR UPDATE SKIP LOCKED) next,
                        assessments a
                   WHERE r.assessment_id = next.assessment_id AND r.user_id = next.user_id
                     AND a.id = r.assessment_id
                   RETURNING r.assessment_id, r.user_id, a.title, a.deadline,
                             (SELECT COUNT(*) FROM draft_answers x
                              WHERE x.user_id = r.user_id AND x.assessment_id = r.assessment_id) AS saved"""
            ).fetchone()

    def finish_reminder(self, assessment_id: int, user_id: int, status: str, error: str | None = None) -> None:
        """Record that a claimed reminder could not be sent: 'blocked' or 'failed'."""
        self._run("UPDATE reminders SET status = %s, last_error = %s WHERE assessment_id = %s AND user_id = %s",
                  (status, error, assessment_id, user_id))

    def reminder_counts(self, assessment_id: int) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS n FROM reminders WHERE assessment_id = %s GROUP BY status",
                         (assessment_id,))
        return {r["status"]: r["n"] for r in rows}

    # ── study check-ins (side feature) ───────────────────────────────────
    def offer_checkins(self, user_id: int) -> bool:
        """Record that the user is offered check-ins. True only the first time, in any process."""
        return self._one(
            "INSERT INTO study_checkins (user_id, status) VALUES (%s, 'offered') "
            "ON CONFLICT DO NOTHING RETURNING user_id", (user_id,)) is not None

    def get_checkins(self, user_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM study_checkins WHERE user_id = %s", (user_id,))

    def set_checkins(self, user_id: int, status: str, next_at: datetime | None = None,
                     paused_until: datetime | None = None) -> None:
        """Turn the user's check-ins on (due at `next_at`), off, or pause them (on, due after the pause).
        The count of ignored check-ins starts again: the user has just answered the bot."""
        self._run(
            """INSERT INTO study_checkins (user_id, status, next_at, paused_until)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE SET status = EXCLUDED.status, next_at = EXCLUDED.next_at,
                   paused_until = EXCLUDED.paused_until, ignored = 0, updated_at = now()""",
            (user_id, status, next_at, paused_until))

    def claim_checkin(self, decide, day_start: datetime) -> dict[str, Any] | None:
        """Take the next check-in that is due, decide what to do with it, and record that decision
        in the same transaction, so that each one is handled by one process.

        `decide(row)` gets the check-in row with the user's facts (`user_active`, `last_active_at`,
        `full_name`, and `reminded_today`: a deadline reminder was sent since `day_start`) and
        returns (action, next_at, ignored). "send" logs it as sent before it is sent (at most once:
        a process stopping right after this loses that one check-in); "blocked" stops check-ins to
        a user who blocked the bot; any other action only moves `next_at`. Returns the row with
        `action` and, for "send", `log_id`; None if nothing is due.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                """SELECT c.*, COALESCE(u.is_active, FALSE) AS user_active, u.last_active_at, u.full_name,
                          EXISTS (SELECT 1 FROM reminders r WHERE r.user_id = c.user_id AND r.status = 'sent'
                                  AND r.sent_at >= %s) AS reminded_today
                   FROM study_checkins c LEFT JOIN users u ON u.user_id = c.user_id
                   WHERE c.status = 'on' AND c.next_at <= now()
                   ORDER BY c.next_at, c.user_id LIMIT 1 FOR UPDATE OF c SKIP LOCKED""",
                (day_start,),
            ).fetchone()
            if row is None:
                return None
            action, next_at, ignored = decide(row)
            row = {**row, "action": action, "next_at": next_at, "ignored": ignored, "log_id": None}
            if action == "blocked":
                conn.execute("UPDATE study_checkins SET status = 'blocked', next_at = NULL, updated_at = now() "
                             "WHERE user_id = %s", (row["user_id"],))
            elif action == "send":
                conn.execute("UPDATE study_checkins SET next_at = %s, ignored = %s, last_sent_at = now(), "
                             "paused_until = NULL, updated_at = now() WHERE user_id = %s",
                             (next_at, ignored, row["user_id"]))
                row["log_id"] = conn.execute("INSERT INTO checkin_log (user_id) VALUES (%s) RETURNING id",
                                             (row["user_id"],)).fetchone()["id"]
            else:
                conn.execute("UPDATE study_checkins SET next_at = %s, updated_at = now() WHERE user_id = %s",
                             (next_at, row["user_id"]))
            return row

    def finish_checkin(self, log_id: int, user_id: int, outcome: str, source: str | None,
                       error: str | None = None) -> None:
        """Record how a check-in went: 'sent', 'failed', or 'blocked' (no more check-ins to them)."""
        with self._pool.connection() as conn:
            conn.execute("UPDATE checkin_log SET outcome = %s, source = %s, error = %s WHERE id = %s",
                         (outcome, source, error, log_id))
            if outcome == "blocked":
                conn.execute("UPDATE study_checkins SET status = 'blocked', next_at = NULL, updated_at = now() "
                             "WHERE user_id = %s", (user_id,))

    def prune_checkin_log(self, older_than_days: float) -> int:
        with self._pool.connection() as conn:
            return conn.execute("DELETE FROM checkin_log WHERE sent_at < now() - make_interval(days => %s)",
                                (older_than_days,)).rowcount

    def checkin_overview(self, days: int = 7) -> dict[str, Any]:
        """For /status: who receives check-ins, and how the ones of the last `days` days went."""
        people = self._one(
            """SELECT COUNT(*) FILTER (WHERE status = 'on') AS receiving,
                      COUNT(*) FILTER (WHERE status = 'on' AND paused_until > now()) AS paused,
                      COUNT(*) FILTER (WHERE status = 'offered') AS undecided,
                      COUNT(*) FILTER (WHERE status = 'off') AS declined,
                      COUNT(*) FILTER (WHERE status = 'blocked') AS blocked
               FROM study_checkins""")
        sent = self._one(
            """SELECT COUNT(*) FILTER (WHERE outcome = 'sent') AS sent,
                      COUNT(*) FILTER (WHERE outcome = 'sent' AND source = 'ai') AS by_ai,
                      COUNT(*) FILTER (WHERE outcome = 'sent' AND source = 'fallback') AS fallback,
                      COUNT(*) FILTER (WHERE outcome = 'failed') AS failed,
                      COUNT(*) FILTER (WHERE outcome = 'blocked') AS blocked_now
               FROM checkin_log WHERE sent_at > now() - make_interval(days => %s)""", (days,))
        error = self._one("SELECT error FROM checkin_log WHERE outcome = 'failed' AND error IS NOT NULL "
                          "ORDER BY id DESC LIMIT 1")
        return {**people, **sent, "error": error["error"] if error else None, "days": days}

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
                      JOIN assessments a ON a.id = d.assessment_id AND {OPEN}
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

    def requeue_failed_deliveries(self, assessment_id: int) -> list[int]:
        """/resend (Stage 8): queue again the messages of an open assessment that failed to send.
        Returns the users queued; a second call finds them no longer 'failed' and queues nobody,
        so a double tap or two processes cannot send a copy twice. Blocked users are not included."""
        rows = self._all(
            f"""UPDATE deliveries d SET status = 'pending', attempts = 0, claimed_until = NULL, last_error = NULL
                FROM assessments a
                WHERE a.id = d.assessment_id AND d.assessment_id = %s AND d.status = 'failed' AND {OPEN}
                RETURNING d.user_id""",
            (assessment_id,),
        )
        return sorted(r["user_id"] for r in rows)

    def delivery_statuses(self, assessment_id: int, user_ids: list[int]) -> dict[str, int]:
        rows = self._all("""SELECT status, COUNT(*) AS n FROM deliveries
                            WHERE assessment_id = %s AND user_id = ANY(%s) GROUP BY status""",
                         (assessment_id, user_ids))
        return {r["status"]: r["n"] for r in rows}

    def failed_deliveries_by_assessment(self) -> list[dict[str, Any]]:
        """Open assessments with messages that failed to send (what /resend can act on), newest first."""
        return self._all(
            f"""SELECT d.assessment_id, COUNT(*) AS n FROM deliveries d
                JOIN assessments a ON a.id = d.assessment_id AND {OPEN}
                WHERE d.status = 'failed' GROUP BY d.assessment_id ORDER BY d.assessment_id DESC"""
        )

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
            """SELECT a.id, a.title, a.status, a.created_at, a.deadline,
                      (SELECT COUNT(*) FROM questions q WHERE q.assessment_id = a.id) AS num_questions,
                      (SELECT COUNT(DISTINCT s.user_id) FROM submissions s WHERE s.assessment_id = a.id)
                          AS num_submitters
               FROM assessments a ORDER BY a.id DESC LIMIT %s""",
            (limit,),
        )

    def open_assessments_for(self, user_id: int, default_resubmission: bool) -> list[dict[str, Any]]:
        """Assessments the user can still answer, newest first: open (OPEN), and not submitted by
        them yet unless the assessment allows resubmission (its own option, else the default)."""
        return self._all(
            f"""SELECT a.* FROM assessments a WHERE {OPEN}
                  AND (COALESCE(a.allow_resubmission, %s)
                       OR NOT EXISTS (SELECT 1 FROM submissions s WHERE s.assessment_id = a.id AND s.user_id = %s))
                ORDER BY a.published_at DESC, a.id DESC""",
            (default_resubmission, user_id),
        )

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
                      details = %s, graded_at = %s, grading_claimed_until = NULL, result_due = TRUE,
                      grading_error = NULL
               WHERE id = %s AND status != 'graded' RETURNING id""",
            (score, max_score, feedback, json.dumps(details), _now(), submission_id),
        ) is not None

    def mark_report_sent(self, assessment_id: int) -> None:
        """For an assessment published to nobody: its (empty) report has been sent."""
        self._run("UPDATE assessments SET delivery_reported_at = now() WHERE id = %s", (assessment_id,))

    def mark_submission_failed(self, submission_id: int, error: str | None = None, notify: bool = True) -> None:
        """Grading failed; the submission waits for a retry (automatic, or /regrade). The admins are
        to be told unless `notify` is False (an automatic retry of one they were already told about)."""
        self._run(
            "UPDATE submissions SET status = 'failed', grading_claimed_until = NULL, grading_error = %s, "
            "failed_at = now(), failure_notice_due = failure_notice_due OR %s "
            "WHERE id = %s AND status != 'graded'",
            (error, notify, submission_id),
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
                conn.execute("UPDATE submissions SET status = 'failed', grading_claimed_until = NULL, "
                             "grading_error = %s, failed_at = now(), failure_notice_due = TRUE WHERE id = %s",
                             (f"grading was interrupted {row['grading_attempts']} times", row["id"]))
                return "gave_up", row
            row = conn.execute(
                """UPDATE submissions SET grading_attempts = grading_attempts + 1,
                          grading_claimed_until = now() + make_interval(secs => %s)
                   WHERE id = %s RETURNING *""",
                (lease_seconds, row["id"]),
            ).fetchone()
            return "claimed", row

    def claim_auto_regrade(self, lease_seconds: float, max_auto: int, delay_seconds: float) -> dict[str, Any] | None:
        """Take the next failed submission for the bot to grade again by itself (Stage 8), or None.

        Only while the AI provider is answering (its last request got a reply: no failures in a
        row), and only `delay_seconds` × 2^(retries so far) after the failure, at most `max_auto`
        times per submission; after that it waits for /regrade. The submission is leased exactly
        like a /regrade (claim_for_regrade), so the two never grade it at the same time.
        """
        return self._one(
            """UPDATE submissions SET status = 'pending', grading_attempts = grading_attempts + 1,
                      auto_regrades = auto_regrades + 1,
                      grading_claimed_until = now() + make_interval(secs => %s)
               WHERE id = (
                   SELECT s.id FROM submissions s
                   WHERE s.status = 'failed' AND s.auto_regrades < %s
                     AND COALESCE(s.failed_at, s.created_at::timestamptz)
                         < now() - make_interval(secs => %s * power(2, s.auto_regrades))
                     AND (SELECT consecutive_failures FROM ai_health WHERE id = 1) = 0
                   ORDER BY s.id LIMIT 1 FOR UPDATE SKIP LOCKED)
               RETURNING *""",
            (lease_seconds, max_auto, delay_seconds),
        )

    def mark_regraded_notice_due(self, submission_id: int) -> None:
        self._run("UPDATE submissions SET regraded_notice_due = TRUE WHERE id = %s", (submission_id,))

    def claim_regraded_notice(self, min_interval: float) -> list[dict[str, Any]]:
        """Submissions the bot graded again by itself that the admins have not been told about, each
        returned once, to one caller; nothing while the last such notice is under `min_interval`
        seconds old (the background loop sends what is left later)."""
        with self._pool.connection() as conn:
            conn.execute("""INSERT INTO admin_notices (kind, notified_at) VALUES ('auto_regraded', '-infinity')
                            ON CONFLICT DO NOTHING""")
            last = conn.execute("""SELECT notified_at > now() - make_interval(secs => %s) AS recent
                                   FROM admin_notices WHERE kind = 'auto_regraded' FOR UPDATE""",
                                (min_interval,)).fetchone()
            if last["recent"]:
                return []
            rows = conn.execute("""UPDATE submissions SET regraded_notice_due = FALSE
                                   WHERE regraded_notice_due RETURNING id, assessment_id""").fetchall()
            if rows:
                conn.execute("UPDATE admin_notices SET notified_at = now() WHERE kind = 'auto_regraded'")
            return sorted(rows, key=lambda r: r["id"])

    def auto_retry_pending(self, max_auto: int) -> int:
        """Failed submissions the bot will still retry by itself."""
        return self._one("SELECT COUNT(*) AS n FROM submissions WHERE status = 'failed' AND auto_regrades < %s",
                         (max_auto,))["n"]

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

    # ── admin visibility (Stage 5) ───────────────────────────────────────
    def record_ai_success(self) -> dict[str, Any] | None:
        """A request reached the AI provider. Returns the outage that just ended (its
        `failing_since`) if the admins had been told it was down, so that exactly one caller,
        across all processes, tells them it is back."""
        with self._pool.connection() as conn:
            before = conn.execute("SELECT * FROM ai_health WHERE id = 1 FOR UPDATE").fetchone()
            conn.execute(
                """UPDATE ai_health SET consecutive_failures = 0, failing_since = NULL,
                          last_success_at = now(), down_notified_at = NULL WHERE id = 1"""
            )
            return before if before and before["down_notified_at"] is not None else None

    def record_ai_failure(self, error: str, alert_after: int) -> dict[str, Any] | None:
        """A request could not be made at all. Returns the health row when this failure makes
        `alert_after` in a row and the admins have not been told yet (to exactly one caller)."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """UPDATE ai_health SET consecutive_failures = consecutive_failures + 1,
                          failing_since = COALESCE(failing_since, now()), last_failure_at = now(),
                          last_error = %s
                   WHERE id = 1 RETURNING *""",
                (error,),
            ).fetchone()
            if row["consecutive_failures"] < alert_after or row["down_notified_at"] is not None:
                return None
            return conn.execute("UPDATE ai_health SET down_notified_at = now() WHERE id = 1 RETURNING *").fetchone()

    def ai_health(self) -> dict[str, Any]:
        return self._one("SELECT * FROM ai_health WHERE id = 1")

    def claim_grading_failure_notice(self, min_interval: float, force: bool = False) -> list[dict[str, Any]]:
        """Grading failures the admins have not been told about, each returned once, to one caller.

        Returns nothing while the last notice is less than `min_interval` seconds old, or while the
        admins know the AI provider is down (its recovery message reports them), unless `force`.
        """
        with self._pool.connection() as conn:
            conn.execute("""INSERT INTO admin_notices (kind, notified_at) VALUES ('grading_failed', '-infinity')
                            ON CONFLICT DO NOTHING""")
            last = conn.execute("""SELECT notified_at > now() - make_interval(secs => %s) AS recent
                                   FROM admin_notices WHERE kind = 'grading_failed' FOR UPDATE""",
                                (min_interval,)).fetchone()
            if not force:
                down = conn.execute("SELECT down_notified_at IS NOT NULL AS down FROM ai_health WHERE id = 1").fetchone()
                if last["recent"] or (down and down["down"]):
                    return []
            # A failure fixed by /regrade before the notice is no longer 'failed', so it is not reported.
            rows = conn.execute(
                """UPDATE submissions SET failure_notice_due = FALSE
                   WHERE failure_notice_due AND status = 'failed'
                   RETURNING id, assessment_id, grading_error, failed_at, auto_regrades"""
            ).fetchall()
            if rows:
                conn.execute("UPDATE admin_notices SET notified_at = now() WHERE kind = 'grading_failed'")
            return sorted(rows, key=lambda r: r["id"])

    def failed_grading_by_assessment(self) -> list[dict[str, Any]]:
        """Submissions waiting for /regrade, per assessment, newest assessment first."""
        return self._all(
            """SELECT assessment_id, COUNT(*) AS n FROM submissions WHERE status = 'failed'
               GROUP BY assessment_id ORDER BY assessment_id DESC"""
        )

    def grading_counts(self, assessment_id: int, grading_lease: float) -> dict[str, int]:
        """How grading stands for one assessment (Stage 6): failed (needs /regrade), being graded
        now (lease live), interrupted (lease ran out; the background work grades it again)."""
        return self._one(
            f"""SELECT COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                       COUNT(*) FILTER (WHERE status = 'pending' AND NOT {self._STALE}) AS grading_now,
                       COUNT(*) FILTER (WHERE status = 'pending' AND {self._STALE}) AS interrupted
                FROM submissions WHERE assessment_id = %s AND status IN ('pending', 'failed')""",
            (grading_lease, grading_lease, assessment_id),
        )

    def status_overview(self, grading_lease: float) -> dict[str, Any]:
        """Everything /status shows, read from the shared tables (so it covers every process)."""
        grading = self._one(
            f"""SELECT COUNT(*) FILTER (WHERE status = 'pending' AND NOT {self._STALE}) AS grading_now,
                       COUNT(*) FILTER (WHERE status = 'pending' AND {self._STALE}) AS interrupted,
                       COUNT(*) FILTER (WHERE status = 'graded' AND result_due) AS results_unsent
                FROM submissions WHERE status IN ('pending', 'graded')""",
            (grading_lease, grading_lease),
        )
        latest_error = self._one(
            """SELECT grading_error FROM submissions WHERE status = 'failed' AND grading_error IS NOT NULL
               ORDER BY failed_at DESC NULLS LAST, id DESC LIMIT 1"""
        )
        deliveries = self._all(
            """SELECT assessment_id, status, COUNT(*) AS n FROM deliveries
               WHERE status IN ('pending', 'failed') GROUP BY assessment_id, status
               ORDER BY assessment_id DESC"""
        )
        delivery_error = self._one(
            """SELECT last_error FROM deliveries WHERE status = 'failed' AND last_error IS NOT NULL
               ORDER BY assessment_id DESC, user_id DESC LIMIT 1"""
        )
        blocked = self._one("SELECT COUNT(*) AS n FROM users WHERE NOT is_active")["n"]
        return {
            **grading,
            "failed_grading": self.failed_grading_by_assessment(),
            "grading_error": latest_error["grading_error"] if latest_error else None,
            "deliveries": deliveries,
            "delivery_error": delivery_error["last_error"] if delivery_error else None,
            "blocked_users": blocked,
            "ai": self.ai_health(),
        }

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
        no longer accepts this user's answers (closed, its deadline passed, or already submitted
        and it does not allow resubmission); the draft is then discarded. `allow_resubmission` is
        the bot-wide default, used when the assessment has no option of its own. Checking, inserting
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
            # FOR SHARE: closing the assessment (/close, or its deadline) waits for this transaction.
            assessment = conn.execute(
                f"SELECT {OPEN} AS open, COALESCE(a.allow_resubmission, %s) AS resubmit "
                "FROM assessments a WHERE a.id = %s FOR SHARE",
                (allow_resubmission, assessment_id)).fetchone()
            already = assessment is not None and not assessment["resubmit"] and conn.execute(
                "SELECT 1 FROM submissions WHERE assessment_id = %s AND user_id = %s LIMIT 1",
                (assessment_id, user_id),
            ).fetchone() is not None
            if assessment is None or not assessment["open"] or already:
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
