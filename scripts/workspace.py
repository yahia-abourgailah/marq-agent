#!/usr/bin/env python3
"""
[claude] Command-line access to the workspace, for trying it by hand.

There is no upload endpoint yet — `app/api/` is empty — so this is how a file
gets into a workspace outside of a test. It is a thin wrapper over
WorkspaceService, so it exercises exactly the path the agent's tools use
rather than a parallel one that could drift.

Usage:

    python scripts/workspace.py upload <workspace> <file>...
    python scripts/workspace.py list   <workspace>
    python scripts/workspace.py search <workspace> "<question>"
    python scripts/workspace.py ask    <workspace> "<question>"
    python scripts/workspace.py delete <workspace> <file_id>

`ask` runs the real Workspace Agent, so it needs the model endpoint and
PostgreSQL. Everything else runs locally.

The workspace name is any short slug — letters, digits, hyphen, underscore.
Files are isolated per workspace, so use two names to see that hold.

If no Qdrant server is running, set QDRANT_URL="" to use the embedded engine
persisted under settings.qdrant_path:

    QDRANT_URL="" python scripts/workspace.py upload demo report.xlsx
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.workspace.service import get_workspace_service  # noqa: E402
from app.workspace.store import WorkspaceError  # noqa: E402


def upload(workspace: str, paths: list[str]) -> int:
    service = get_workspace_service()

    for raw in paths:
        path = Path(raw)

        if not path.is_file():
            print(f"  {raw}: no such file")
            return 1

        try:
            result = service.ingest(workspace, path.name, path.read_bytes())
        except WorkspaceError as exc:
            print(f"  {path.name}: {exc}")
            return 1

        entry = result.file

        print(f"  {entry.filename}  ->  {entry.file_id}")
        print(f"    {entry.byte_size:,} bytes, "
              f"{result.chunks_indexed} chunks indexed")

        for sheet in entry.sheets:
            names = ", ".join(
                f"{column.name} ({column.dtype})" for column in sheet.columns
            )
            print(f"    sheet {sheet.name!r}: {sheet.row_count} rows — {names}")

        if entry.page_count:
            print(f"    {entry.page_count} pages")

        for warning in result.warnings:
            print(f"    ! {warning}")

    return 0


def show(workspace: str) -> int:
    files = get_workspace_service().list_files(workspace)

    if not files:
        print(f"  nothing uploaded to {workspace!r}")
        return 0

    for entry in files:
        detail = (
            f"{entry.page_count} pages"
            if entry.page_count
            else ", ".join(
                f"{sheet.name} ({sheet.row_count} rows)" for sheet in entry.sheets
            )
        )
        print(f"  {entry.file_id}  {entry.filename}  [{detail}]")

    return 0


def search(workspace: str, question: str) -> int:
    hits = get_workspace_service().search(workspace, question, limit=5)

    if not hits:
        print("  nothing found")
        return 0

    for hit in hits:
        print(f"  [{hit.score:.3f}] {hit.chunk.locator.cite()}")
        print(f"          {hit.chunk.text[:160]}")

    return 0


def delete(workspace: str, file_id: str) -> int:
    get_workspace_service().delete(workspace, file_id)
    print(f"  deleted {file_id}")
    return 0


def ask(workspace: str, question: str) -> int:
    """Run the real agent, printing the tools it used along the way."""

    from langchain_core.messages import HumanMessage

    from app.graph.agents.domain import WORKSPACE
    from app.graph.builder import build_graph

    async def run() -> None:
        graph = build_graph(WORKSPACE)

        result = await graph.ainvoke(
            {
                "messages": [HumanMessage(content=question)],
                "workspace_id": workspace,
            },
            config={"configurable": {"thread_id": f"cli-{workspace}"}},
        )

        used = [
            call["name"]
            for message in result["messages"]
            for call in getattr(message, "tool_calls", []) or []
        ]

        print(f"  tools: {used or 'none'}\n")
        print(result["messages"][-1].content)

    asyncio.run(run())
    return 0


COMMANDS = {
    "upload": lambda ws, rest: upload(ws, rest),
    "list": lambda ws, rest: show(ws),
    "search": lambda ws, rest: search(ws, " ".join(rest)),
    "ask": lambda ws, rest: ask(ws, " ".join(rest)),
    "delete": lambda ws, rest: delete(ws, rest[0]),
}


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return 2

    command, workspace, *rest = sys.argv[1:]

    if command != "list" and not rest:
        print(f"  {command} needs an argument — see --help")
        return 2

    try:
        return COMMANDS[command](workspace, rest)
    except WorkspaceError as exc:
        print(f"  {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
