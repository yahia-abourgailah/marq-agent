"""
[claude] Structured logging, and the redaction rule.

The redaction test is the reason this file exists. `logging_config` claims in
its docstring that question and answer text never reaches a log — both are
customer data, a question can name a client and an answer contains CRM rows,
and a log is the least controlled place either could end up.

A claim like that in a docstring is a comment. Asserted here, it is a rule.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.logging_config import JsonFormatter, configure_logging


def record(**extra):
    """One LogRecord carrying the given `extra` fields."""

    made = logging.LogRecord(
        name="marq.api",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="chat_turn",
        args=(),
        exc_info=None,
    )

    for key, value in extra.items():
        setattr(made, key, value)

    return made


def formatted(**extra) -> dict:
    return json.loads(JsonFormatter().format(record(**extra)))


def test_a_log_line_is_one_json_object():
    line = JsonFormatter().format(record())
    payload = json.loads(line)

    assert "\n" not in line
    assert payload["event"] == "chat_turn"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "marq.api"
    assert payload["ts"]


def test_extra_fields_are_emitted():
    payload = formatted(request_id="abc123", subject="employee-1", route="deals")

    assert payload["request_id"] == "abc123"
    assert payload["subject"] == "employee-1"
    assert payload["route"] == "deals"


@pytest.mark.parametrize(
    "field", ["question", "answer", "content", "message_text", "token"]
)
def test_customer_data_fields_are_redacted(field):
    """
    [claude] Enforced in the formatter, not trusted to every call site.

    The realistic failure is a future `extra={"question": ...}` added while
    debugging and never removed. Nothing at the call site would catch it, so
    the rule lives where every record passes through.
    """

    secret = "How many deals does Acme Holdings have?"
    payload = formatted(**{field: secret})

    assert payload[field] == "[redacted]"
    assert "Acme" not in json.dumps(payload)


def test_a_traceback_goes_to_the_log_and_carries_its_detail():
    """
    The counterpart to the HTTP rule: the caller gets a fixed sentence, the
    log gets everything. A failure nobody can diagnose is its own outage.
    """

    try:
        raise RuntimeError("connection to 10.10.67.77 failed")
    except RuntimeError:
        import sys

        made = record()
        made.exc_info = sys.exc_info()

        payload = json.loads(JsonFormatter().format(made))

    assert "RuntimeError" in payload["exception"]
    assert "10.10.67.77" in payload["exception"]


def test_configure_logging_installs_exactly_one_handler():
    """
    Replaced rather than appended.

    uvicorn installs its own handler; leaving both attached prints every
    line twice, which is how a log doubles in size and stops being trusted.
    """

    root = logging.getLogger()
    original = root.handlers[:]
    original_level = root.level

    try:
        configure_logging(level="DEBUG")
        configure_logging(level="DEBUG")

        assert len(root.handlers) == 1
        assert root.level == logging.DEBUG
    finally:
        root.handlers[:] = original
        root.setLevel(original_level)


def test_the_configured_level_is_honoured():
    """
    LOG_LEVEL was in every .env template and read by nothing — Settings did
    not declare it and `extra="ignore"` swallowed it, so setting it did
    precisely nothing. Asserted so it cannot quietly stop working again.
    """

    root = logging.getLogger()
    original = root.handlers[:]
    original_level = root.level

    try:
        configure_logging(level="WARNING")

        assert root.level == logging.WARNING
        assert not root.isEnabledFor(logging.INFO)
    finally:
        root.handlers[:] = original
        root.setLevel(original_level)


def test_noisy_third_party_loggers_are_quieted():
    """httpx logs a line per model call at INFO, drowning our own events."""

    root = logging.getLogger()
    original = root.handlers[:]
    original_level = root.level

    try:
        configure_logging(level="DEBUG")

        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        root.handlers[:] = original
        root.setLevel(original_level)


def test_json_output_survives_values_that_are_not_serialisable():
    """
    A log line that raises while formatting loses the event it was
    reporting — usually the interesting one.
    """

    payload = formatted(when=object())

    assert isinstance(payload["when"], str)
