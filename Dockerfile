# Both base images are pinned by the digest of their multi-arch index (linux/amd64 and
# linux/arm64 resolve from the same line). Bump the digests and the tag together, on purpose;
# the image scan in CI fails when a pinned base has a fixable HIGH or CRITICAL.
FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS builder

COPY --from=ghcr.io/astral-sh/uv:0.8.13@sha256:4de5495181a281bc744845b9579acf7b221d6791f99bcc211b9ec13f417c2853 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first so this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-editable --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable


FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ARG VERSION
ARG VCS_REF
ARG BUILD_DATE

# Version and revision are build arguments: an image built without them is refused, never labelled
# "unknown".
RUN test -n "${VERSION}" && test -n "${VCS_REF}" && test -n "${BUILD_DATE}"

LABEL org.opencontainers.image.title="mcp-gateway" \
      org.opencontainers.image.description="OAuth resource server and streaming reverse proxy for MCP servers that swaps the caller's identity for a per-user API key" \
      org.opencontainers.image.source="https://github.com/alien-intelligence/mcp-gateway" \
      org.opencontainers.image.vendor="Alien Intelligence" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# Security updates published since the pinned base was built, then the package manager caches go.
# The pip that ships with the base is not needed at runtime: the application is a prebuilt venv.
RUN apt-get update \
    && apt-get upgrade --yes --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall --yes pip \
    && useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin gateway

# Owned by root: the runtime user (1000) can read and run the app but not modify its code.
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The gateway writes nothing to disk (state is in memory or Redis, logs go to stdout), so the
# container runs with a read-only root filesystem and no capabilities:
#   docker run --read-only --cap-drop ALL --security-opt no-new-privileges ...
USER 1000:1000
EXPOSE 8000

# A real HTTP GET of /healthz on the configured listen port.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import json, os, urllib.request; port = json.loads(os.environ['MCP_GATEWAY_CONFIG'])['listen']['port']; urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3).read()"]

CMD ["mcp-gateway"]
