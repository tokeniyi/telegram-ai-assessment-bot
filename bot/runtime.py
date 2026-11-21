"""Running the bot: start-up, the health check, and a clean shutdown.

Replaces python-telegram-bot's run_polling so that start and stop happen in an order that suits a
deploy, where the new version starts while the old one still runs:

Start:   database and bot ready → /health says "starting" → fetching updates begins (polling, or
         the webhook is registered) → /health says "ok", and Railway may switch over.
Stop (SIGTERM, sent by Railway to the old version once the new one is healthy):
         /health says "stopping" → no new updates are fetched (polling stops; the webhook answers
         503, so Telegram retries against the new version) → updates already received finish →
         the background work finishes the item in hand → the process exits.

Everything after SIGTERM must fit in Railway's draining time (RAILWAY_DEPLOYMENT_DRAINING_SECONDS,
0 by default, after which the process is killed). SHUTDOWN_GRACE_SECONDS caps the wait for work in
progress; whatever is cut off by a kill is picked up by the next process (Stage 3 leases).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor

from telegram import Update
from telegram.error import NetworkError
from telegram.ext import Application

from .handlers import work
from .utils import Services
from .web import WEBHOOK_PATH, HealthState, WebServer

log = logging.getLogger(__name__)

INITIALIZE_ATTEMPTS = 5
PING_TIMEOUT = 3  # seconds the health check waits for the database


async def _initialize(application: Application) -> None:
    """Connect to Telegram, retrying for a little while if the network is not there yet."""
    for attempt in range(1, INITIALIZE_ATTEMPTS + 1):
        try:
            await application.initialize()
            return
        except NetworkError as exc:
            if attempt == INITIALIZE_ATTEMPTS:
                raise
            log.warning("Could not reach Telegram (%s); retrying in %s s", exc, attempt * 2)
            await asyncio.sleep(attempt * 2)


class BotRunner:
    """Runs one bot process until `request_stop` is called (on SIGTERM or SIGINT)."""

    def __init__(self, application: Application):
        self.app = application
        self.svc: Services = application.bot_data["services"]
        self.settings = self.svc.settings
        self.webhook = self.settings.webhook_url is not None
        self.health = HealthState(self._database_ok)
        self.web: WebServer | None = None
        self._stop = asyncio.Event()
        # Its own thread, so a hanging database never takes threads from OCR, and at most one
        # ping runs at a time however often /health is called.
        self._ping_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="health")
        self._ping: asyncio.Future | None = None

    async def _database_ok(self) -> bool:
        if self._ping is None or self._ping.done():
            self._ping = asyncio.get_running_loop().run_in_executor(
                self._ping_pool, self.svc.db.ping, PING_TIMEOUT)
        try:
            return await asyncio.wait_for(asyncio.shield(self._ping), PING_TIMEOUT)
        except asyncio.TimeoutError:
            return False

    def request_stop(self) -> None:
        if self._stop.is_set():
            log.info("Already shutting down")
            return
        log.info("Stop requested: finishing work in progress, taking no new updates")
        self.health.stopping = True
        if self.web is not None:
            self.web.accepting_updates = False
        self._stop.set()

    async def _put_update(self, data: dict) -> bool:
        if self._stop.is_set():  # the queue may already be closed: let Telegram resend it
            return False
        await self.app.update_queue.put(Update.de_json(data, self.app.bot))
        return True

    async def start(self) -> None:
        """Start up; returns early if a stop is requested meanwhile."""
        await _initialize(self.app)
        if self._stop.is_set():
            return
        if self.app.post_init:
            await self.app.post_init(self.app)
        if self._stop.is_set():
            return
        if self.settings.port is not None:
            self.web = WebServer(
                self.settings.port, self.health,
                on_update=self._put_update if self.webhook else None,
                secret=self.settings.webhook_secret,
            )
            self.web.accepting_updates = self.webhook and not self._stop.is_set()
            await self.web.start()
        if self._stop.is_set():
            return
        await self.app.start()
        if self.webhook:
            await self.app.bot.set_webhook(
                url=self.settings.webhook_url + WEBHOOK_PATH,
                secret_token=self.settings.webhook_secret,
                allowed_updates=Update.ALL_TYPES,
            )
            log.info("Receiving updates by webhook at %s%s", self.settings.webhook_url, WEBHOOK_PATH)
        else:
            # start_polling removes a webhook left from an earlier setup.
            await self.app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            log.info("Receiving updates by polling")
        self.health.ready = True

    async def stop(self) -> None:
        """Stop in the order that loses nothing; bounded by SHUTDOWN_GRACE_SECONDS."""
        self.request_stop()
        started = time.monotonic()
        grace = self.settings.shutdown_grace_seconds

        def left() -> float:
            return max(grace - (time.monotonic() - started), 0)

        if self.app.updater and self.app.updater.running:
            # Confirms the updates already fetched, so the next process is not sent them again.
            await self.app.updater.stop()
        self.svc.stopping.set()  # sending and grading take no new item
        if self.app.running:
            try:
                await asyncio.wait_for(self.app.stop(), left())  # handlers in progress finish
            except asyncio.TimeoutError:
                log.warning("Updates still being handled after %s s; stopping anyway", grace)
                await self._cancel_handlers()
            if self.app.post_stop:
                await work.stop(self.app, timeout=left())
                await self.app.post_stop(self.app)
        if self.web is not None:
            await self.web.stop()
        self._ping_pool.shutdown(wait=False, cancel_futures=True)
        await self.app.shutdown()
        if self.app.post_shutdown:
            await self.app.post_shutdown(self.app)
        log.info("Stopped cleanly in %.1f s", time.monotonic() - started)

    @staticmethod
    async def _cancel_handlers() -> None:
        """Cut off the handlers still running before the database and HTTP client close under
        them. What they were doing is recovered by the next process (Stage 3 leases)."""
        tasks = [t for t in asyncio.all_tasks()
                 if t.get_name().startswith("Application:") and not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self, install_signal_handlers: bool = True) -> None:
        loop = asyncio.get_running_loop()
        if install_signal_handlers:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.request_stop)
        try:
            await self.start()
            await self._stop.wait()
        finally:
            await self.stop()


def run(application: Application) -> None:
    asyncio.run(BotRunner(application).run())
