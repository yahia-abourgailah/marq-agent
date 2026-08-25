"""
[claude] The FastAPI application — the layer between the company website and
this agent.

    browser  ->  FastAPI (auth, identity, limits)  ->  supervisor graph
                                                          |
                                                    SQLGuard -> PostgreSQL

What this layer is responsible for, in one sentence each:

*   **Authentication.** A verified JWT is the only way in, and the only
    source of `requester_id` and `workspace_id`.
*   **Identity plumbing.** Those two ids travel as graph state, never as
    request fields — see app/api/schemas.py.
*   **Lifecycle.** One graph, one model client, one state pool, built at
    startup rather than per request.
*   **Containment.** Typed errors, size limits, and no internals on the wire.

It deliberately holds no business logic. Every route is a thin translation
between HTTP and something that already existed and was already tested.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.errors import install_error_handlers, new_request_id
from app.api.routes import chat, health, threads, workspace
from app.auth.jwt import TokenVerifier
from app.config import APP_ENV, reveal, settings
from app.db.connection import app_db
from app.db.repositories.conversations import ConversationRepository
from app.db.state import build_state_pool
from app.graph.builder import build_supervisor_graph
from app.graph.checkpointer import open_checkpointer
from app.logging_config import configure_logging

logger = logging.getLogger("marq.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Build everything expensive once, and take it down cleanly.

    [claude] docs/HANDOFF.md predicted this file: "`settings` and `app_db`
    are built at import. This is why the test suite needs a session-scoped
    event loop, and it will resurface when the API layer needs a lifespan."

    It did resurface, and the ordering below is the resolution. `app_db` is
    still a module-level singleton, so the pool is opened here rather than
    constructed here — the object already exists by the time this runs. The
    graph, the state pool and the checkpointer are genuinely built here,
    because they are the ones that must not be per-request.

    Anything that fails here fails the boot. That is deliberate: a service
    that starts without a checkpointer and discovers it at the first
    follow-up question has converted a configuration error into an
    intermittent one.
    """

    configure_logging()

    logger.info(
        "starting",
        extra={"environment": APP_ENV, "version": health.API_VERSION},
    )

    # Fails immediately when key material is missing, rather than at the
    # first user's first request.
    app.state.verifier = TokenVerifier(settings)

    await app_db.connect()

    # [claude] Writable pool, separate from app_db, which connects as the
    # read-only role. Shared between the checkpointer and the conversation
    # index — see app/db/state.py for why they cannot use the CRM pool.
    state_pool = build_state_pool(settings)
    await state_pool.open()
    app.state.state_pool = state_pool

    app.state.checkpointer = await open_checkpointer(settings, pool=state_pool)
    app.state.conversations = ConversationRepository(state_pool)

    app.state.graph = build_supervisor_graph(
        checkpointer=app.state.checkpointer.saver
    )

    # Built lazily inside the service; touched here so a broken workspace
    # configuration surfaces at boot rather than at the first upload.
    from app.workspace.service import get_workspace_service

    app.state.workspace_service = get_workspace_service()

    logger.info(
        "started",
        extra={
            "persistent_checkpoints": app.state.checkpointer.is_persistent,
            "read_only_db": app_db.is_read_only,
            "auth_dev_mode": settings.auth_dev_mode,
        },
    )

    try:
        yield
    finally:
        logger.info("shutting_down")

        await app.state.checkpointer.aclose()
        await state_pool.close()
        await app_db.close()


def create_app() -> FastAPI:
    """Build the application. Importable as `app.api.app:create_app`."""

    api = FastAPI(
        title="MarQ Agent API",
        version=health.API_VERSION,
        description=(
            "Conversational access to the MyTAI CRM, plus a per-user "
            "workspace for uploaded files. Read-only: every question "
            "reaches the database through a guarded SELECT."
        ),
        lifespan=lifespan,
    )

    # ----------------------------------------------------------
    # Cross-origin access
    # ----------------------------------------------------------
    #
    # [claude] Only the origins named in configuration, and only when some
    # are named. `allow_credentials` with `allow_origins=["*"]` is rejected
    # by browsers anyway, so a permissive default would be a setting that
    # looks like it works and does not.
    if settings.cors_origin_list:
        api.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @api.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        """
        Tag every request so a report of "it failed" can find the log line.

        [claude] Read from the inbound header when the front end already has
        one, so a trace crosses the two systems instead of restarting here.
        """

        request_id = request.headers.get("X-Request-ID") or new_request_id()
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id

        return response

    install_error_handlers(api)

    # Health is unprefixed and unauthenticated: probes should not need a
    # token, and a load balancer should not have to know the API version.
    api.include_router(health.router)

    prefix = settings.api_v1_prefix

    api.include_router(chat.router, prefix=prefix)
    api.include_router(threads.router, prefix=prefix)
    api.include_router(workspace.router, prefix=prefix)

    # ----------------------------------------------------------
    # The local UI
    # ----------------------------------------------------------
    #
    # [claude] Registered last, and only when enabled, so it can never
    # shadow an API route. Same-origin by construction, which is what
    # keeps it working with `CORS_ORIGINS` empty.
    # [claude] A configured console token is refused outright in
    # production, whether or not the UI is served there.
    #
    # The route below is only registered outside production, so this is the
    # second gate rather than the only one. It exists because the failure
    # it guards against is silent: a `.env.production` that inherited
    # `DEV_UI_TOKEN` from a copied development file would hand a working
    # bearer token to anyone who could load the page, and nothing about the
    # deployment would look wrong.
    if APP_ENV == "production" and settings.dev_ui_token:
        raise RuntimeError(
            "DEV_UI_TOKEN must not be set when APP_ENV=production. It "
            "serves a bearer token to anyone who can load the console."
        )

    if settings.serve_ui:
        static_dir = Path(__file__).parent / "static"
        index = static_dir / "index.html"
        vendor = static_dir / "vendor"

        # [claude] Third-party front-end code, served from disk rather than
        # from a CDN.
        #
        # This console is a window onto a private CRM, and a `<script
        # src="https://cdn…">` would announce to a third party, on every
        # page load, that an internal tool is being used — as well as
        # putting a public network dependency between an internal user and
        # an internal service.
        #
        # Mounted on the vendor directory specifically, not on `static`, so
        # this cannot become an accidental way to serve anything else that
        # is dropped in beside index.html. See static/vendor/README.md.
        if vendor.is_dir():
            api.mount(
                "/vendor",
                StaticFiles(directory=vendor),
                name="vendor",
            )

        # [claude] The page's own stylesheet and script, split out of
        # index.html on 21 August 2026 when it reached 3,000 lines.
        #
        # Registered by name rather than by mounting `static`, for the same
        # reason `/vendor` is narrow: a directory mount here would serve
        # whatever anyone later drops in beside the page, and the things
        # people drop next to a UI are exactly the things that should not be
        # public — a design export, a scratch copy, a `.env` someone was
        # comparing against.
        #
        # `no-store` on all three, and that is the part worth keeping.
        # These files only make sense as a set: the page names the CSS
        # classes, the CSS styles them and the script queries them by id.
        # Cache one and not the others and you get a *mismatched* set, which
        # does not present as a caching problem — it presents as a layout
        # regression, or as controls that silently do nothing because the
        # script is addressing markup that is no longer there. That is a
        # much worse hour than re-fetching 100 KB over localhost.
        #
        # The vendored library is the exception and is left cacheable: it is
        # version-pinned in its filename's package, changes only when
        # somebody re-vendors it, and is the only large file here.
        def _serve(path: Path):
            async def asset() -> FileResponse:
                return FileResponse(path, headers={"Cache-Control": "no-store"})

            return asset

        # [claude] The console's runtime configuration, as a script rather
        # than as a value baked into index.html.
        #
        # index.html is served from disk unchanged, and keeping it that way
        # matters: a page that is templated at request time is a page whose
        # served bytes differ from the file on disk, which is exactly the
        # sort of difference that makes "it works locally" hard to
        # investigate. This is a separate one-line file instead.
        #
        # Registered only outside production. In production the console is
        # normally off entirely (`SERVE_UI=false`), but if someone turns it
        # on, the automatic token must not follow.
        if APP_ENV != "production":

            @api.get("/app-config.js", include_in_schema=False)
            async def app_config() -> Response:
                token = reveal(settings.dev_ui_token) or ""

                # JSON-encoded, so a token containing a quote or a
                # backslash cannot terminate the string and become script.
                return Response(
                    content=(
                        "window.MARQ_DEV_TOKEN = "
                        f"{json.dumps(token or None)};\n"
                    ),
                    media_type="text/javascript",
                    headers={"Cache-Control": "no-store"},
                )

        for asset_name in ("app.css", "app.js"):
            asset_path = static_dir / asset_name

            if asset_path.is_file():
                api.get(f"/{asset_name}", include_in_schema=False)(
                    _serve(asset_path)
                )

        if index.is_file():

            @api.get("/", include_in_schema=False)
            async def ui() -> FileResponse:
                # no-store: the file changes as it is being worked on, and a
                # cached copy of a half-finished UI is a confusing bug report.
                return FileResponse(
                    index, headers={"Cache-Control": "no-store"}
                )

    return api


__all__ = ["create_app", "lifespan"]
