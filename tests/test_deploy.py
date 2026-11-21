"""Stage 4: deploys. The health check, a clean shutdown, and the optional webhook.

These tests run the bot the way it runs in production (bot.runtime.BotRunner over real HTTP
against a fake Bot API, see tests/fake_bot_api.py), and one runs `python main.py` as a real
process and stops it with SIGTERM, as Railway does.
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bot.app import build_application
from bot.db import Database
from bot.handlers import work
from bot.runtime import BotRunner
from bot.web import WEBHOOK_PATH, HealthState, WebServer
from tests.conftest import ADMIN_ID, STUDENT_ID, FakeProvider, make_settings
from tests.fake_bot_api import FakeBotAPI, ThreadedFakeBotAPI, button_update, message_update

ROOT = Path(__file__).resolve().parent.parent
MATERIAL = ("Mitochondria produce most of the cell's ATP. Osmosis is the movement of water "
            "across a semi-permeable membrane. " * 5)


@pytest.fixture(autouse=True)
def _no_proxy_for_localhost(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


async def http(port, method="GET", path="/health", body=b"", headers=None):
    """One raw HTTP request to the bot's server. Returns (status, json body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n"
    for name, value in (headers or {}).items():
        head += f"{name}: {value}\r\n"
    writer.write(head.encode() + b"\r\n" + body)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    status_line, _, rest = raw.partition(b"\r\n")
    return int(status_line.split()[1]), json.loads(rest.split(b"\r\n\r\n", 1)[1])


def fresh_db(pg_url):
    """A new connection: a stopped bot has closed its own."""
    return Database(pg_url)


async def until(condition, timeout=10.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(step)
    raise AssertionError("condition not reached in time")


@pytest.fixture
async def api():
    fake = FakeBotAPI()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def start_bot(pg_url, api):
    """Start a BotRunner against the fake API; returns (runner, task)."""
    running = []

    async def factory(**overrides):
        overrides.setdefault("shutdown_grace_seconds", 5)  # a failing test must not hang
        settings = make_settings(pg_url, telegram_api_url=api.url, port=0, **overrides)
        application = build_application(settings)
        svc = application.bot_data["services"]
        svc.ai.provider = FakeProvider()
        svc.ocr.warm_up = lambda: None  # no OCR models in these tests
        runner = BotRunner(application)
        task = asyncio.create_task(runner.run(install_signal_handlers=False))
        running.append((runner, task))
        await until(lambda: runner.health.ready or task.done())
        if task.done():
            task.result()
        return runner, task

    yield factory
    for runner, task in running:
        if not task.done():
            runner.request_stop()
        try:
            await asyncio.wait_for(task, 15)
        except asyncio.TimeoutError:  # pragma: no cover - only when a test already failed
            task.cancel()
            pytest.fail("the bot did not stop within 15 s")


async def publish_one(api, runner, first_id=1):
    """Admin and student start the bot, the admin creates and publishes a draft."""
    for i, (user, text) in enumerate([(ADMIN_ID, "/start"), (STUDENT_ID, "/start")]):
        api.push(message_update(first_id + i, user, text))
    db = runner.svc.db
    await until(lambda: db.count_users()[0] == 2)
    aid = db.create_assessment("Cell Biology", None, MATERIAL, None, ADMIN_ID, [
        {"type": "short", "question": "Explain osmosis.", "answer": "Water moves", "points": 1}])
    api.push(button_update(first_id + 2, ADMIN_ID, f"pub:{aid}"))
    await until(lambda: any("sent to 2 users" in t for t in api.texts(ADMIN_ID)))
    return aid


# ── the health check ─────────────────────────────────────────────────────
async def test_health_is_only_ok_while_fetching_updates_and_the_database_answers():
    db_ok = {"value": True}

    async def check():
        if db_ok["value"] is None:
            raise OSError("connection refused")
        return db_ok["value"]

    health = HealthState(check)
    server = WebServer(0, health, host="127.0.0.1")
    await server.start()
    try:
        assert await http(server.port) == (503, {"status": "starting"})
        health.ready = True
        assert await http(server.port) == (200, {"status": "ok"})
        db_ok["value"] = None
        assert await http(server.port) == (503, {"status": "database unreachable"})
        db_ok["value"] = True
        health.stopping = True
        assert await http(server.port) == (503, {"status": "stopping"})
        assert (await http(server.port, path="/nope"))[0] == 404
        assert (await http(server.port, method="POST", path=WEBHOOK_PATH))[0] == 404  # polling: no webhook
    finally:
        await server.stop()


async def test_a_running_bot_reports_healthy_and_answers_users(start_bot, api):
    runner, _ = await start_bot()
    assert await http(runner.web.port) == (200, {"status": "ok"})
    assert "getUpdates" in api.methods() and "setWebhook" not in api.methods()
    api.push(message_update(1, STUDENT_ID, "/myid"))
    await until(lambda: any("Your Telegram ID" in t for t in api.texts(STUDENT_ID)))


async def test_health_fails_when_the_database_is_down(start_bot, monkeypatch):
    runner, _ = await start_bot()

    def down():
        raise OSError("database unreachable")

    monkeypatch.setattr(runner.svc.db, "ping", down)
    assert await http(runner.web.port) == (503, {"status": "database unreachable"})


# ── a clean shutdown ─────────────────────────────────────────────────────
async def test_stop_confirms_fetched_updates_so_the_next_process_is_not_sent_them(start_bot, api):
    runner, task = await start_bot()
    api.push(message_update(7, STUDENT_ID, "/myid"))
    await until(lambda: api.texts(STUDENT_ID))
    runner.request_stop()
    await asyncio.wait_for(task, 10)
    assert api.offsets[-1] == 8  # the last call told Telegram that update 7 was received


async def test_grading_in_progress_finishes_before_the_process_exits(start_bot, api, pg_url):
    """SIGTERM arrives while a submission is being graded: no new updates are taken, the grading
    finishes, its result is sent, then the process exits."""
    runner, task = await start_bot()
    aid = await publish_one(api, runner)
    release = asyncio.Event()
    provider = runner.svc.ai.provider
    complete = provider.complete

    async def slow_grading(system, user):
        if not system.startswith("You are the chat assistant"):
            await release.wait()
        return await complete(system, user)

    provider.complete = slow_grading
    api.push(message_update(10, STUDENT_ID, "1. Water moves through a membrane"))
    await until(lambda: any("Saved as your answer" in t for t in api.texts(STUDENT_ID)))
    api.push(button_update(11, STUDENT_ID, f"ans:submit:{aid}"))
    await until(lambda: any(m == "editMessageText" for m in api.methods()))  # "Grading is in progress…"

    runner.request_stop()
    assert await http(runner.web.port) == (503, {"status": "stopping"})
    await until(lambda: not runner.app.updater.running)
    await asyncio.sleep(0.2)  # the poller's last call, which confirms what it fetched
    assert api.offsets[-1] == 12
    polls = len(api.offsets)
    api.push(message_update(12, STUDENT_ID, "/myid"))  # arrives after SIGTERM: left for the next process
    await asyncio.sleep(0.3)
    assert not task.done()  # still waiting for the grading
    release.set()
    await asyncio.wait_for(task, 10)

    db = fresh_db(pg_url)
    submission = db.get_submission(1)
    db.close()
    assert submission["status"] == "graded" and submission["result_sent_at"] is not None
    assert any("Result" in t for t in api.texts(STUDENT_ID))
    assert not any("Your Telegram ID" in t for t in api.texts(STUDENT_ID))
    assert len(api.offsets) == polls  # no getUpdates after the poller stopped


async def test_shutdown_does_not_wait_past_the_grace_period(start_bot, api, pg_url):
    """Work that would outlast SHUTDOWN_GRACE_SECONDS is left to the next process; its lease
    lets that process take it over (Stage 3)."""
    runner, task = await start_bot(shutdown_grace_seconds=1)
    aid = await publish_one(api, runner)

    async def hang(system, user):
        await asyncio.Event().wait()

    api.push(message_update(10, STUDENT_ID, "1. Water"))
    await until(lambda: any("Saved as your answer" in t for t in api.texts(STUDENT_ID)))
    runner.svc.ai.provider.complete = hang
    api.push(button_update(11, STUDENT_ID, f"ans:submit:{aid}"))
    await until(lambda: runner.svc.db.get_submission(1) is not None)
    started = time.monotonic()
    runner.request_stop()
    await asyncio.wait_for(task, 10)
    assert time.monotonic() - started < 5
    db = fresh_db(pg_url)
    submission = db.get_submission(1)
    db.close()
    assert submission["status"] == "pending" and submission["grading_claimed_until"] is not None


async def test_sending_stops_after_the_message_in_hand_and_the_rest_is_left_queued(start_bot, api, pg_url):
    """A shutdown during a broadcast finishes the message being sent (so it is marked sent and
    never repeated) and leaves the others unclaimed for the next process."""
    runner, task = await start_bot()
    db = runner.svc.db
    api.push(message_update(1, ADMIN_ID, "/start"))
    await until(lambda: db.count_users()[0] == 1)
    for user_id in range(3001, 3006):
        db.upsert_user(user_id, None, None)
    aid = db.create_assessment("Long", None, MATERIAL, None, ADMIN_ID, [
        {"type": "short", "question": "Q?", "answer": "A", "points": 1}])
    api.gate = asyncio.Event()  # every sendMessage hangs until released
    api.push(button_update(2, ADMIN_ID, f"pub:{aid}"))  # the admin taps Send, then a deploy starts
    await until(lambda: db._one("SELECT COUNT(*) AS n FROM deliveries WHERE claimed_until IS NOT NULL")["n"] == 1)

    runner.request_stop()
    stopping = asyncio.create_task(asyncio.wait_for(task, 10))
    await asyncio.sleep(0.2)
    api.gate.set()
    await stopping
    db = fresh_db(pg_url)
    rows = db._all("SELECT status, claimed_until FROM deliveries ORDER BY user_id")
    db.close()
    assert [r["status"] for r in rows].count("sent") == 1
    assert all(r["claimed_until"] is None for r in rows)  # nothing left leased: the next process sends at once
    assert [r["status"] for r in rows].count("pending") == len(rows) - 1
    # the publisher was not told "sent to 1 user": the report waits for the next process
    assert not any("sent to" in t for t in api.texts(ADMIN_ID))


async def test_background_sending_finishes_the_message_in_hand(pg_url, start_bot, api):
    """Same, for the background loop (which resumes sending after a restart): stopping lets it
    finish the message it is sending instead of cutting it off, which would leave a leased row
    that the next process sends again."""
    setup = fresh_db(pg_url)
    for user_id in (3001, 3002, 3003):
        setup.upsert_user(user_id, None, None)
    aid = setup.create_assessment("Left over", None, MATERIAL, None, ADMIN_ID, [
        {"type": "short", "question": "Q?", "answer": "A", "points": 1}])
    setup.publish_assessment(aid, ADMIN_ID)  # published by a process that then stopped
    setup.close()
    api.gate = asyncio.Event()
    runner, task = await start_bot()  # its background loop starts sending at once
    db = runner.svc.db
    await until(lambda: db._one("SELECT COUNT(*) AS n FROM deliveries WHERE claimed_until IS NOT NULL")["n"] == 1)

    runner.request_stop()
    stopping = asyncio.create_task(asyncio.wait_for(task, 10))
    await asyncio.sleep(0.2)
    api.gate.set()
    await stopping
    db = fresh_db(pg_url)
    rows = db._all("SELECT status, claimed_until FROM deliveries ORDER BY user_id")
    db.close()
    assert [r["status"] for r in rows] == ["sent", "pending", "pending"]
    assert all(r["claimed_until"] is None for r in rows)


async def test_a_deploy_handover_loses_nothing_and_handles_nothing_twice(start_bot, api, pg_url):
    """The whole handover with polling: the new version starts while the old one is grading,
    the old one gets SIGTERM, finishes the grading and exits; updates sent around the switch are
    each handled exactly once, by one version or the other."""
    old, old_task = await start_bot()
    aid = await publish_one(api, old)
    release = asyncio.Event()
    complete = old.svc.ai.provider.complete

    async def slow_grading(system, user):
        if not system.startswith("You are the chat assistant"):
            await release.wait()
        return await complete(system, user)

    old.svc.ai.provider.complete = slow_grading
    api.push(message_update(10, STUDENT_ID, "1. Water"))
    await until(lambda: any("Saved as your answer" in t for t in api.texts(STUDENT_ID)))
    api.push(button_update(11, STUDENT_ID, f"ans:submit:{aid}"))
    await until(lambda: any(m == "editMessageText" for m in api.methods()))

    new, new_task = await start_bot()  # Railway starts the new version; it becomes healthy
    assert await http(new.web.port) == (200, {"status": "ok"})
    api.push(message_update(12, ADMIN_ID, "/users"))  # both versions may fetch this one
    await until(lambda: any("registered users" in t for t in api.texts(ADMIN_ID)))
    old.request_stop()  # Railway stops the old version
    api.push(message_update(13, ADMIN_ID, "/assessments"))
    await until(lambda: any("Assessments" in t for t in api.texts(ADMIN_ID)))
    release.set()
    await asyncio.wait_for(old_task, 10)

    assert sum("registered users" in t for t in api.texts(ADMIN_ID)) == 1
    assert sum("<b>Assessments</b>" in t for t in api.texts(ADMIN_ID)) == 1
    results = [t for t in api.texts(STUDENT_ID) if "Result" in t]
    assert len(results) == 1  # graded by the old version before it exited, not twice
    assert await http(new.web.port) == (200, {"status": "ok"})
    db = fresh_db(pg_url)
    assert db.get_submission(1)["status"] == "graded"
    db.close()


# ── the webhook ──────────────────────────────────────────────────────────
async def test_webhook_mode_receives_updates_and_refuses_strangers(start_bot, api):
    runner, task = await start_bot(webhook_url="https://bot.example.com", webhook_secret="s3cret")
    _, params = next(c for c in api.calls if c[0] == "setWebhook")
    assert params["url"] == "https://bot.example.com/telegram" and params["secret_token"] == "s3cret"
    assert "getUpdates" not in api.methods()

    update = json.dumps(message_update(1, STUDENT_ID, "/myid")).encode()
    port = runner.web.port
    assert (await http(port, "POST", WEBHOOK_PATH, update, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}))[0] == 403
    assert (await http(port, "POST", WEBHOOK_PATH, update))[0] == 403
    assert (await http(port, "POST", WEBHOOK_PATH, b"{not json",
                       {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}))[0] == 400
    assert api.texts(STUDENT_ID) == []
    assert (await http(port, "POST", WEBHOOK_PATH, update, {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}))[0] == 200
    await until(lambda: any("Your Telegram ID" in t for t in api.texts(STUDENT_ID)))

    runner.request_stop()
    later = json.dumps(message_update(2, STUDENT_ID, "/myid")).encode()
    status, _ = await http(port, "POST", WEBHOOK_PATH, later, {"X-Telegram-Bot-Api-Secret-Token": "s3cret"})
    assert status == 503  # Telegram retries it, against the new deployment
    await asyncio.wait_for(task, 10)
    assert "deleteWebhook" not in api.methods()  # the webhook stays for the next version


def test_webhook_settings_are_checked(monkeypatch):
    from bot.config import ConfigError, load_settings
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("AI_API_KEY", "k")
    monkeypatch.setenv("WEBHOOK_URL", "http://insecure.example.com")
    with pytest.raises(ConfigError, match="https://"):
        load_settings()
    monkeypatch.setenv("WEBHOOK_URL", "https://bot.example.com/")
    monkeypatch.delenv("PORT", raising=False)
    with pytest.raises(ConfigError, match="needs PORT"):
        load_settings()
    monkeypatch.setenv("PORT", "8080")
    settings = load_settings()
    assert settings.webhook_url == "https://bot.example.com" and settings.port == 8080
    assert len(settings.webhook_secret) == 64  # derived from the token when not given
    monkeypatch.setenv("WEBHOOK_SECRET", "bad secret!")
    with pytest.raises(ConfigError, match="WEBHOOK_SECRET"):
        load_settings()


# ── the real process ─────────────────────────────────────────────────────
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def health(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
            s.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
            return int(s.recv(100).split()[1])
    except OSError:
        return None


def test_main_py_reports_healthy_and_exits_cleanly_on_sigterm(pg_url):
    port = free_port()
    with ThreadedFakeBotAPI() as api:
        env = {**os.environ, "TELEGRAM_BOT_TOKEN": "123:TEST", "AI_API_KEY": "k", "ADMIN_IDS": str(ADMIN_ID),
               "DATABASE_URL": pg_url, "TELEGRAM_API_URL": api.url, "PORT": str(port),
               "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
               "OCR_ENGINES": "rapidocr", "SHUTDOWN_GRACE_SECONDS": "20"}
        proc = subprocess.Popen([sys.executable, "main.py"], cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.monotonic() + 60
            while health(port) != 200:
                assert proc.poll() is None, proc.stdout.read()
                assert time.monotonic() < deadline, "never became healthy"
                time.sleep(0.2)
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=30) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
        output = proc.stdout.read()
    assert "Receiving updates by polling" in output
    assert "Stop requested" in output and "Stopped cleanly" in output
    assert "Traceback" not in output
