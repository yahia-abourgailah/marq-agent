#!/usr/bin/env python3
"""
[claude] Entry point for the MarQ Agent HTTP service.

Two ways to start it, and they are not interchangeable:

    python main.py                          development — one process, reload
    uvicorn main:app --workers 4            production — several processes

`app` is a module-level application, so any ASGI server can be pointed at
`main:app`. That is the form a container, a systemd unit or gunicorn wants.

On workers
----------
More than one worker is safe *because* conversations live in PostgreSQL. With
the in-memory checkpointer it would not be: each worker holds its own
conversations, so a follow-up question routed to a different process finds a
thread that never existed there, and the agent appears to forget at random.
`/health/ready` reports the checkpointer as not-ready when it is in-memory,
so this shows up as a failing probe rather than as a mystery.

Note this does not run a CRM migration. `migrations/` is applied with psql —
see migrations/README.md — and 002 is deliberately unapplied.
"""

from __future__ import annotations

import uvicorn

from app.api.app import create_app
from app.config import APP_ENV, settings

app = create_app()


def main() -> int:
    """Run the development server."""

    reload = APP_ENV == "development"

    uvicorn.run(
        # [claude] The import string rather than `app` when reloading —
        # uvicorn's reloader re-imports the module in a child process and
        # cannot do that with an already-constructed object.
        "main:app" if reload else app,
        host=settings.api_host,
        port=settings.api_port,
        reload=reload,
        # Logging is configured by the lifespan, in one format, with one
        # handler. Letting uvicorn install its own prints every line twice.
        log_config=None,
        log_level=settings.log_level.lower(),
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
