# Stage 1: build — install packages and compile C extensions (gcc, libpq-dev).
# Nothing from this stage except the installed packages reaches the runtime image.
FROM python:3.12-slim AS builder

# git is required by pip to clone the circle-titanoboa-sdk (circlekit) VCS
# dependency (git+https://…) added for the marketplace x402 payments (PR #958).
# CI runners already have git; this slim builder does not, so the first deploy
# carrying that dependency failed at `pip install` until git was added here.
#
# gcc + libc6-dev + libpq-dev are load-bearing as of issue #1632: `psycopg2` is
# now the source distribution rather than `psycopg2-binary`, so pip COMPILES it
# here. Each package is doing a distinct job — none is removable:
#   gcc       — the compiler itself (was already here for other C extensions).
#   libc6-dev — the C standard-library HEADERS. python:3.12-slim ships gcc-able
#               binaries but no libc headers, so `apt-get install gcc` alone
#               gets you a compiler that cannot compile: the first psycopg2 wheel
#               build failed with `Python.h:23:12: fatal error: stdlib.h: No such
#               file or directory` (observed 2026-09-01, verified locally).
#               `build-essential` also works and is the usual incantation; this
#               is the same thing minus g++/make/dpkg-dev, which nothing here
#               needs. If a future sdist wants C++ or make, widen it to
#               build-essential rather than debugging a confusing header error.
#   libpq-dev — `pg_config` + the libpq headers psycopg2 links against; without
#               it pip fails with "pg_config executable not found".
# None of this reaches the runtime image — that is what the second stage is for.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev libpq-dev git \
    && rm -rf /var/lib/apt/lists/*

# Build context is the REPO ROOT (not ./backend) as of issue #1039 — see the
# runtime COPYs below for why. Paths are therefore repo-relative.
# requirements.txt is an `-r` include of the shared base plus the CPU-only torch
# block (issue #1522), so BOTH files must land here or pip fails at build time
# with "Could not open requirements file". Keep them in one COPY.
COPY backend/requirements.txt backend/requirements-base.txt ./
RUN pip install --no-cache-dir --target=/deps -r requirements.txt

# Stage 2: runtime — python:3.12-slim without build tools, running as nonroot.
# Distroless is not used here: psycopg2 is a Python 3.12 C extension and
# gcr.io/distroless/python3-debian12 ships Python 3.11 (Debian 12 default).
FROM python:3.12-slim

# libpq5 is REQUIRED at runtime (issue #1632). The source-built psycopg2 links
# libpq dynamically instead of vendoring it the way psycopg2-binary did, and
# python:3.12-slim carries no libpq — without this the image imports fine
# through the builder stage and then dies at runtime with
# "libpq.so.5: cannot open shared object file".
#
# Size: near-neutral, not a regression. libpq5 + its transitive TLS/auth deps
# add ~5 MB to the runtime layer, and the psycopg2 wheel shrinks by roughly the
# same amount now that it no longer vendors libpq + a second libssl/libcrypto.
# That second OpenSSL is the whole point of the swap — see requirements-base.txt.
#
# -dev is deliberately NOT installed here: the runtime needs the shared library,
# not the headers, and keeping build tooling out of the serving image is why the
# builder stage exists at all.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --no-create-home --uid 1001 nonroot

COPY --from=builder /deps /deps

# Self-contained image (issue #1039). On Fargate there are NO host volumes, so
# everything docker-compose used to supply as bind mounts must be baked in.
# The build context is the repo root (deploy.yml / docker-compose.yml pass `.`).
#   - backend/                     → /app                (the FastAPI package;
#                                     includes services/gmm_model.pkl, now committed)
#   - analytics-engine/strategies/ → the curated strategy library (provider reads
#                                     these files; without them list_strategies()==0
#                                     → risk_data=mock + empty library)
#   - analytics-engine/src/        → fetch_ohlcv etc.; without it real-data
#                                     backtests fall back to synthetic
#   - analytics-engine/artifacts/  → backtest artifact JSONs (seed source)
#   - data/corpus/                 → corpus manifest
#   - contracts/abis/              → on-chain ABIs (chain client reads /contracts/abis)
COPY backend /app
COPY analytics-engine/strategies /app/analytics-engine/strategies
COPY analytics-engine/src /app/analytics-engine/src
COPY analytics-engine/artifacts /app/analytics-engine/artifacts
COPY data/corpus /app/data/corpus
COPY contracts/abis /contracts/abis

# Pre-create rw volume mount points with nonroot ownership.
# Docker copies these dirs into named volumes on first initialisation, so the
# volume inherits nonroot ownership rather than the default root:root.
# Paths mirror docker-compose.yml: corpus-artifact is mounted at both locations.
#
# /home/nonroot MUST exist and be writable: circlekit → boa → vvm (the Vyper
# version manager, pulled in by the marketplace x402 payments, PR #958) runs
# `Path.home()/".vvm".mkdir()` at IMPORT time with no parents=True, so a missing
# home dir raises FileNotFoundError and crash-loops the backend (2026-07-05
# outage). useradd is --no-create-home, so we create it explicitly here.
ENV HOME=/home/nonroot
RUN mkdir -p /app/data/corpus-artifact /srv/corpus-artifact /home/nonroot/.vvm \
    && chown -R nonroot:nonroot /app/data/corpus-artifact /srv/corpus-artifact /home/nonroot

# Bake all-MiniLM-L6-v2 into the image at build time so cold starts never
# fetch from HuggingFace at runtime (offline / air-gapped containers work).
# HF_HOME points to the baked cache; HF_HUB_OFFLINE=1 tells huggingface_hub
# to use only local files at runtime — no metadata re-checks, no network.
# The download runs as root here; we chown to nonroot before USER nonroot.
# Build-time model selection: override via --build-arg MINILM_MODEL=<hf_id>
# to bake a different model. Default matches the runtime MINILM_MODEL default.
ARG MINILM_MODEL=all-MiniLM-L6-v2
ENV MINILM_MODEL=${MINILM_MODEL}
ENV HF_HOME=/app/model_cache
ENV HF_HUB_OFFLINE=1
RUN HF_HUB_OFFLINE=0 PYTHONPATH=/deps python -c \
    "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ['MINILM_MODEL'])" \
    && chown -R nonroot:nonroot /app/model_cache

# Build provenance (issue #1039): stamp the git SHA in at build time so /health
# reports it as "version" — "which code is live" in one glance. deploy.yml passes
# --build-arg GIT_SHA=${{ github.sha }}; defaults to "dev" for local builds.
ARG GIT_SHA=dev
ENV ARCHIMEDES_GIT_SHA=${GIT_SHA}

ENV PYTHONPATH=/deps:/app
WORKDIR /app

USER nonroot

EXPOSE 8000

# CONTAINER health = READINESS, not liveness (#1818 P3). /health/ready answers
# 200 while this task can still read its database and 503 once the DB-backed
# probes have been serving cached readings for longer than
# HEALTH_STALE_UNREADY_S (default 900s) — the state that sat behind a green
# /health for ten hours during the 2026-09-03 outage. urlopen raises on 503, so
# the check fails and ECS replaces THIS task; the ALB target group keeps polling
# /health (still always 200, infra/alb.tf) so a shared transient can never pull
# every target out of service at once. Mirrored in infra/ecs.tf — an image-only
# HEALTHCHECK is invisible to the ECS agent for container-dependency purposes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready')"]

# --proxy-headers + --forwarded-allow-ips="*" so request.client.host reflects the
# real client behind nginx. The container port is never bound to the host — nginx
# is the sole ingress (docker-compose backend is `expose`-only) — so trusting the
# proxy peer is safe here. The rate limiter reads X-Real-IP directly, but this
# keeps request.client.host correct for any other consumer too.
CMD ["python", "-m", "uvicorn", "archimedes.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
