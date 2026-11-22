"""Logging set-up and structured log events.

`event(log, "grading.finished", submission=12, seconds=4.2)` writes one line that a person can
read and a machine can filter:

    text (default):  ... INFO bot.handlers.work: grading.finished submission=12 seconds=4.2
    LOG_FORMAT=json: {"time": "...", "level": "info", "logger": "bot.handlers.work",
                      "message": "grading.finished submission=12 seconds=4.2",
                      "event": "grading.finished", "submission": 12, "seconds": 4.2}

Railway reads JSON lines, colours them by `level` and makes every other field a filterable
attribute (`@event:grading.failed`, `@assessment:3`). Logs go to stdout: Railway shows plain
lines written to stderr as errors.

Events never carry answer texts, document contents or keys: only ids, counts, timings and error
messages.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

LOG_FORMATS = ("text", "json")
TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_LEVELS = {"WARNING": "warn", "CRITICAL": "error"}
_RESERVED = {"time", "level", "logger", "message"}


def _value(value: Any) -> str:
    """A field value in the text form: quoted only when it has spaces or quotes."""
    if isinstance(value, float):
        value = round(value, 3)
    text = str(value)
    if not text or any(c in text for c in ' "=\n'):
        return json.dumps(text)
    return text


def event(logger: logging.Logger, name: str, level: int = logging.INFO, /, **fields: Any) -> None:
    """Log `name` followed by its fields; fields that are None are left out."""
    if not logger.isEnabledFor(level):
        return
    fields = {k: v for k, v in fields.items() if v is not None}
    message = " ".join([name, *(f"{k}={_value(v)}" for k, v in fields.items())])
    logger.log(level, message, extra={"event": name, "fields": fields}, stacklevel=2)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the event fields as top-level attributes."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": _LEVELS.get(record.levelname, record.levelname.lower()),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if getattr(record, "event", None):
            entry["event"] = record.event
            for key, value in record.fields.items():
                entry[f"field_{key}" if key in _RESERVED else key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def setup_logging(level: str, log_format: str = "text") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if log_format == "json" else logging.Formatter(TEXT_FORMAT))
    logging.basicConfig(level=getattr(logging, level, logging.INFO), handlers=[handler], force=True)
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
