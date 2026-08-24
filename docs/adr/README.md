# Architecture decision records

Five decisions that shape everything else here. Each was made deliberately,
each has been questioned at least once since, and none of them was written
down as a decision until now — the reasoning lived in `[claude]` comments,
in `HANDOFF.md`, and in commit messages, which is three places to update and
two that drift.

These are the durable half. A record says what was decided, what it was
decided *against*, and what it costs — so a future change can be an informed
reversal rather than an accidental one.

| | Decision |
|---|---|
| [0001](0001-guard-and-role-not-either.md) | The guard and the database role are both required, and neither substitutes for the other |
| [0002](0002-domain-binds-tables-guard-and-prompt.md) | One `Domain` binds a table set to the guard allowlist and the prompt's schema block |
| [0003](0003-nested-sql-agent.md) | SQL generation is a nested agent behind a tool, not the domain agent's own job |
| [0004](0004-routing-not-handoff.md) | One routing decision up front, not agents handing off mid-answer |
| [0005](0005-vectors-find-readers-compute.md) | Retrieval locates; the parsed file computes. No number comes from a vector search |

**Format.** Context, Decision, Alternatives, Consequences. Short. A record
that takes ten minutes to read will not be read before the change that
contradicts it.

**Status.** All five are Accepted and load-bearing. Superseding one means
adding a record that says so, not editing these.
