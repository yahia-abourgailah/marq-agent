"""
[claude] Run the SQL Agent behavioural cases and print a report.

    python -m evals.run
    python -m evals.run --tag masked
    python -m evals.run --verbose      # show generated SQL for every case

Exits non-zero if any case fails, so it can gate a prompt or catalogue change.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.cases import CASES, Case  # noqa: E402


async def evaluate(case: Case, agent) -> tuple[bool, str]:
    """Return (passed, detail)."""

    from app.sql.agent import Refused, generate_sql

    result = await generate_sql(agent=agent, question=case.question)

    if isinstance(result, Refused):
        if case.expect_refusal:
            return True, f"refused: {result.reason}"
        return False, f"unexpected refusal: {result.reason}"

    if case.expect_refusal:
        return False, f"expected a refusal, got SQL: {result.query}"

    sql = " ".join(result.query.lower().split())

    missing = [s for s in case.must_contain if s not in sql]
    present = [s for s in case.must_not_contain if s in sql]

    if missing or present:
        parts = []
        if missing:
            parts.append(f"missing {missing}")
        if present:
            parts.append(f"must not contain {present}")
        return False, f"{'; '.join(parts)}\n      SQL: {result.query}"

    return True, result.query


async def main_async(tag: str | None, verbose: bool) -> int:
    from app.llm.model import get_model
    from app.sql.agent import build_sql_agent

    cases = [c for c in CASES if not tag or tag in c.tags]
    agent = build_sql_agent(get_model())

    passed = 0
    failures = []

    for case in cases:
        ok, detail = await evaluate(case, agent)

        if ok:
            passed += 1
            print(f"  PASS  {case.name}")
            if verbose:
                print(f"        {detail}")
        else:
            failures.append(case.name)
            print(f"  FAIL  {case.name}")
            print(f"        why: {case.why}")
            print(f"        {detail}")

    total = len(cases)
    print(f"\n  {passed}/{total} passed")

    if failures:
        print(f"  failing: {', '.join(failures)}")

    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="only run cases carrying this tag")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    raise SystemExit(asyncio.run(main_async(args.tag, args.verbose)))


if __name__ == "__main__":
    main()
