"""
[claude] Turn outcomes are countable, not just greppable.

`stop_reason` was built so an out-of-steps run stops passing for an answer.
Logging it made that investigable; counting it makes it *alertable*, which
is the difference between finding the failure when you go looking and being
told about it.

These assert the two properties that make the metric usable: it moves when a
turn ends, and it carries nothing unbounded. An unbounded label is how a
metrics backend falls over — and, more importantly here, subject and thread
ids are customer data, and the redaction rule in logging_config.py does not
reach a metrics endpoint.
"""

from __future__ import annotations

import pytest

from app.api import metrics
from tests.api_support import StubGraph, TokenIssuer, build_app, client


@pytest.fixture(scope="module")
def issuer():
    return TokenIssuer()


def sample(name: str, **labels) -> float:
    """One counter's current value, or 0.0 if it has never been touched."""

    value = metrics.REGISTRY.get_sample_value(name, labels)

    return value or 0.0


@pytest.mark.asyncio
async def test_a_completed_turn_is_counted(issuer):
    before = sample(
        "marq_turns_total",
        stop_reason="completed",
        route="deals",
        streamed="false",
    )

    app, _ = build_app(verifier=issuer.verifier(), graph=StubGraph())

    async with client(app) as http:
        await http.post("/v1/chat", json={"message": "hi"}, headers=issuer.auth())

    after = sample(
        "marq_turns_total",
        stop_reason="completed",
        route="deals",
        streamed="false",
    )

    assert after == before + 1


@pytest.mark.asyncio
async def test_an_out_of_steps_turn_is_counted_separately(issuer):
    """
    The whole reason this exists. An out-of-steps run returns 200 with prose
    in the body — indistinguishable from an answer to anything watching,
    unless it is counted under its own label.
    """

    before = sample(
        "marq_turns_total",
        stop_reason="out_of_steps",
        route="deals",
        streamed="false",
    )

    graph = StubGraph(stop_reason="out_of_steps")
    app, _ = build_app(verifier=issuer.verifier(), graph=graph)

    async with client(app) as http:
        response = await http.post(
            "/v1/chat", json={"message": "loops"}, headers=issuer.auth()
        )

    assert response.status_code == 200

    after = sample(
        "marq_turns_total",
        stop_reason="out_of_steps",
        route="deals",
        streamed="false",
    )

    assert after == before + 1


@pytest.mark.asyncio
async def test_tools_are_counted_by_name(issuer):
    before = sample("marq_tool_calls_total", tool="sql_query")

    app, _ = build_app(verifier=issuer.verifier(), graph=StubGraph())

    async with client(app) as http:
        await http.post("/v1/chat", json={"message": "hi"}, headers=issuer.auth())

    assert sample("marq_tool_calls_total", tool="sql_query") == before + 1


@pytest.mark.asyncio
async def test_a_rate_limited_request_is_counted(issuer, monkeypatch):
    from app.api import deps

    monkeypatch.setattr(
        deps.settings, "rate_limit_per_minute", 1, raising=False
    )
    deps.reset_rate_limiter()

    before = sample("marq_rate_limited_total")

    app, _ = build_app(verifier=issuer.verifier(), graph=StubGraph())

    async with client(app) as http:
        await http.post("/v1/chat", json={"message": "a"}, headers=issuer.auth())
        blocked = await http.post(
            "/v1/chat", json={"message": "b"}, headers=issuer.auth()
        )

    assert blocked.status_code == 429
    assert sample("marq_rate_limited_total") == before + 1

    deps.reset_rate_limiter()


@pytest.mark.asyncio
async def test_the_endpoint_serves_the_exposition_format(issuer):
    app, _ = build_app(verifier=issuer.verifier(), graph=StubGraph())

    async with client(app) as http:
        await http.post("/v1/chat", json={"message": "hi"}, headers=issuer.auth())
        response = await http.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "marq_turns_total" in response.text
    # A scraper needs the HELP/TYPE lines, not just the samples.
    assert "# TYPE marq_turns_total counter" in response.text


@pytest.mark.asyncio
async def test_metrics_need_no_token(issuer):
    """
    Requiring one would put a CRM credential in the monitoring stack, which
    is a worse place for it than anywhere it currently lives.
    """

    app, _ = build_app(verifier=issuer.verifier(), graph=StubGraph())

    async with client(app) as http:
        assert (await http.get("/metrics")).status_code == 200


@pytest.mark.asyncio
async def test_no_label_carries_customer_data(issuer):
    """
    [claude] The assertion that keeps this endpoint safe to leave open.

    Subject, thread id, request id and question text are all unbounded and
    all customer data. A label carrying any of them would both blow up the
    metrics backend's cardinality and route around the redaction that
    logging_config.py applies to logs — which does not reach here.
    """

    graph = StubGraph()
    app, _ = build_app(verifier=issuer.verifier(), graph=graph)

    async with client(app) as http:
        await http.post(
            "/v1/chat",
            json={
                "message": "what is Ahmed Ibrahim's unit number",
                "thread_id": "t-secret",
            },
            headers=issuer.auth("employee-9"),
        )
        body = (await http.get("/metrics")).text

    for forbidden in ("employee-9", "t-secret", "Ahmed", "unit number"):
        assert forbidden not in body, f"{forbidden!r} reached the metrics"

    # The label names themselves are the closed set.
    for line in body.splitlines():
        if line.startswith("marq_turns_total{"):
            labels = line.split("{", 1)[1].split("}", 1)[0]
            names = {p.split("=")[0] for p in labels.split(",")}
            assert names <= {"stop_reason", "route", "streamed"}
