#!/usr/bin/env python3
"""
[claude] Manual database connectivity check.

Moved here from tests/test_db_connection.py, which pytest collected as a test
module even though it contained no test functions — only a main() and a
`__main__` guard. It is a diagnostic script, so it now lives with the other
scripts.

Usage:
    python scripts/check_db_connection.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import ENV_FILE  # noqa: E402
from app.db.connection import app_db  # noqa: E402


async def main() -> None:
    print(f"using {ENV_FILE}")

    await app_db.connect()

    try:
        async with app_db.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1 AS result;")
                print("database connection successful:", await cursor.fetchone())
    finally:
        await app_db.close()


if __name__ == "__main__":
    asyncio.run(main())
