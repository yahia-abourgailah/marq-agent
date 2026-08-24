"""
Chat model client, pointed at the configured vLLM endpoint.
"""

from __future__ import annotations

import asyncio
import logging

from langchain_openai import ChatOpenAI

from app.config import reveal, settings

logger = logging.getLogger("marq.llm")

# [claude] One ceiling for every model call this process makes.
#
# A turn is up to five calls — routing, two specialists in parallel, a
# nested SQL-agent call inside each, then synthesis — and nothing bounded
# them. This is module-level rather than per-graph because the resource
# being protected is the endpoint, which is shared by every graph, every
# request and every other service pointed at the same box.
#
# Built lazily and bound to the running loop: a Semaphore created at import
# belongs to whichever loop imported it, and the test suite runs a
# session-scoped loop that is not the server's.
_slots: asyncio.Semaphore | None = None
_slots_loop: asyncio.AbstractEventLoop | None = None


def model_slot() -> asyncio.Semaphore:
    """The shared ceiling on concurrent model calls."""

    global _slots, _slots_loop

    loop = asyncio.get_running_loop()

    if _slots is None or _slots_loop is not loop:
        _slots = asyncio.Semaphore(settings.max_concurrent_model_calls)
        _slots_loop = loop

    return _slots


def get_model(temperature: float | None = None) -> ChatOpenAI:
    """
    Build a chat model client.

    [claude] `temperature` was previously not passed at all, so generation ran
    at whatever the endpoint defaulted to — sampled, not greedy. Two
    consequences:

    - SQL generation is a compilation step. The same question should produce
      the same query; sampling means it sometimes does not, and a query that
      is right nine times in ten is a bug that only shows up in production.
    - Any test or eval over model output measures a sample rather than the
      prompt. A failure cannot be told apart from an unlucky draw, which
      makes prompt and catalogue edits unmeasurable.

    Defaults to `settings.model_temperature` (0.0). Pass a value to override
    per call site — for example, if the Deals Agent's final prose should vary
    while SQL generation stays deterministic.
    """

    if temperature is None:
        temperature = settings.model_temperature

    return _Throttled(
        model=settings.model_name,
        base_url=settings.model_base_url,
        api_key=reveal(settings.model_api_key),
        temperature=temperature,  # [claude] was unset
        timeout=30,
        max_retries=2,
    )


class _Throttled(ChatOpenAI):
    """
    [claude] A ChatOpenAI that waits for a slot before it calls out.

    Subclassed rather than wrapped because the model is handed to
    `create_agent`, which calls it through interfaces this code never sees —
    a wrapper would bound the calls made from `builder.py` and miss every
    nested one made inside a ReAct loop, which is where the concurrency
    actually comes from.

    Only the async paths are gated. Nothing in the request path is
    synchronous, and a semaphore cannot be acquired from a sync method
    anyway.
    """

    async def ainvoke(self, *args, **kwargs):
        async with model_slot():
            return await super().ainvoke(*args, **kwargs)

    async def astream(self, *args, **kwargs):
        async with model_slot():
            async for chunk in super().astream(*args, **kwargs):
                yield chunk


__all__ = ["get_model", "model_slot"]
