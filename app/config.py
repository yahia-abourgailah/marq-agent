"""
Application settings.

Values come from the environment file selected by APP_ENV.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

# [claude] APP_ENV now actually selects the environment file. It was
# hardcoded to ".env.development", so the README's environment table was
# fiction: staging and production silently loaded development settings, or
# failed at import when that file was absent.
#
# Read from os.environ rather than Settings itself — the value has to be
# known before the model is constructed, because it decides which file
# the model reads.
APP_ENV = os.getenv("APP_ENV", "development")
ENV_FILE = f".env.{APP_ENV}"


def reveal(secret: SecretStr | str | None) -> str | None:
    """
    [claude] Read a secret's actual value, at the one place that needs it.

    The five credential fields are `SecretStr` rather than `str`, which is a
    small change with one specific purpose: `str()` and `repr()` of a
    `SecretStr` are `**********`. A password can no longer reach a traceback,
    a log line, an `f"{settings}"`, or a `repr` of the settings object by
    accident — and every one of those is a route that has leaked credentials
    in real systems.

    It protects against carelessness, not against an attacker: anything that
    can call this can read the value, and the process holds it in memory
    regardless. What it buys is that leaking one now takes a deliberate
    `reveal()` rather than an incidental string format, which is the
    difference between a mistake anyone can make and one you have to mean.

    Accepts a plain `str` so a caller passing an already-revealed value, or
    a test passing a literal, does not have to care which it holds.
    """

    if secret is None:
        return None

    if isinstance(secret, SecretStr):
        return secret.get_secret_value()

    return secret


class Settings(BaseSettings):
    # Marquise application database
    postgres_host: str
    postgres_port: int = 5432
    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str

    # CRM PostgreSQL database
    # Optional for now — we will add the credentials when we connect
    # to the CRM database.
    crm_postgres_host: str | None = None
    crm_postgres_port: int = 5432
    crm_postgres_user: str | None = None
    crm_postgres_password: SecretStr | None = None
    crm_postgres_db: str | None = None

    # [claude] The read-only role the CRM reads should use.
    #
    # `guard.py` claimed since day one that a PostgreSQL role prevented the
    # application from modifying CRM data. It did not: `marq_agent_ro`
    # existed with no grants and the app connected as the owning user, so
    # every guard check was the only thing standing between a generated
    # query and a write.
    #
    # Optional, and unset falls back to the owner above — so nothing breaks
    # for a developer who has not run migrations/001_read_only_role.sql.
    # `Database.is_read_only` reports which one is actually in use, and a
    # startup check can refuse to serve if it is not the role.
    postgres_readonly_user: str | None = None
    postgres_readonly_password: SecretStr | None = None

    # Redis
    redis_url: str

    # Qdrant
    qdrant_url: str
    qdrant_collection: str

    # [claude] Workspace — user-uploaded files.
    #
    # All three are optional with defaults, so existing environment files
    # keep working unchanged.
    #
    # `workspace_root` holds uploaded files and their parsed content. It is
    # deliberately outside the repository tree by default: uploads contain
    # customer data, and a directory under the working tree is one `git add
    # -A` away from being committed.
    workspace_root: str = "./var/workspace"

    # Separate from `qdrant_collection` on purpose. That one is reserved for
    # CRM-derived vectors; mixing user uploads into it would make the
    # per-workspace payload filter the only thing separating a user's files
    # from application data.
    workspace_collection: str = "marq_workspace"

    # [claude] Embedded-Qdrant directory, used only when qdrant_url is empty.
    # Lets local development run without a Qdrant server; see build_index().
    # Not a production configuration — set QDRANT_URL instead.
    qdrant_path: str = "./var/qdrant"

    # Multilingual on purpose — see app/workspace/embeddings.py.
    embedding_model: str = (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )

    model_name: str
    model_base_url: str
    model_api_key: SecretStr

    # [claude] Defaults to 0.0 — greedy decoding.
    #
    # The SQL Agent is effectively a compiler: one question should map to one
    # query. Sampling there produces intermittently different SQL and makes
    # any eval a coin flip rather than a measurement.
    #
    # Optional with a default, so existing environment files keep working.
    model_temperature: float = 0.0

    # ==========================================================
    # [claude] HTTP API layer
    # ==========================================================
    #
    # `LOG_LEVEL` and `API_V1_PREFIX` have been in every .env template since
    # the beginning and were read by nothing — `extra="ignore"` swallowed
    # them silently, so setting LOG_LEVEL=DEBUG did exactly nothing. They are
    # declared here now, which is what makes them real.

    log_level: str = "INFO"
    api_v1_prefix: str = "/v1"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # [claude] Bound to loopback by default, deliberately.
    #
    # This service answers questions about customer data. A default of
    # 0.0.0.0 means anyone who runs it without reading the configuration has
    # published it to their whole network; a default of 127.0.0.1 fails
    # closed, and the deployment that genuinely needs to bind publicly has
    # to say so. Set API_HOST=0.0.0.0 in the container.

    # ----------------------------------------------------------
    # Cross-origin access
    # ----------------------------------------------------------
    #
    # The website front end is served from a different origin, so it needs
    # CORS. Empty by default rather than "*": credentialed requests with a
    # wildcard origin are rejected by browsers anyway, so a permissive
    # default would only look like it worked.
    #
    # Comma-separated: CORS_ORIGINS=https://app.example.com,https://admin.example.com
    cors_origins: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        """`cors_origins` split into a list, ignoring blanks."""

        return [
            origin.strip()
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]

    # ----------------------------------------------------------
    # JWT bearer authentication
    # ----------------------------------------------------------
    #
    # [claude] The front end authenticates its own users and presents a
    # signed token here. The employee id is read from a claim, so it is
    # asserted by whoever holds the signing key rather than by the caller.
    #
    # This is the layer docs/HANDOFF.md defers `requester_id` to, and the
    # reason it must be a claim: row-level security in
    # migrations/002_row_level_security.sql filters on `app.requester_id`,
    # so a requester id taken from a request body would let any caller read
    # any employee's rows by editing one JSON field.
    #
    # Asymmetric (RS256/ES256) is the better fit — the issuer holds the
    # private key and this service only ever needs the public half, so a
    # compromise here cannot mint tokens. HS256 is supported for deployments
    # that already have a shared secret.
    jwt_algorithm: str = "RS256"

    # One of these is required unless auth_dev_mode is on. `jwt_public_key`
    # for asymmetric algorithms, `jwt_secret` for HS*.
    jwt_public_key: str | None = None

    # [claude] The outgoing secret during an HS* key rotation.
    #
    # Asymmetric setups need no equivalent — `jwt_public_key` accepts a PEM
    # bundle, so several keys travel in the field that already exists. A
    # shared secret has no such format, so retiring one needs somewhere to
    # put it.
    #
    # Set it to the old secret, deploy, wait out the longest token TTL,
    # then clear it. Without a window, changing a signing key signs every
    # user out mid-question.
    jwt_secret_previous: SecretStr | None = None
    jwt_secret: str | None = None

    # [claude] The public key as a file path instead of a literal.
    #
    # A PEM is multi-line. python-dotenv does parse a quoted multi-line value
    # correctly, so `JWT_PUBLIC_KEY` works — but a key blob wedged into an
    # env file is unreadable and easy to corrupt with an editor that trims
    # trailing whitespace. A path is also how secrets actually arrive in
    # production: mounted into the container as a file.
    #
    # `jwt_public_key` wins when both are set, so an explicit literal is
    # never silently overridden by a stale file.
    jwt_public_key_path: str | None = None

    # Verified when set. Leaving them unset skips the check, which is worth
    # avoiding in production: without an audience check, a token minted for
    # a different service by the same issuer is accepted here.
    jwt_issuer: str | None = None
    jwt_audience: str | None = None

    # Which claim carries the employee id. `sub` is the standard home for
    # it; override if the issuer puts it elsewhere.
    jwt_subject_claim: str = "sub"

    # [claude] Local development without a token issuer.
    #
    # When on, an unsigned `X-Debug-Subject` header supplies the identity.
    # It is refused outright when APP_ENV is production — see
    # app/auth/jwt.py, which raises at construction rather than trusting
    # this to be set correctly. A flag that disables authentication is the
    # kind of thing that gets left on, so it defends itself.
    auth_dev_mode: bool = False

    # ----------------------------------------------------------
    # Web search
    # ----------------------------------------------------------
    #
    # [claude] Optional. Unset means the Research Agent reports that it
    # cannot look things up, rather than failing — the same shape as an
    # unreachable Qdrant costing search and nothing else.
    #
    # A query sent here leaves your infrastructure. Only the Research Agent
    # holds the tool, and it has no CRM access, so nothing derived from the
    # database can reach a third party through it.
    tavily_api_key: SecretStr | None = None

    # ----------------------------------------------------------
    # Local UI
    # ----------------------------------------------------------
    #
    # [claude] Serves app/api/static/index.html at `/`.
    #
    # A single file with no build step, served by this API so it is
    # same-origin — which is why it needs no CORS entry and cannot be
    # broken by one being missing. It is a development tool: it talks to
    # the same endpoints the real front end will, so it doubles as a way to
    # see the API behave rather than reading JSON.
    #
    # Off in production. Not because the page is dangerous — it is static
    # and holds no secrets, and every request it makes still needs a
    # verified token — but because the production deployment's front end is
    # the company website, and two UIs answering on one host is a way to
    # confuse whoever is debugging at 2am.
    serve_ui: bool = True

    # [claude] A token the local console picks up on its own, so a
    # developer opening `/` does not have to mint and paste one.
    #
    # Development only, and enforced rather than documented: `create_app`
    # raises when this is set with `APP_ENV=production`, and the route that
    # serves it is not registered there at all. Two gates, because this is
    # a bearer token being handed to anyone who can load the page, and
    # "we'll remember not to set it in prod" is not a control.
    #
    # It is a literal token rather than a subject to mint for, which means
    # it expires — `dev_token.py mint` defaults to 24 hours. Mint a longer
    # one for a machine you use daily:
    #
    #     python scripts/dev_token.py mint --expires 2592000   # 30 days
    #
    # When it expires the console falls back to the manual field, which is
    # the same place it started.
    dev_ui_token: SecretStr | None = None

    # ----------------------------------------------------------
    # Answer provenance
    # ----------------------------------------------------------
    #
    # [claude] Return the SQL behind each answer.
    #
    # On by default because it is the point: every bug this project has
    # found was a confident wrong number rather than an exception, and
    # without the query a user cannot tell a right answer from a plausible
    # one. Showing the query makes the claim checkable.
    #
    # The cost is that it reveals table and column names to anyone who can
    # already query them through this API, which is a much smaller
    # disclosure than it sounds. Turn it off if the front end would surface
    # it to an audience that should not see the schema.
    expose_provenance: bool = True

    # ----------------------------------------------------------
    # Context budget
    # ----------------------------------------------------------
    #
    # [claude] How much conversation history a specialist may be handed,
    # in tokens.
    #
    # The transcript no longer carries tool payloads (see graph/state.py),
    # which removed the fast path to overflow. It does not remove the slow
    # one: `messages` still accumulates a question and an answer per turn
    # for the life of the thread, and a thread has no other bound on its
    # length.
    #
    # What made this worth a hard limit rather than a warning is the
    # failure mode. Overflow arrives as a provider error, is caught as a
    # generic specialist failure, and — because the state is checkpointed —
    # the oversized history is now the thread's permanent state. Every
    # later turn reloads it and fails identically. The conversation cannot
    # be recovered by the user, and nothing in the reply tells them to
    # start a new one.
    #
    # 12,000 leaves room inside a 32k window for the system prompt, the
    # tool schemas, the retrieved rows a turn actually needs, and the
    # answer. It is a budget rather than a measurement — `usage_metadata`
    # is logged per turn so the real figure can be observed and this tuned
    # against it rather than guessed at again.
    max_context_tokens: int = 12_000

    # [claude] How many model calls this process may have in flight at once.
    #
    # One turn is not one call. It is the routing call, then up to two
    # specialists in parallel, each of which may run a nested SQL-agent
    # call, then synthesis — up to five, and nothing anywhere bounded them.
    # Against a vLLM server running `--max-num-seqs 32`, a handful of
    # simultaneous users saturates the endpoint and everybody queues behind
    # everybody else, which shows up as every request being slow rather
    # than as a capacity problem.
    #
    # A ceiling does not make the endpoint faster. It makes the queue
    # explicit and keeps it on this side of the network, where a request
    # waiting for a slot is visible as a slot wait rather than as an
    # unexplained latency spike.
    #
    # 8 is deliberately below the endpoint's own concurrency: several
    # services share that box, and a client that assumes it owns all of a
    # shared resource is how one tenant starves the rest. Raise it from
    # what vLLM logs at startup, not from this comment.
    max_concurrent_model_calls: int = 8

    # Requests per minute per authenticated subject. A browser tab in a
    # retry loop is otherwise unbounded spend against a paid endpoint.
    rate_limit_per_minute: int = 30

    # [claude] The longest a single conversation may run, in turns.
    #
    # Trimming keeps a long thread *working* — it no longer overflows the
    # context window — but it does so by dropping the oldest exchanges,
    # silently. Past some length a thread is not a conversation any more,
    # it is a thread whose beginning the agent can no longer see while the
    # user still remembers writing it, and the honest thing is to say so
    # and start a fresh one.
    #
    # Surfaced as a clear message rather than a generic failure, which is
    # the specific complaint the review made: the old overflow arrived as a
    # provider error caught as "specialist failed", so the user learned
    # nothing and the thread stayed broken.
    max_turns_per_thread: int = 100

    # How long a conversation is kept after its last turn.
    #
    # This is a retention decision, not a capacity one. Checkpoints hold
    # the transcript and `conversation_turns` holds the SQL behind every
    # answer — both derived from CRM data, and nothing expired either of
    # them. Sweeping is exposed as a repository method and a script rather
    # than run automatically, because deleting customer-derived data on a
    # timer is a policy someone should choose deliberately.
    conversation_retention_days: int = 90

    # ----------------------------------------------------------
    # Uploads
    # ----------------------------------------------------------
    #
    # [claude] A bound on what one request may push through the parser.
    # Ingest reads the whole file into memory and parses it synchronously,
    # so an unbounded upload is a memory exhaustion away from taking the
    # process down. 25 MB comfortably covers a real CRM export.
    max_upload_bytes: int = 25 * 1024 * 1024

    # ----------------------------------------------------------
    # Conversation persistence
    # ----------------------------------------------------------
    #
    # [claude] The LangGraph checkpointer. InMemorySaver loses every
    # conversation when the process restarts and shares nothing between
    # workers, so under more than one uvicorn worker a follow-up question
    # lands on a process that has never heard of the thread.
    #
    # Postgres-backed when on, which is what makes the thread endpoints
    # honest. Off in tests, which keeps them hermetic.
    checkpointer_backend: str = "postgres"

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,  # [claude] was hardcoded to ".env.development"
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    # [claude] Settings is instantiated at import time (see below), so a
    # misconfigured environment surfaces as a ten-error pydantic traceback
    # from whichever module happened to import app.config first. When the
    # cause is simply a missing env file, say so plainly instead.
    #
    # Real environment variables are still honoured when no file exists —
    # the friendly error only replaces the message when validation has
    # already failed AND the expected file is absent.
    try:
        return Settings()
    except ValidationError as exc:
        if not Path(ENV_FILE).is_file():
            raise RuntimeError(
                f"Missing environment file '{ENV_FILE}' "
                f"(APP_ENV={APP_ENV!r}).\n"
                f"Copy the template and fill it in:\n"
                f"    cp {ENV_FILE}.example {ENV_FILE}\n"
                f"Or set the variables in the environment directly."
            ) from exc
        raise


settings = get_settings()


__all__ = [
    "APP_ENV",  # [claude]
    "ENV_FILE",  # [claude]
    "Settings",
    "get_settings",
    "settings",
]
