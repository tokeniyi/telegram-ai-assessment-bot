"""A small HTTP server for deploys: GET /health, and the Telegram webhook when one is used.

Railway calls the health check while a new deployment starts and switches over only once it
answers 2xx, so /health answers 200 only once the bot is fetching updates and can reach the
database, and 503 again as soon as it starts shutting down.

With WEBHOOK_URL set, Telegram POSTs each update to /telegram. Railway sends that traffic to one
deployment at a time, so two bot versions never receive the same update, unlike polling. The
X-Telegram-Bot-Api-Secret-Token header proves the request comes from Telegram.

Plain asyncio, no framework: the bot needs two routes, and python-telegram-bot's own webhook
server (tornado) cannot serve a health check next to the webhook.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/telegram"
MAX_BODY = 1_000_000          # Telegram updates are far smaller
MAX_HEADERS = 100
READ_TIMEOUT = 10             # seconds to receive one request


class HealthState:
    """What /health reports. `check_database` is called on each health request."""

    def __init__(self, check_database: Callable[[], Awaitable[bool]]):
        self.ready = False        # fetching updates
        self.stopping = False     # shutting down: never healthy again
        self._check_database = check_database

    async def healthy(self) -> tuple[bool, str]:
        if self.stopping:
            return False, "stopping"
        if not self.ready:
            return False, "starting"
        try:
            if not await self._check_database():
                return False, "database unreachable"
        except Exception:
            log.warning("Health check could not reach the database", exc_info=True)
            return False, "database unreachable"
        return True, "ok"


class WebServer:
    """Serves /health and, if `on_update` is given, the webhook."""

    def __init__(self, port: int, health: HealthState, host: str = "0.0.0.0",
                 on_update: Callable[[dict], Awaitable[bool]] | None = None, secret: str = ""):
        """`on_update` queues an update for handling; it returns False if the bot is shutting
        down and can no longer take it, and the request then fails so that Telegram retries."""
        self.port, self.host = port, host
        self.health = health
        self.on_update = on_update
        self.secret = secret
        self.accepting_updates = on_update is not None
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]  # the real one when 0 was asked for
        log.info("Listening on port %s for /health%s", self.port,
                 f" and the webhook ({WEBHOOK_PATH})" if self.on_update else "")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            status, body, head_only = await asyncio.wait_for(self._respond(reader), READ_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            status, body, head_only = HTTPStatus.BAD_REQUEST, {"error": "bad request"}, False
        except Exception:
            log.exception("Error while answering an HTTP request")
            status, body, head_only = HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"}, False
        payload = json.dumps(body).encode()
        try:
            writer.write(
                f"HTTP/1.1 {status.value} {status.phrase}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + (b"" if head_only else payload)
            )
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def _respond(self, reader: asyncio.StreamReader) -> tuple[HTTPStatus, dict, bool]:
        request: dict[str, str] = {}
        status, body = await self._route(reader, request)
        return status, body, request.get("method") == "HEAD"

    async def _route(self, reader: asyncio.StreamReader, request: dict) -> tuple[HTTPStatus, dict]:
        request_line = (await reader.readline()).decode("latin-1").strip()
        method, target, _ = request_line.split(" ", 2)
        request["method"] = method
        headers: dict[str, str] = {}
        while (line := (await reader.readline()).decode("latin-1").strip()):
            if len(headers) >= MAX_HEADERS:
                raise ValueError("too many headers")
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        path = target.split("?", 1)[0]

        if path == "/health" and method in {"GET", "HEAD"}:
            ok, reason = await self.health.healthy()
            return (HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE), {"status": reason}

        if path == WEBHOOK_PATH and method == "POST" and self.on_update is not None:
            given = headers.get("x-telegram-bot-api-secret-token", "")
            if not hmac.compare_digest(given.encode(), self.secret.encode()):
                return HTTPStatus.FORBIDDEN, {"error": "wrong secret"}
            if not self.accepting_updates:
                # Shutting down: Telegram retries a failed delivery, and by then Railway sends it
                # to the new deployment.
                return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "stopping"}
            length = int(headers.get("content-length", "0"))
            if not 0 < length <= MAX_BODY:
                return HTTPStatus.BAD_REQUEST, {"error": "bad length"}
            update = json.loads(await reader.readexactly(length))
            if not isinstance(update, dict) or "update_id" not in update:
                return HTTPStatus.BAD_REQUEST, {"error": "not an update"}
            try:
                taken = await self.on_update(update)  # queued for handling: answer at once
            except Exception:
                # Telegram would resend it forever; a malformed update is dropped instead.
                log.exception("Dropped a webhook update that could not be read: %s", update.get("update_id"))
                return HTTPStatus.OK, {"ok": False}
            if not taken:  # shutdown began while the body was arriving
                return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "stopping"}
            return HTTPStatus.OK, {"ok": True}

        return HTTPStatus.NOT_FOUND, {"error": "not found"}
