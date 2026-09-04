# deploy/Dockerfile
# Two-stage app image.
# - builder: uv installs the dependencies and the project into /app/.venv, with build tools
#   present so any package without a 3.14 wheel can still compile from sdist.
# - runtime: the venv plus WeasyPrint's system libraries. No uv, no compilers.
#
# The base is Debian slim, NOT Alpine: pyogrio (GIS import) publishes manylinux wheels only,
# and on musl it would fall back to a source build against a GDAL that is not there.

# ---- builder ----
FROM python:3.14-slim-bookworm AS builder

# Pinned, not :latest — a floating tag would let a breaking uv release land mid-deploy on
# the server with no code change to point at. Bump deliberately when there is a reason to.
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so a code change never invalidates the dependency layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# ---- runtime ----
FROM python:3.14-slim-bookworm

# WeasyPrint dlopens these at import time; they are system libraries, not Python packages,
# and `uv sync` does not install them. Without them `import weasyprint` raises
# OSError: cannot load library 'libgobject-2.0-0' and permits cannot be issued at all.
# libharfbuzz-subset0 is nominally optional, but its absence makes WeasyPrint embed
# DIFFERENT font bytes than CI does — PDFs would disagree between environments.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

COPY --from=builder /app/.venv /app/.venv
# The source tree travels into the image (rather than being mounted at run time) so that
# `alembic upgrade head` finds migrations/versions and the permit template finds its fonts
# in app/modules/permits/assets/ — both are read from disk at run time.
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

EXPOSE 8000

# app/main.py exposes create_app() only — no module-level `app` object (see the
# Makefile's own dev command) — so uvicorn needs --factory pointed at it.
#
# --proxy-headers makes the client IP reach the audit trail and makes request.base_url
# render https; without it every same-origin mutation is rejected with ERR-AUTH-006.
# --forwarded-allow-ips is set to the proxy's exact address by the compose file.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
