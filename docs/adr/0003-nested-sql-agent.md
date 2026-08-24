# 0003 — SQL generation is a nested agent behind a tool

**Status:** Accepted · **Date:** 2026-08-24 (decided 2026-08-12)

## Context

A domain agent has to answer questions in prose and produce correct SQL.
Those are different jobs with different failure modes: prose tolerates
paraphrase, SQL does not tolerate a single wrong column.

## Decision

`sql_query` is a tool whose implementation is its own agent
(`app/sql/agent.py`), with its own prompt, its own retry loop against the
guard's typed errors, and its own view of the schema. The domain agent asks
a question in English and receives rows.

## Alternatives

**The domain agent writes SQL directly.** Fewer moving parts and one less
model call, at the cost of one prompt carrying both the catalogue and the
conversational instructions — and every catalogue rule competing for
attention with every tone rule.

**Templated queries.** Safe and unable to answer anything not anticipated,
which is most of what people ask.

## Consequences

- A turn costs more model calls. The step ceiling accounts for it: a ReAct
  loop spends two steps per tool call, and `Domain.max_steps` is set per
  domain from its longest workflow.
- Guard errors are typed and handled where they can be acted on.
  `TableNotAllowedError` is non-retryable, because rephrasing cannot make a
  table permitted — untyped, it produced four retries and then a decline.
- Generated SQL never reaches the user's screen mid-stream: the SQL agent
  runs *inside* the tool boundary, which is what the token filter in
  `app/api/streaming.py` keys on.
- The queries are recorded out of band (`app/sql/provenance.py`) and shown
  under the answer, so a number can be checked rather than trusted.
