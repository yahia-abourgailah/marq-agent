# Team rules for AI coding agents

Read this before any change. If a request conflicts with these rules, stop and ask.

## Workflow
- Before coding: read the code the change touches, then state the approach in 2-3 sentences and wait for confirmation.
- Stay inside the task's scope (the Trello card). Do not refactor, rename or reformat unrelated code.
- Never change behaviour and structure in the same commit.
- Logic change or bug fix: write a failing test first, then the code.
- Work on a `feature/*` or `fix/*` branch. Never push to the integration or release branch, never merge, never force-push.
- Never run migrations or write queries against shared, staging or production databases.
- Never read `.env` files or secrets. Never put real personal data (CVs, national IDs, passports, cheques, phone numbers, chats) in prompts, tests or fixtures; use synthetic samples.
- Commit messages use conventional prefixes (`feat:`, `fix:`, `refactor:`, `docs:`, `chore:`). No AI co-author trailers.

## Simplicity ladder: stop at the first rung that works
1. Does this need to exist? If the task doesn't ask for it, don't build it.
2. Already in this repo? Reuse the existing helper, type or pattern.
3. Standard library or platform feature? Use it.
4. Already-installed dependency? Use it. Never add a new dependency without asking.
5. Only then: the minimum code that meets the acceptance criteria.

Forbidden: interfaces or base classes with one implementation, factories for one product, config for values that never change, scaffolding "for later", broad `try/except` that hides errors.

## Code style
- No explanatory comments. Make code self-documenting; docstrings state the contract only.
- Small single-purpose functions; composition over inheritance.
- Python: type hints always; Pydantic for schemas; FastAPI dependency injection; async for I/O; no raw dicts for internal data.
- No magic numbers: named constants or enums.
- Validate inputs at boundaries (API, files, LLM output) and fail fast.
- Config via environment variables; update `.env.example` when adding one.
- Structured JSON logging, never `print`.
- APIs under `/v1/`. Services stateless; shared state in Postgres/Redis, never in process memory.
- Every external call has a timeout and handled errors; one bad input must not kill a worker.

## Before saying "done"
- Run the tests and linters below and show the output.
- Review your own diff for: edge-case input, crashed workers, races, resource leaks, auth/validation gaps, startup/health behaviour.
- Update README / `.env.example` / docs if behaviour changed.

## Repo specifics
- Feature branches off `dev`; PR into `dev`.
- Checks: `ruff check .`, `pytest -m "not integration" -q` (hermetic). `pytest -m integration` needs live infrastructure.
- Read `docs/HANDOFF.md`, `docs/TESTING.md` and `docs/adr/`.
