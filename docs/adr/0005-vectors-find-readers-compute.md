# 0005 — Vectors find; readers compute

**Status:** Accepted · **Date:** 2026-08-24 (decided 2026-08-19)

## Context

Uploaded spreadsheets and PDFs have to be both searchable ("what does my
file say about penalties") and countable ("how many rows are contracted").
Retrieval answers the first well and the second disastrously: a similarity
search returns the passages most like the question, which is not the set of
rows matching a condition.

## Decision

Two lanes, and no number crosses between them.

`workspace_search` returns passages with citations and is marked
`"complete": false`. Every aggregate comes from the parsed file through a
fixed vocabulary of operations (`app/workspace/query.py`) that reads every
row.

## Alternatives

**Compute from retrieved chunks.** The standard RAG shape, and it produces a
denominator drawn from a sample — a total that is confidently wrong in a way
nothing downstream can detect.

**Let the agent write SQL over uploaded files.** An uploaded file has no
schema until it arrives, so the guard's central guarantee — an allowlist
derived from the catalogue — cannot be reproduced for it.

## Consequences

- Only count/sum/avg/min/max with filters and one group-by are expressible
  over an upload. Joins across two uploaded files are not.
- Filters are data, never parsed expressions, so there is nothing for a
  hostile spreadsheet to inject into.
- The split bounds the damage from retrieval defects to *finding* rather
  than *counting*. When chunks were measured at 1,007 word-pieces against a
  128-token window, the consequence was passages that failed to surface —
  not totals that came out wrong.
- PDF numbers can be quoted, never computed: there is no exact lane for a
  table inside a PDF.
- Retrieval quality is not unit-testable here. The fake embedder is a token
  hash; whether the right passage ranks first belongs in an eval against the
  real encoder.
