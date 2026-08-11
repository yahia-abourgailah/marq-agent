import asyncio

from app.db.connection import app_db


async def main() -> None:
    await app_db.connect()

    try:
        async with app_db.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1 AS result;")
                result = await cursor.fetchone()

                print("Database connection successful!")
                print(result)
    finally:
        await app_db.close()


if __name__ == "__main__":
    asyncio.run(main())