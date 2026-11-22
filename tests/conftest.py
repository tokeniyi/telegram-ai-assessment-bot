"""Offline test doubles: a fake Telegram API and a fake AI provider."""

import asyncio
import json
import os
import re
import itertools
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import psycopg
import pytest
from psycopg import sql
from telegram import Update
from telegram.ext import ApplicationBuilder
from telegram.request import BaseRequest

from bot.app import build_application
from bot.config import Settings

ADMIN_ID, STUDENT_ID = 1001, 2002

GENERATED = {
    "title": "Cell Biology Basics",
    "questions": [
        {"type": "mcq", "question": "Which organelle produces most of the cell's ATP?",
         "options": ["A) Nucleus", "Mitochondria", "Ribosome", "Golgi apparatus"],
         "answer": "B) Mitochondria", "points": 1},
        {"type": "short", "question": "Explain what osmosis is.",
         "answer": "Movement of water across a semi-permeable membrane from low to high solute concentration.",
         "points": 3},
    ],
}
GRADED = {
    "results": [
        {"number": 1, "student_answer": "B", "awarded": 1, "feedback": "Correct."},
        {"number": 2, "student_answer": "water moving", "awarded": 99, "feedback": "Mention the membrane."},
    ],
    "overall_feedback": "Good start <keep going>.",
}


def fake_chat_decision(user):
    """A stand-in for the chat model: numbered text is an answer, 'done'/'review' asks for review,
    anything else is conversation. Mirrors what the real prompt asks the model to do."""
    message = user.split("<message>\n", 1)[1].rsplit("\n</message>", 1)[0]
    open_ = "Open assessment:" in user
    if open_ and re.match(r"^\s*\d+\s*[.)]", message):
        return {"intent": "answer", "reply": ""}
    if open_ and re.search(r"\b(i'm done|review my answers|grade me)\b", message.lower()):
        return {"intent": "review", "reply": ""}
    return {"intent": "chat", "reply": f"Chat reply to: {message} <hi>"}


class FakeProvider:
    name, model = "fake", "fake-model"

    def __init__(self):
        self.calls = []
        self.replies = []  # queued raw replies; falls back to the canned JSON
        self.error = None
        self.check_error = None  # raised by check(), the /status probe
        self.check_delay = 0.0
        self.checks = 0

    async def check(self):
        self.checks += 1
        await asyncio.sleep(self.check_delay)
        if self.check_error:
            raise self.check_error
        return "reachable"

    async def complete(self, system, user):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        if self.replies:
            return self.replies.pop(0)
        if system.startswith("You are the chat assistant"):
            return json.dumps(fake_chat_decision(user))
        if system.startswith("You are a friendly study companion"):
            return json.dumps({"message": "How is your revision going? <b>Reply</b> if you're stuck."})
        if system.startswith("You write"):
            return json.dumps(GENERATED)
        if system.startswith("You are a patient tutor"):
            return json.dumps({"explanation": "It means water moves through a membrane <slowly>."})
        if system.startswith("You repair text"):
            return json.dumps({"text": user.split("<ocr>\n", 1)[1].rsplit("\n</ocr>", 1)[0]})  # unchanged
        return json.dumps(GRADED)


class FakeVisionProvider(FakeProvider):
    """A provider that also accepts images; replies with whatever the test queued."""

    def __init__(self):
        super().__init__()
        self.vision_calls = []
        self.vision_replies = []
        self.vision_error = None

    async def complete_vision(self, system, user, images, model=None):
        self.vision_calls.append((system, user, images, model))
        if self.vision_error:
            raise self.vision_error
        return self.vision_replies.pop(0)


class Crash(BaseException):
    """Stands for the bot process dying mid-work: not an Exception, so no handler catches it."""


class FakeTelegram(BaseRequest):
    """Records every Bot API call and answers with a minimal valid response."""

    def __init__(self):
        self.calls = []
        self.files = {}
        self._message_id = 5000
        # fail(method, params) -> None to answer normally, or (http_status, description) for a
        # Bot API error; it may also raise, e.g. Crash to stand for the process dying.
        self.fail = None

    @property
    def read_timeout(self):
        return 1.0

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, *args, **kwargs):
        if "/file/" in url:
            return 200, self.files[url.rsplit("/", 1)[1]]
        api_method = url.rsplit("/", 1)[1]
        params = dict(request_data.parameters) if request_data else {}
        if self.fail:
            error = self.fail(api_method, params)
            if error:
                status, description = error
                return status, json.dumps({"ok": False, "error_code": status,
                                           "description": description}).encode()
        self.calls.append((api_method, params))
        if api_method == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "Bot", "username": "test_bot"}
        elif api_method == "getFile":
            result = {"file_id": params["file_id"], "file_unique_id": "u", "file_path": params["file_id"]}
        elif api_method in {"sendMessage", "editMessageText", "editMessageReplyMarkup", "sendDocument"}:
            self._message_id += 1
            result = {"message_id": self._message_id, "date": int(time.time()),
                      "chat": {"id": int(params.get("chat_id", 0)), "type": "private"}, "text": "ok"}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    # helpers for assertions
    def texts(self, chat_id=None, method="sendMessage"):
        return [p.get("text", "") for m, p in self.calls
                if m == method and (chat_id is None or int(p["chat_id"]) == chat_id)]

    def last_keyboard(self, chat_id):
        for m, p in reversed(self.calls):
            if m == "sendMessage" and int(p["chat_id"]) == chat_id and p.get("reply_markup"):
                markup = p["reply_markup"]
                markup = json.loads(markup) if isinstance(markup, str) else markup
                return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        return []


def make_settings(database_url: str, **overrides) -> Settings:
    values = dict(
        telegram_bot_token="123:TEST", admin_ids=frozenset({ADMIN_ID}), ai_provider="openai",
        ai_api_key="k", ai_model="m", ai_base_url=None, ai_timeout_seconds=5,
        default_num_questions=5, max_questions=30, max_file_mb=20, max_document_chars=120_000,
        show_correct_answers=True, allow_resubmission=False, database_url=database_url,
        log_level="INFO",
    )
    values.update(overrides)
    return Settings(**values)


class Harness:
    def __init__(self, application, telegram, provider, update_ids=None):
        self.app, self.tg, self.ai = application, telegram, provider
        self.db = application.bot_data["services"].db
        # Telegram numbers a bot's updates in one sequence, whichever process receives them, so
        # harnesses standing for a restarted bot share the counter.
        self._update_ids = update_ids or itertools.count(1)
        self._update_id = 0
        self.last_update = None

    def _user(self, user_id):
        return {"id": user_id, "is_bot": False, "first_name": f"User{user_id}", "username": f"u{user_id}"}

    async def _process(self, payload):
        self._update_id = next(self._update_ids)
        payload["update_id"] = self._update_id
        self.last_update = payload
        await self.deliver(payload)

    async def deliver(self, payload):
        """Hand the bot an update exactly as given, e.g. a copy Telegram sends again."""
        await self.app.process_update(Update.de_json(json.loads(json.dumps(payload)), self.app.bot))

    def _message(self, user_id, **fields):
        return {"message_id": self._update_id + 1, "date": int(time.time()),
                "chat": {"id": user_id, "type": "private"}, "from": self._user(user_id), **fields}

    async def text(self, user_id, text):
        fields = {"text": text}
        if text.startswith("/"):
            fields["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self._process({"message": self._message(user_id, **fields)})

    async def document(self, user_id, name, data, caption=None):
        self.tg.files[name] = data
        doc = {"file_id": name, "file_unique_id": "u", "file_name": name, "file_size": len(data)}
        fields = {"document": doc}
        if caption:
            fields["caption"] = caption
        await self._process({"message": self._message(user_id, **fields)})

    async def photo(self, user_id, data, caption=None, album=None, reply_to=None):
        """A photo message; `data` is the image file. `album` is a media group id."""
        self._photos = getattr(self, "_photos", 0) + 1
        file_id = f"photo{self._photos}"
        self.tg.files[file_id] = data
        sizes = [{"file_id": "thumb", "file_unique_id": "t", "width": 90, "height": 60, "file_size": 10},
                 {"file_id": file_id, "file_unique_id": file_id, "width": 1280, "height": 720,
                  "file_size": len(data)}]
        fields = {"photo": sizes}
        if caption:
            fields["caption"] = caption
            if caption.startswith("/"):
                fields["caption_entities"] = [{"type": "bot_command", "offset": 0, "length": len(caption.split()[0])}]
        if album:
            fields["media_group_id"] = album
        message = self._message(user_id, **fields)
        await self._process({"message": message})
        return message

    async def reply(self, user_id, text, to):
        """A text message (usually a command) sent as a reply to an earlier message."""
        fields = {"text": text, "reply_to_message": to}
        if text.startswith("/"):
            fields["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self._process({"message": self._message(user_id, **fields)})

    async def press(self, user_id, data):
        await self._process({"callback_query": {
            "id": "cb", "from": self._user(user_id), "chat_instance": "ci", "data": data,
            "message": self._message(user_id, text="x"),
        }})


TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/bot_test"
)


@pytest.fixture
def pg_url():
    """A PostgreSQL URL bound to a fresh, private schema that is dropped afterwards."""
    schema = "t_" + uuid.uuid4().hex[:12]
    try:
        admin = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.fail(f"PostgreSQL is required for the tests; set TEST_DATABASE_URL ({exc})")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    sep = "&" if "?" in TEST_DATABASE_URL else "?"
    yield f"{TEST_DATABASE_URL}{sep}options={quote('-c search_path=' + schema)}"
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    admin.close()


@pytest.fixture
async def make_harness(pg_url):
    created = []
    update_ids = itertools.count(1)

    async def factory(**overrides):
        telegram = FakeTelegram()
        builder = ApplicationBuilder().request(telegram).get_updates_request(FakeTelegram())
        application = build_application(make_settings(pg_url, **overrides), builder)
        provider = FakeProvider()
        application.bot_data["services"].ai.provider = provider
        await application.initialize()
        created.append(application)
        return Harness(application, telegram, provider, update_ids)

    yield factory
    for application in created:
        await application.shutdown()
        application.bot_data["services"].db.close()


@pytest.fixture
async def harness(make_harness):
    return await make_harness()


@pytest.fixture(autouse=True)
def _database_url_env(monkeypatch):
    """Config tests build Settings from the environment, which must name a database."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/x")
