# syntax=docker/dockerfile:1.7

# ─────────────────────────────────────────────────────────────────────
# Stage 1 — Build the Svelte frontend
# ─────────────────────────────────────────────────────────────────────
FROM node:20-slim AS frontend

WORKDIR /app/frontend-svelte

# Install deps separately so this layer caches when only source changes
COPY frontend-svelte/package.json frontend-svelte/package-lock.json ./
RUN npm ci

# Build outputs to /app/frontend-dist (per vite.config.js: outDir: '../frontend-dist')
COPY frontend-svelte/ ./
RUN npm run build


# ─────────────────────────────────────────────────────────────────────
# Stage 2 — Install Python dependencies into an isolated venv
# ─────────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS python-build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential covers cffi/cryptography source builds when wheels are missing
# (mostly relevant on linux/arm64); pruned in the runtime stage anyway.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Bring in metadata + source for an editable-style install (hatchling reads src/)
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install into an isolated venv — easy to copy into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# NOTE: the `web` extra (camoufox) is intentionally omitted.
# Camoufox needs Firefox runtime libs (libgtk-3-0, libx11-xcb1, libasound2, …)
# AND a `python -m camoufox fetch` browser download (~200MB) to actually work.
# A future `pinkybot:web` variant can layer those in. v1 keeps the image lean
# and assumes web scraping runs out-of-container (e.g. via the shared pinky-web
# MCP service) when needed.
RUN pip install --upgrade pip \
    && pip install ".[telegram,discord,slack,google,calendar,voice]"


# ─────────────────────────────────────────────────────────────────────
# Stage 3 — Lean runtime image
# ─────────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PINKYBOT_CHANNEL=stable

# curl needed for HEALTHCHECK; ca-certificates for outbound TLS
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user
RUN groupadd --system pinky \
    && useradd --system --gid pinky --shell /bin/bash --home /home/pinky --create-home pinky

WORKDIR /app

# Bring in the prebuilt venv
COPY --from=python-build --chown=pinky:pinky /opt/venv /opt/venv

# Application source + built frontend assets
COPY --chown=pinky:pinky src/ ./src/
COPY --chown=pinky:pinky pyproject.toml README.md ./
COPY --from=frontend --chown=pinky:pinky /app/frontend-dist ./frontend-dist

# Persistent state lives at /app/data — mount a volume here.
# The daemon's default working_dir is "." so `/app/data/agents/<name>/...`
# matches the layout used in non-Docker deployments.
RUN mkdir -p /app/data && chown -R pinky:pinky /app/data
VOLUME ["/app/data"]

USER pinky

EXPOSE 8888

# Liveness probe — the SPA root returns 200 once the API server is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent --show-error http://localhost:8888/ -o /dev/null || exit 1

ENTRYPOINT ["python", "-m", "pinky_daemon"]
CMD ["--mode", "api", "--host", "0.0.0.0", "--port", "8888"]
