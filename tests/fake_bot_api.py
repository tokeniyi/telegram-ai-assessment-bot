"""A fake Telegram Bot API over real HTTP, for tests that run the bot as it runs in production:
the real HTTP client, real long polling, real webhook registration and, in a subprocess, real
signals. Point the bot at it with TELEGRAM_API_URL.

Only the methods the bot uses are implemented. Updates queued with `push` are handed to the next
getUpdates call; `confirmed` records the offset each getUpdates call confirmed, as Telegram does.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from urllib.parse import parse_qs


class FakeBotAPI:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.offsets: list[int] = []      # the offset of every getUpdates call
        self.delays: dict[str, float] = {}  # method -> seconds to wait before answering
        self.gate: asyncio.Event | None = None  # if set, sendMessage waits for it
        self._message_id = 100
        self._server = None
        self.port = 0
        self._new_updates = None

    # ── control from tests ───────────────────────────────────────────────
    def push(self, update: dict) -> None:
        self.updates.append(update)
        if self._new_updates is not None:
            self._new_updates.set()

    def texts(self, chat_id: int) -> list[str]:
        return [p.get("text", "") for m, p in self.calls
                if m == "sendMessage" and int(p.get("chat_id", 0)) == chat_id]

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._new_updates = asyncio.Event()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    # ── HTTP ─────────────────────────────────────────────────────────────
    async def _handle(self, reader, writer):
        try:
            while True:  # httpx keeps connections alive
                request_line = await reader.readline()
                if not request_line:
                    break
                _, target, _ = request_line.decode().split(" ", 2)
                headers = {}
                while (line := (await reader.readline()).decode().strip()):
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()
                body = await reader.readexactly(int(headers.get("content-length", "0")))
                method = target.rsplit("/", 1)[1]
                params = self._parse(headers.get("content-type", ""), body)
                result = await self._answer(method, params)
                payload = json.dumps({"ok": True, "result": result}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    @staticmethod
    def _parse(content_type: str, body: bytes) -> dict:
        if not body:
            return {}
        if content_type.startswith("application/json"):
            return json.loads(body)
        if content_type.startswith("application/x-www-form-urlencoded"):
            return {k: v[0] for k, v in parse_qs(body.decode()).items()}
        if content_type.startswith("multipart/form-data"):
            boundary = content_type.split("boundary=", 1)[1].strip('"').encode()
            params = {}
            for part in body.split(b"--" + boundary):
                head, _, value = part.partition(b"\r\n\r\n")
                if b'name="' in head:
                    name = head.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
                    params[name] = value.rstrip(b"\r\n").decode(errors="replace")
            return params
        return {}

    async def _answer(self, method: str, params: dict):
        if method in self.delays:
            await asyncio.sleep(self.delays[method])
        if method == "sendMessage" and self.gate is not None:
            await self.gate.wait()
        self.calls.append((method, params))
        if method == "getMe":
            return {"id": 42, "is_bot": True, "first_name": "Bot", "username": "fake_bot"}
        if method == "getUpdates":
            offset = int(params.get("offset", 0) or 0)
            self.offsets.append(offset)
            self.updates = [u for u in self.updates if u["update_id"] >= offset]
            if not self.updates and float(params.get("timeout", 0) or 0) > 0:
                self._new_updates.clear()
                try:
                    await asyncio.wait_for(self._new_updates.wait(), 0.3)
                except asyncio.TimeoutError:
                    pass
            return list(self.updates)
        if method in {"sendMessage", "editMessageText"}:
            self._message_id += 1
            return {"message_id": self._message_id, "date": int(time.time()),
                    "chat": {"id": int(params.get("chat_id", 0)), "type": "private"}, "text": "ok"}
        return True


def message_update(update_id: int, user_id: int, text: str) -> dict:
    entities = ([{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
                if text.startswith("/") else [])
    return {"update_id": update_id, "message": {
        "message_id": update_id, "date": int(time.time()), "text": text, "entities": entities,
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": f"User{user_id}"}}}


def button_update(update_id: int, user_id: int, data: str) -> dict:
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "chat_instance": "ci", "data": data,
        "from": {"id": user_id, "is_bot": False, "first_name": f"User{user_id}"},
        "message": {"message_id": 1, "date": int(time.time()), "text": "x",
                    "chat": {"id": user_id, "type": "private"}}}}


class ThreadedFakeBotAPI:
    """The fake API on its own thread and event loop, for a bot running in another process."""

    def __init__(self):
        self.api = FakeBotAPI()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self.api.start(), self._loop).result(5)
        return self.api

    def push(self, update: dict) -> None:
        self._loop.call_soon_threadsafe(self.api.push, update)

    def __exit__(self, *exc):
        asyncio.run_coroutine_threadsafe(self.api.stop(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
