"""
[claude] Structured logging.

Named `logging_config` rather than `logging` deliberately — a module called
`app/logging.py` reads like a shadow of the standard library at every import
site, and the confusion costs more than the shorter name saves.

Why JSON
--------
`LOG_LEVEL` has been in every `.env` template since the beginning and was
read by nothing: `Settings` did not declare it, and `extra="ignore"` swallowed
it, so setting `LOG_LEVEL=DEBUG` did precisely nothing. It is declared now,
and this is what honours it.

One line per event, as JSON, because the alternative is a service whose
failures can only be investigated by reading prose. Every route logs
`request_id`, so a user reporting "it failed at about two o'clock" maps to an
exact line.

What must never be logged
-------------------------
Question text and answer text. Both are customer data — a question can name a
client and an answer contains CRM rows — and a log is the least controlled
place either could end up. `subject`, `thread_id` and `request_id` are enough
to reconstruct what happened, and the conversation itself is already stored,
access-controlled, in the checkpointer.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from app.config import APP_ENV, settings

# Attributes present on every LogRecord. Anything outside this set was passed
# by a caller through `extra=` and is worth emitting.
_STANDARD = frozenset(
    """
    args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName
    relativeCreated stack_info thread threadName taskName
    """.split()
)

# [claude] Never emitted, whatever a caller passes. Defence against a future
# `extra={"question": ...}` added in a hurry — the rule is enforced here
# rather than trusted to every call site.
_REDACT = frozenset({"message_text", "question", "answer", "content", "token"})


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "env": APP_ENV,
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_"):
                continue

            if key in _REDACT:
                payload[key] = "[redacted]"
                continue

            payload[key] = value

        if record.exc_info:
            # The traceback goes to the log and nowhere near a response.
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None, force_json: bool = False) -> None:
    """
    Install the handler. Called once, from the API lifespan.

    Plain text in development, where a human is reading the terminal; JSON
    everywhere else, where a log shipper is. `force_json` overrides that for
    anyone who wants structured output locally.
    """

    level = (level or settings.log_level).upper()

    handler = logging.StreamHandler(sys.stdout)

    if force_json or APP_ENV != "development":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(levelname)-8s %(name)-18s %(message)s")
        )

    root = logging.getLogger()

    # Replace rather than append: uvicorn installs its own handler, and
    # leaving both attached prints every line twice.
    root.handlers[:] = [handler]
    root.setLevel(level)

    # [claude] Quieted deliberately. httpx logs a line per model call at
    # INFO, which at one line per turn drowns the application's own events.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


__all__ = ["JsonFormatter", "configure_logging"]
