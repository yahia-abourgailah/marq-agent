import asyncio

from app.db.connection import app_db
from app.sql.executor import SQLExecutor


async def main() -> None:
    await app_db.connect()

    try:
        executor = SQLExecutor(app_db)

        rows = await executor.execute(
            "SELECT 1 AS result;"
        )

        print("SQL execution successful!")
        print(rows)

    finally:
        await app_db.close()


if __name__ == "__main__":
    asyncio.run(main())