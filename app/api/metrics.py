"""
[claude] Prometheus counters for how turns end.

`stop_reason` exists so that an out-of-steps run stops passing for an answer
— it is decided by control flow at one node and logged on every turn. Logged
is not the same as observable: without a rate to alert on, the field is only
useful during an investigation you already knew to start, and the whole point
of it is the failure nobody noticed.

What is counted, and what is not
--------------------------------
Turn outcomes, tool calls, and rate-limit rejections. All of them are
low-cardinality by construction:

    stop_reason   four values, closed set (app/graph/state.py)
    route         five values, closed set (app/graph/supervisor.py)
    tool          the names the agents actually hold

Nothing here is labelled with a subject, a thread id, a request id or a
question. Those are unbounded, and an unbounded label is how a metrics
backend falls over — but the more important reason is that they are customer
data, and the redaction rule in logging_config.py does not reach a metrics
endpoint. `request_id` stays in the logs, where a specific incident is
investigated; metrics answer "how often", not "which one".

Why a registry of our own
-------------------------
The default registry is global and pre-populated with process collectors,
which makes it awkward to reset between tests and impossible to assert on
without also asserting on the interpreter's garbage collector. This one holds
only what this module defines.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, generate_latest

REGISTRY = CollectorRegistry()

TURNS = Counter(
    "marq_turns_total",
    "Conversation turns, by how they ended and who answered.",
    ("stop_reason", "route", "streamed"),
    registry=REGISTRY,
)

TOOL_CALLS = Counter(
    "marq_tool_calls_total",
    "Tool invocations, by tool name.",
    ("tool",),
    registry=REGISTRY,
)

RATE_LIMITED = Counter(
    "marq_rate_limited_total",
    "Requests rejected for exceeding a caller's rate limit.",
    registry=REGISTRY,
)


def record_turn(
    stop_reason: str,
    route: str | None,
    streamed: bool,
    tools: list[str] | None = None,
) -> None:
    """
    Count one completed turn.

    [claude] Never raises. A metrics backend is an operational convenience
    and a turn that answered correctly must not be reported as failed
    because a counter did — which is the failure mode of instrumenting a
    request path at all.
    """

    try:
        TURNS.labels(
            stop_reason=stop_reason,
            route=route or "unknown",
            streamed="true" if streamed else "false",
        ).inc()

        for tool in tools or ():
            TOOL_CALLS.labels(tool=tool).inc()
    except Exception:  # pragma: no cover - defensive
        pass


def record_rate_limited() -> None:
    try:
        RATE_LIMITED.inc()
    except Exception:  # pragma: no cover - defensive
        pass


def render() -> bytes:
    """The exposition format, for the /metrics endpoint."""

    return generate_latest(REGISTRY)


__all__ = [
    "RATE_LIMITED",
    "REGISTRY",
    "TOOL_CALLS",
    "TURNS",
    "record_rate_limited",
    "record_turn",
    "render",
]
