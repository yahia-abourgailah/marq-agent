#!/usr/bin/env python3
"""
[claude] Delete conversations nobody has touched for a while.

Why this is a script and not a background task
----------------------------------------------
It deletes customer-derived data on a timer. Two things follow from that.

It should be *scheduled by whoever owns the retention policy*, not started
silently by the application — a sweep that runs because the process booted is
a policy nobody chose. Point cron or a Kubernetes CronJob at this.

And it defaults to reporting rather than deleting. `--apply` is required;
without it this prints exactly what it would remove and exits. The first run
of a deletion job should never be the first time anyone sees its scope.

What it removes, and in which order
-----------------------------------
For each expired thread: the checkpoints first, then the index row.

That order is not arbitrary and it matches app/api/routes/threads.py.
Deleting the index row first would leave the transcript in the checkpointer
with nothing pointing at it — unlistable, undeletable through the API, and
still holding answers derived from CRM data. Done this way round, a failure
halfway leaves a conversation that is still listed and can be swept again,
which is the recoverable direction.

`conversation_turns` follows the index row by cascade, so the SQL behind each
answer cannot be orphaned on disk.

    python scripts/sweep_conversations.py                  # report only
    python scripts/sweep_conversations.py --days 30        # a different window
    python scripts/sweep_conversations.py --apply          # actually delete
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db.repositories.conversations import ConversationRepository  # noqa: E402
from app.db.state import build_state_pool  # noqa: E402
from app.graph.checkpointer import open_checkpointer  # noqa: E402


async def sweep(days: int, apply_changes: bool) -> int:
    pool = build_state_pool(settings)
    await pool.open()

    checkpointer = await open_checkpointer(settings, pool=pool)
    conversations = ConversationRepository(pool)

    try:
        expired = await conversations.expired(days)

        if not expired:
            print(f"Nothing older than {days} days. Nothing to do.")
            return 0

        print(
            f"{len(expired)} conversation(s) untouched for more than "
            f"{days} days:"
        )

        for subject, thread_id in expired[:20]:
            print(f"  {subject}  {thread_id}")

        if len(expired) > 20:
            print(f"  ... and {len(expired) - 20} more")

        if not apply_changes:
            print(
                "\nReport only. Re-run with --apply to delete these, their "
                "checkpoints, and the SQL provenance that cascades with them."
            )
            return 0

        delete_thread = getattr(checkpointer.saver, "adelete_thread", None)
        removed = 0
        failed = 0

        for subject, thread_id in expired:
            thread_key = f"{subject}:{thread_id}"

            try:
                # Checkpoints first — see the module docstring.
                if delete_thread is not None:
                    await delete_thread(thread_key)

                await conversations.delete(subject, thread_id)
                removed += 1
            except Exception as exc:
                # One bad row must not abandon the rest of the sweep. The
                # thread stays listed and is swept again next run, which is
                # the recoverable direction.
                failed += 1
                print(f"  FAILED {subject}/{thread_id}: {type(exc).__name__}")

        print(f"\nDeleted {removed} conversation(s).")

        if failed:
            print(f"{failed} could not be deleted and remain listed.")

        return 1 if failed else 0
    finally:
        await checkpointer.aclose()
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=settings.conversation_retention_days,
        help=(
            "Age in days after which a conversation is swept "
            f"(default: {settings.conversation_retention_days})"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this, the run only reports.",
    )
    args = parser.parse_args()

    if args.days < 1:
        parser.error("--days must be at least 1")

    return asyncio.run(sweep(args.days, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
