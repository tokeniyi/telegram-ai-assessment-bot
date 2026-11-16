"""Offline test doubles: a fake Telegram API and a fake AI provider."""

import json
import time
from pathlib import Path

import pytest
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


class FakeProvider:
    name, model = "fake", "fake-model"

    def __init__(self):
        self.calls = []
        self.replies = []  # queued raw replies; falls back to the canned JSON
        self.error = None

    async def complete(self, system, user):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        if self.replies:
            return self.replies.pop(0)
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


class FakeTelegram(BaseRequest):
    """Records every Bot API call and answers with a minimal valid response."""

    def __init__(self):
        self.calls = []
        self.files = {}
        self._message_id = 5000

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


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        telegram_bot_token="123:TEST", admin_ids=frozenset({ADMIN_ID}), ai_provider="openai",
        ai_api_key="k", ai_model="m", ai_base_url=None, ai_timeout_seconds=5,
        default_num_questions=5, max_questions=30, max_file_mb=20, max_document_chars=120_000,
        show_correct_answers=True, allow_resubmission=False, database_path=tmp_path / "bot.db",
        log_level="INFO",
    )
    values.update(overrides)
    return Settings(**values)


class Harness:
    def __init__(self, application, telegram, provider):
        self.app, self.tg, self.ai = application, telegram, provider
        self.db = application.bot_data["services"].db
        self._update_id = 0

    def _user(self, user_id):
        return {"id": user_id, "is_bot": False, "first_name": f"User{user_id}", "username": f"u{user_id}"}

    async def _process(self, payload):
        self._update_id += 1
        payload["update_id"] = self._update_id
        await self.app.process_update(Update.de_json(payload, self.app.bot))

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


@pytest.fixture
async def make_harness(tmp_path):
    created = []

    async def factory(**overrides):
        telegram = FakeTelegram()
        builder = ApplicationBuilder().request(telegram).get_updates_request(FakeTelegram())
        application = build_application(make_settings(tmp_path, **overrides), builder)
        provider = FakeProvider()
        application.bot_data["services"].ai.provider = provider
        await application.initialize()
        created.append(application)
        return Harness(application, telegram, provider)

    yield factory
    for application in created:
        await application.shutdown()
        application.bot_data["services"].db.close()


@pytest.fixture
async def harness(make_harness):
    return await make_harness()
