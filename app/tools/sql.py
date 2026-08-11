from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from app.db.repositories.deals import DealsRepository
from app.sql.agent import generate_sql


class SQLTool:
    """Global read-only SQL tool backed by the SQL generation agent."""

    def __init__(
        self,
        sql_agent: Any,
        repository: DealsRepository,
    ) -> None:
        self.sql_agent = sql_agent
        self.repository = repository

    async def query(self, question: str) -> dict[str, Any]:
        """Generate and execute a read-only SQL query for a natural-language question."""

        if not question or not question.strip():
            return {
                "success": False,
                "error": "The database question cannot be empty.",
            }

        try:
            sql = await generate_sql(
                agent=self.sql_agent,
                question=question,
            )

            rows = await self.repository.execute_read(sql)

            return {
                "success": True,
                "data": rows,
                "row_count": len(rows),
            }

        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
            }


def build_sql_tool(
    sql_agent: Any,
    repository: DealsRepository,
):
    """Build the global SQL LangChain tool with injected dependencies."""

    service = SQLTool(
        sql_agent=sql_agent,
        repository=repository,
    )

    @tool
    async def sql_query(question: str) -> dict[str, Any]:
        """Answer a read-only database question using the SQL agent."""

        return await service.query(question)

    return sql_query


__all__ = [
    "SQLTool",
    "build_sql_tool",
]