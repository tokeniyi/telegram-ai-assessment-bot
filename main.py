"""Entry point:  python main.py"""

import logging
import signal
import sys

from bot.app import build_application
from bot.runtime import run
from bot.config import ConfigError, load_settings


def main() -> None:
    # In a container this process is PID 1, which ignores SIGTERM unless it has a handler; until
    # bot.runtime installs its own (once start-up is under way), a stop request simply exits.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        settings = load_settings()
    except ConfigError as exc:
        sys.exit(str(exc))

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)

    # Polls Telegram, or receives a webhook if WEBHOOK_URL is set; serves /health on PORT.
    # Stops cleanly on SIGTERM (see bot/runtime.py).
    run(build_application(settings))


if __name__ == "__main__":
    main()
