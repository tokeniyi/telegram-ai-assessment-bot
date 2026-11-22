"""Stage 5: what the admins can see (/status), what they are told (alerts) and what the logs say.

Everything goes through the bot with the fake Telegram API and AI provider. "Another process" is a
second harness on the same database. The real AI SDKs are checked against a small local HTTP
server standing in for the provider.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time

import pytest
from telegram.error import TimedOut

from bot import alerts
from bot.ai import AIError, AIUnavailable
from bot.ai.providers import AnthropicProvider, OpenAICompatibleProvider
from bot.db import Database
from bot.handlers import admin, work
from bot.logs import JsonFormatter, event
from tests.conftest import ADMIN_ID, STUDENT_ID, Crash
from tests.test_deploy import ROOT, ThreadedFakeBotAPI, bot_env, free_port, health
from tests.test_recovery import STUDENTS, draft_with_students, expire_grading_leases

DOWN = AIUnavailable("Could not reach the AI provider. Check AI_BASE_URL and the network.")


async def status_text(h):
    await h.text(ADMIN_ID, "/status")
    return h.tg.texts(ADMIN_ID, "editMessageText")[-1]


def notices(h):
    """Messages the admins got from monitoring: grading failures, AI down, AI back."""
    marks = ("⚠️ Grading failed", "🚨", "✅ The AI provider")
    return [t for t in h.tg.texts(ADMIN_ID) if t.startswith(marks)]


async def published_with_answers(h, students=STUDENTS):
    """An assessment sent to `students`, each of whom has typed an answer but not submitted."""
    await draft_with_students(h, students)
    await h.press(ADMIN_ID, "pub:1")
    for user_id in students:
        await h.text(user_id, "1. B 2. water")


def let_notice_interval_pass(db):
    db._run("UPDATE admin_notices SET notified_at = now() - interval '16 minutes'")


# ── /status ──────────────────────────────────────────────────────────────
async def test_status_is_for_admins_and_says_when_nothing_needs_attention(harness):
    h = harness
    await h.text(STUDENT_ID, "/status")
    assert "administrators only" in h.tg.texts(STUDENT_ID)[-1]
    assert h.ai.checks == 0

    text = await status_text(h)
    assert "✅ reachable" in text and "fake / fake-model" not in text  # provider name is the setting
    assert "openai / fake-model" in text
    assert "Recent requests: none recorded yet" in text
    assert "✅ Nothing waiting" in text and "✅ Nothing queued or failed" in text
    assert "(polling)" in text


async def test_status_shows_grading_and_sending_problems_from_every_process(make_harness):
    old = await make_harness(allow_chat=False)
    blocked, timed_out, retrying = STUDENTS[:3]

    def fail(method, params):
        if method != "sendMessage" or "Which organelle" not in params.get("text", "") \
                or "Draft #" in params["text"]:
            return None
        chat = int(params["chat_id"])
        if chat == blocked:
            return 403, "Forbidden: bot was blocked by the user"
        if chat == timed_out:
            raise TimedOut()
        if chat == retrying:
            return 400, "Bad Request: chat not found"
        return None

    old.tg.fail = fail
    await draft_with_students(old)
    await old.press(ADMIN_ID, "pub:1")
    old.tg.fail = None

    failing, crashing = STUDENTS[3:]
    await old.text(failing, "1. B")
    old.ai.error = AIError("The AI returned a reply that could not be understood (no results).")
    await old.press(failing, "ans:submit:1")
    await old.text(crashing, "1. B")
    old.ai.error = Crash()
    with pytest.raises(Crash):
        await old.press(crashing, "ans:submit:1")

    new = await make_harness()  # another process: everything comes from the database
    text = await status_text(new)
    assert "⏳ 1 being graded now" in text  # the crashed process's lease has not run out yet
    assert "❌ 1 failed." in text and "Retry grading 1 submission to #1: <code>/regrade 1</code>" in text
    assert "Latest error: The AI returned a reply that could not be understood" in text
    assert "⏳ 1 message(s) still to send (#1: 1)" in text
    assert "❌ 1 could not be sent (#1: 1)" in text and "Latest error: Timed out" in text
    assert "🚫 1 user(s) have blocked the bot" in text
    assert "Recent requests: ✅" in text

    expire_grading_leases(new.db)
    text = await status_text(new)
    assert "🔁 1 interrupted; graded again automatically" in text and "being graded now" not in text


async def test_status_reports_an_ai_provider_that_does_not_answer(harness, monkeypatch):
    h = harness
    await h.text(ADMIN_ID, "/start")
    h.ai.error = DOWN
    for _ in range(2):
        await h.text(STUDENT_ID, "hello")  # chat requests that fail
    h.ai.check_error = DOWN
    text = await status_text(h)
    assert "Now: ❌ Could not reach the AI provider" in text
    assert "❌ 2 in a row failed since" in text and "Admins alerted" not in text

    h.ai.check_error = None
    h.ai.check_delay = 5
    monkeypatch.setattr(admin, "STATUS_CHECK_TIMEOUT", 0.2)
    started = time.monotonic()
    text = await status_text(h)
    assert "Now: ❌ no answer within 0.2 s" in text and time.monotonic() - started < 3

    h.ai.check = None  # a provider without the check method
    text = await status_text(h)
    assert "not checked (this provider has no check)" in text


# ── alerts: the AI provider is down, and back ────────────────────────────
async def test_admins_are_told_once_when_the_ai_is_down_and_once_when_it_is_back(harness):
    h = harness
    await h.text(ADMIN_ID, "/start")
    h.ai.error = DOWN
    for _ in range(alerts.AI_DOWN_AFTER - 1):
        await h.text(STUDENT_ID, "hello")
    assert notices(h) == []  # a couple of failures is not an outage

    await h.text(STUDENT_ID, "hello")
    assert len(notices(h)) == 1
    assert notices(h)[0].startswith("🚨 The AI provider is not answering: 3 requests in a row failed")
    assert "Could not reach the AI provider" in notices(h)[0]
    for _ in range(3):
        await h.text(STUDENT_ID, "hello")
    assert len(notices(h)) == 1  # not repeated while it stays down
    assert "Admins alerted at" in await status_text(h)

    h.ai.error = None
    await h.text(STUDENT_ID, "hello")
    await h.text(STUDENT_ID, "hello")
    assert len(notices(h)) == 2 and notices(h)[1].startswith("✅ The AI provider is answering again")
    assert "Recent requests: ✅" in await status_text(h)


async def test_a_bad_reply_is_not_an_outage(harness):
    """A reply that arrived but could not be used shows the provider is up."""
    h = harness
    h.ai.replies = ["not json"] * 10
    for _ in range(4):
        await h.text(STUDENT_ID, "hello")
    assert h.db.ai_health()["consecutive_failures"] == 0 and notices(h) == []


async def test_two_processes_failing_at_once_alert_once(make_harness):
    first, second = await make_harness(), await make_harness()
    for h in (first, second):
        await h.text(ADMIN_ID, "/start")
        h.ai.error = DOWN
    await asyncio.gather(*[h.text(STUDENT_ID + i, "hello") for i in range(4) for h in (first, second)])
    assert len(notices(first)) + len(notices(second)) == 1

    first.ai.error = second.ai.error = None
    await asyncio.gather(*[h.text(STUDENT_ID + i, "hello") for i in range(3) for h in (first, second)])
    back = [t for h in (first, second) for t in notices(h) if t.startswith("✅")]
    assert len(back) == 1


def test_concurrent_ai_outcomes_decide_one_alert_each(pg_url):
    """Many threads recording failures (and then successes) at once: exactly one of them is
    told to send each message. Without the row lock, several would see the threshold."""
    dbs = [Database(pg_url, max_size=2) for _ in range(8)]
    try:
        barrier = threading.Barrier(len(dbs))
        due = []

        def fail(db):
            barrier.wait()
            for _ in range(5):
                due.append(db.record_ai_failure("down", alerts.AI_DOWN_AFTER))

        threads = [threading.Thread(target=fail, args=(db,)) for db in dbs]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sum(d is not None for d in due) == 1
        assert dbs[0].ai_health()["consecutive_failures"] == 40

        recovered = []

        def succeed(db):
            barrier.wait()
            recovered.append(db.record_ai_success())

        threads = [threading.Thread(target=succeed, args=(db,)) for db in dbs]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sum(r is not None for r in recovered) == 1
    finally:
        for db in dbs:
            db.close()


async def test_monitoring_failures_never_break_the_work(harness, monkeypatch, caplog):
    h = harness

    def broken(*args, **kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(h.db, "record_ai_success", broken)
    monkeypatch.setattr(h.db, "record_ai_failure", broken)
    await h.text(STUDENT_ID, "hello")
    assert "Chat reply to: hello" in h.tg.texts(STUDENT_ID)[-1]
    h.ai.error = DOWN
    await h.text(STUDENT_ID, "hello")  # the user still gets the usual "AI unavailable" answer
    assert "Could not record" in caplog.text


# ── alerts: grading failures are batched ─────────────────────────────────
async def test_grading_failures_are_reported_at_once_then_batched(harness):
    h = harness
    await published_with_answers(h)
    h.ai.error = AIError("The AI returned a reply that could not be understood (no results).")
    for user_id in STUDENTS:
        await h.press(user_id, "ans:submit:1")
    assert [h.db.get_submission(i)["status"] for i in range(1, 6)] == ["failed"] * 5
    assert len(notices(h)) == 1  # the first one at once, the same wording as before Stage 5
    assert notices(h)[0].startswith("⚠️ Grading failed for a submission to assessment #1: The AI returned")
    assert "👉 Retry: <code>/regrade 1</code>" in notices(h)[0]

    svc = h.app.bot_data["services"]
    await work.run_once(h.app.bot, svc)
    assert len(notices(h)) == 1  # the rest wait for the interval

    let_notice_interval_pass(h.db)
    await work.run_once(h.app.bot, svc)
    assert len(notices(h)) == 2
    assert notices(h)[1].startswith("⚠️ Grading failed for 4 submissions:")
    assert "• assessment #1: 4" in notices(h)[1] and "<code>/regrade 1</code>" in notices(h)[1]

    let_notice_interval_pass(h.db)
    await work.run_once(h.app.bot, svc)
    assert len(notices(h)) == 2  # each failure is reported once

    h.ai.error = None
    await h.text(ADMIN_ID, "/regrade 1")
    assert "✅ Nothing waiting" in await status_text(h)


async def test_a_failure_fixed_by_regrade_before_the_summary_is_not_reported(harness):
    h = harness
    await published_with_answers(h, STUDENTS[:2])
    h.ai.error = AIError("bad reply")
    for user_id in STUDENTS[:2]:
        await h.press(user_id, "ans:submit:1")
    h.ai.error = None
    await h.text(ADMIN_ID, "/regrade 1")
    let_notice_interval_pass(h.db)
    await work.run_once(h.app.bot, h.app.bot_data["services"])
    assert len(notices(h)) == 1


async def test_during_an_outage_grading_failures_wait_for_the_back_message(harness):
    h = harness
    await published_with_answers(h)
    h.ai.error = DOWN
    for user_id in STUDENTS:
        await h.press(user_id, "ans:submit:1")
    texts = notices(h)
    assert len(texts) == 2  # the first failure (before it looked like an outage), then the alert
    assert texts[0].startswith("⚠️ Grading failed for a submission") and texts[1].startswith("🚨")

    let_notice_interval_pass(h.db)
    await work.run_once(h.app.bot, h.app.bot_data["services"])
    assert len(notices(h)) == 2  # held: the admins already know why grading fails

    h.ai.error = None
    await h.text(STUDENT_ID, "hello")  # any request that gets through
    back = notices(h)[-1]
    assert back.startswith("✅ The AI provider is answering again")
    assert "5 submissions are waiting to be graded." in back
    assert "Retry grading 5 submissions to #1: <code>/regrade 1</code>" in back
    let_notice_interval_pass(h.db)
    await work.run_once(h.app.bot, h.app.bot_data["services"])
    assert len(notices(h)) == 3  # the held failures were reported in the back message


def test_concurrent_notice_claims_report_each_failure_once(pg_url):
    db = Database(pg_url)
    try:
        aid = db.create_assessment("t", None, "x", None, ADMIN_ID,
                                   [{"type": "short", "question": "q", "answer": "a", "points": 1}])
        assert db.claim_grading_failure_notice(900, force=True) == []  # the notice row exists, as after
        for user_id in range(20):                                       # the first notice in production
            db.mark_submission_failed(db.create_submission(aid, user_id, "a"), "bad")
        dbs = [Database(pg_url, max_size=2) for _ in range(6)]
        barrier = threading.Barrier(len(dbs))
        claimed = []

        def claim(other):
            barrier.wait()
            claimed.append(other.claim_grading_failure_notice(900, force=True))

        threads = [threading.Thread(target=claim, args=(other,)) for other in dbs]
        [t.start() for t in threads]
        [t.join() for t in threads]
        ids = [row["id"] for rows in claimed for row in rows]
        assert sorted(ids) == list(range(1, 21))  # every failure, none twice
        for other in dbs:
            other.close()
    finally:
        db.close()


# ── the real provider check ──────────────────────────────────────────────
class FakeProviderHTTP:
    """A local HTTP server answering every request with one status and body."""

    def __init__(self, status=200, body=b'{"object": "list", "data": []}'):
        self.status, self.body, self.paths = status, body, []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        self.paths.append(request.split()[1].decode())
        writer.write(b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n%s" % (self.status, len(self.body), self.body))
        await writer.drain()
        writer.close()


@pytest.fixture
def local_network(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_the_provider_check_lists_models_and_reports_failures(kind, local_network):
    def provider(url):
        if kind == "anthropic":
            return AnthropicProvider("k", "claude-test", 5, base_url=url)
        return OpenAICompatibleProvider("custom", "k", "m", 5, base_url=url + "/v1")

    ok = b'{"data": [], "has_more": false, "first_id": null, "last_id": null, "object": "list"}'
    async with FakeProviderHTTP(200, ok) as server:
        assert await provider(server.url).check() == "reachable"
        assert server.paths[0].startswith("/v1/models")  # no completion, so no tokens spent
    async with FakeProviderHTTP(401, b'{"error": {"message": "bad key"}}') as server:
        with pytest.raises(AIUnavailable, match="rejected the API key"):
            await provider(server.url).check()
    async with FakeProviderHTTP(404, b'{"error": {"message": "no such route"}}') as server:
        assert await provider(server.url).check() == "reachable (it has no model list)"
    async with FakeProviderHTTP(503, b'{"error": {"message": "overloaded"}}') as server:
        with pytest.raises(AIUnavailable, match="503"):
            await provider(server.url).check()
        assert len(server.paths) == 1  # not retried: /status must answer quickly

    closed = f"http://127.0.0.1:{free_port()}"
    with pytest.raises(AIUnavailable, match="Could not reach"):
        await provider(closed).check()
    with pytest.raises(AIUnavailable, match="Could not reach"):  # real requests count as outages too
        await provider(closed).complete("system", "user")


# ── structured logs ──────────────────────────────────────────────────────
def events(caplog, name):
    return [r for r in caplog.records if getattr(r, "event", None) == name]


async def test_sending_and_grading_write_events(harness, caplog):
    caplog.set_level(logging.DEBUG)
    h = harness
    await published_with_answers(h, [STUDENT_ID])
    await h.press(STUDENT_ID, "ans:submit:1")

    sent = [r.fields for r in events(caplog, "delivery.sent")]  # the admin started the bot too
    assert sent == [{"assessment": 1, "user": u, "attempt": 1} for u in sorted([ADMIN_ID, STUDENT_ID])]
    (finished,) = events(caplog, "delivery.finished")
    assert finished.fields == {"assessment": 1, "sent": 2, "blocked": 0, "failed": 0, "cancelled": 0}
    (started,) = events(caplog, "grading.started")
    assert started.fields == {"submission": 1, "assessment": 1, "user": STUDENT_ID, "attempt": 1}
    (graded,) = events(caplog, "grading.finished")
    assert graded.fields["score"] == 4 and graded.fields["max"] == 4 and graded.fields["seconds"] >= 0
    assert graded.getMessage().startswith("grading.finished submission=1 assessment=1 ")
    assert events(caplog, "result.sent")[0].fields == {"submission": 1, "user": STUDENT_ID}
    assert {"generate", "chat", "grade"} <= {r.fields["kind"] for r in events(caplog, "ai.request")}
    assert "water" not in "\n".join(r.getMessage() for r in caplog.records if getattr(r, "event", None))


async def test_failures_are_logged_as_events(harness, caplog):
    caplog.set_level(logging.INFO)
    h = harness
    h.tg.fail = lambda m, p: (403, "Forbidden: bot was blocked by the user") \
        if m == "sendMessage" and int(p["chat_id"]) == STUDENT_ID and "Which organelle" in p.get("text", "") else None
    await published_with_answers(h, [STUDENT_ID, STUDENTS[0]])
    assert events(caplog, "delivery.blocked")[0].fields["user"] == STUDENT_ID
    h.ai.error = DOWN
    await h.press(STUDENTS[0], "ans:submit:1")
    (failed,) = events(caplog, "grading.failed")
    assert failed.levelno == logging.ERROR and "Could not reach" in failed.fields["error"]
    assert events(caplog, "ai.unavailable")[0].fields["kind"] == "grade"


def test_json_lines_carry_the_event_fields():
    formatter = JsonFormatter()
    logger = logging.getLogger("bot.test")
    captured = []

    class Keep(logging.Handler):
        def emit(self, record):
            captured.append(record)

    handler = Keep()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        event(logger, "grading.failed", logging.WARNING, submission=3, error='AI said "no"', level="x", skip=None)
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("plain line")
    finally:
        logger.removeHandler(handler)

    line = json.loads(formatter.format(captured[0]))
    assert line["level"] == "warn" and line["event"] == "grading.failed" and line["submission"] == 3
    assert line["field_level"] == "x" and "skip" not in line  # never overwrites the standard keys
    assert line["message"] == 'grading.failed submission=3 error="AI said \\"no\\"" level=x'
    plain = json.loads(formatter.format(captured[1]))
    assert plain["message"] == "plain line" and "ValueError: boom" in plain["exception"] and "event" not in plain


def test_main_py_logs_json_lines_to_stdout(pg_url):
    port = free_port()
    with ThreadedFakeBotAPI() as api:
        env = {**bot_env(pg_url, api, port), "LOG_FORMAT": "json"}
        proc = subprocess.Popen([sys.executable, "main.py"], cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 60
            while health(port) != 200:
                assert proc.poll() is None, proc.stderr.read()
                assert time.monotonic() < deadline, "never became healthy"
                time.sleep(0.2)
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=30) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
        stdout, stderr = proc.stdout.read(), proc.stderr.read()
    lines = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    assert any(line["message"] == "Receiving updates by polling" and line["level"] == "info" for line in lines)
    assert all({"time", "level", "logger", "message"} <= line.keys() for line in lines)
    assert "Stopped cleanly" in stdout and "Receiving updates" not in stderr


def test_log_format_is_checked(monkeypatch):
    from bot.config import ConfigError, load_settings
    for name, value in {"TELEGRAM_BOT_TOKEN": "1:x", "AI_API_KEY": "k", "LOG_FORMAT": "xml"}.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match="LOG_FORMAT must be one of text, json"):
        load_settings(env_file=os.devnull)
    monkeypatch.setenv("LOG_FORMAT", "JSON")
    assert load_settings(env_file=os.devnull).log_format == "json"
