"""
Chat model client, pointed at the configured vLLM endpoint.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from app.config import settings


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

    return ChatOpenAI(
        model=settings.model_name,
        base_url=settings.model_base_url,
        api_key=settings.model_api_key,
        temperature=temperature,  # [claude] was unset
        timeout=30,
        max_retries=2,
    )


__all__ = ["get_model"]
