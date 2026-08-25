# syntax=docker/dockerfile:1.7
#
# [claude] The MarQ agent, as an image.
#
# Two stages. The builder holds a compiler and a package index; the runtime
# holds neither, and copies in a finished virtualenv. That is worth the
# extra Dockerfile because `psycopg`, `pydantic-core` and `tokenizers` all
# want build tooling that has no business being in a running container.
#
# What this image is not
# ----------------------
# It is not a database, a vector store or a model server. Those are external
# and configured by environment variable — see docker-compose.yml for a
# local set. The image is one process, and the health endpoint reports which
# of its dependencies it can actually reach.

# ==============================================================
# Stage 1 — build the virtualenv
# ==============================================================
FROM python:3.14-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential for the wheels that have no manylinux build for this
# interpreter yet; libpq-dev is psycopg's. Both stay in this stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# [claude] CPU-only torch, installed before anything that depends on it.
#
# `sentence-transformers` pulls torch, and the default wheel carries the
# CUDA runtime — well over a gigabyte of it — for hardware this image will
# never have. app/workspace/embeddings.py says the encoder "runs in-process
# on CPU", so the CUDA build is entirely dead weight. Installing the CPU
# wheel first means the dependency is already satisfied when
# sentence-transformers is resolved.
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        torch

COPY requirements.txt .

# [claude] The whole of requirements.txt, including pytest and ruff.
#
# A production image does not need them, and splitting the file would mean
# CI, the developer setup and this image each installing a different set —
# which is how "works in CI" and "works in the image" stop meaning the same
# thing. The saving is tens of megabytes against a torch install; the risk
# is a class of bug that only appears in one environment. Split the
# requirements when there is a reason beyond size.
RUN pip install --no-cache-dir -r requirements.txt

# ==============================================================
# Stage 2 — the runtime
# ==============================================================
FROM python:3.14-slim AS runtime

# libpq5 is the runtime half of libpq-dev; curl is for HEALTHCHECK, which
# has no other way to speak HTTP from inside the container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# [claude] A non-root user, created before anything is copied so the
# ownership is right without a recursive chown layer.
#
# The application never writes outside `var/`, and nothing it does needs
# root. Running as root would mean a container escape starts with the whole
# filesystem rather than with one unprivileged account.
RUN useradd --create-home --uid 10001 marq

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    API_HOST=0.0.0.0 \
    API_PORT=8000 \
    HF_HOME=/home/marq/.cache/huggingface

WORKDIR /app

# Application code only — `.dockerignore` keeps `var/`, `.git`, the local
# virtualenv and every `.env` out of the context entirely.
COPY --chown=marq:marq app/ ./app/
COPY --chown=marq:marq scripts/ ./scripts/
COPY --chown=marq:marq migrations/ ./migrations/
COPY --chown=marq:marq main.py ./

# Uploads and the embedded vector store, when one is used. A volume belongs
# here in any deployment that keeps them; without one they are lost with the
# container, which is correct for a stateless replica and wrong for a single
# host.
RUN mkdir -p /app/var/workspace /app/var/qdrant \
    && chown -R marq:marq /app/var

USER marq

# [claude] The encoder, baked in rather than downloaded on first use.
#
# `sentence-transformers` fetches roughly 470 MB the first time it embeds
# anything. Left to runtime that download happens inside the first user's
# first upload — slow, and impossible on a host with no route to the model
# hub, which is a realistic shape for something sitting next to a CRM.
#
# Build with `--build-arg PRELOAD_EMBEDDER=false` to skip it and mount
# HF_HOME as a volume instead.
ARG PRELOAD_EMBEDDER=true
RUN if [ "$PRELOAD_EMBEDDER" = "true" ]; then \
        python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')" ; \
    fi

# [claude] Offline exactly when the model is already here.
#
# With the weights baked in but the hub still reachable in principle,
# `huggingface_hub` checks for a newer revision on every load — and with no
# route out it does that as five retries with backoff before falling back
# to the cache. Measured on a `--network none` container: about forty
# seconds of retries, on every first embed of every container start, to
# arrive at the file that was already on disk.
#
# The value tracks the build arg, and `_is_true` reads "false" as false —
# so an image built without the preload keeps the hub reachable, which it
# has to, because that image downloads the model on first use.
ENV HF_HUB_OFFLINE=${PRELOAD_EMBEDDER} \
    TRANSFORMERS_OFFLINE=${PRELOAD_EMBEDDER}

EXPOSE 8000

# [claude] Readiness, not liveness. `/health` answers while the process is
# up; `/health/ready` answers only when the database, the checkpointer and
# the model endpoint are all reachable — and fails on an unsafe RLS posture.
# An orchestrator should not send traffic to a replica that cannot answer.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/ready || exit 1

# [claude] uvicorn directly rather than `python main.py`.
#
# main.py's own entry point turns on the reloader in development, which is
# right there and wrong here. Workers are safe because conversations live in
# PostgreSQL rather than in the process — see main.py's note; with the
# in-memory checkpointer they would not be, and `/health/ready` reports that
# as not-ready rather than letting it look fine.
CMD ["sh", "-c", "exec uvicorn main:app \
    --host 0.0.0.0 --port 8000 \
    --workers ${UVICORN_WORKERS:-2} \
    --no-access-log"]
