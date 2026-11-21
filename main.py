"""Entry point:  python main.py"""

import signal
import sys

from bot.app import build_application
from bot.runtime import run
from bot.config import ConfigError, load_settings
from bot.logs import setup_logging


def main() -> None:
    # In a container this process is PID 1, which ignores SIGTERM unless it has a handler; until
    # bot.runtime installs its own (once start-up is under way), a stop request simply exits.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        settings = load_settings()
    except ConfigError as exc:
        sys.exit(str(exc))

    setup_logging(settings.log_level, settings.log_format)  # text or JSON lines, on stdout

    # Polls Telegram, or receives a webhook if WEBHOOK_URL is set; serves /health on PORT.
    # Stops cleanly on SIGTERM (see bot/runtime.py).
    run(build_application(settings))


if __name__ == "__main__":
    main()
