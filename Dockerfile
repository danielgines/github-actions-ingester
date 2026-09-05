# syntax=docker/dockerfile:1.7
#
# github-actions-ingester — multi-stage build.
#
#   Stage 1 (builder): use uv to build the wheel from pyproject + src.
#   Stage 2 (runtime): python:3.13-slim-trixie with the wheel + non-root user.
#
# Builder and runtime share the same Python series so any native build
# step (none today — the wheel is pure Python, psycopg ships a binary
# wheel) cannot surface an ABI mismatch between stages.

FROM ghcr.io/astral-sh/uv:0.12-python3.13-trixie-slim AS builder

WORKDIR /build

# Only what the wheel needs; everything else is excluded by .dockerignore.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN uv build --wheel --out-dir /build/dist

# ---------------------------------------------------------------------------

FROM python:3.14-slim-trixie AS runtime

# OCI labels — identity in registry browsers without pulling the README.
# GHA_VERSION / GHA_REVISION are set by CI (`--build-arg GHA_VERSION=v0.1.0
# --build-arg GHA_REVISION=$GITHUB_SHA`) and default to "dev" locally.
ARG GHA_VERSION=dev
ARG GHA_REVISION=""
LABEL org.opencontainers.image.title="github-actions-ingester" \
      org.opencontainers.image.description="Standalone GitHub Actions ingester: workflows, runs and jobs from the GitHub API into PostgreSQL for Grafana dashboards, FinOps and alerting. Prometheus /metrics for ingester health and scheduled-workflow liveness." \
      org.opencontainers.image.url="https://github.com/danielgines/github-actions-ingester" \
      org.opencontainers.image.source="https://github.com/danielgines/github-actions-ingester" \
      org.opencontainers.image.documentation="https://github.com/danielgines/github-actions-ingester/blob/main/README.md" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="Daniel Gines" \
      org.opencontainers.image.version="${GHA_VERSION}" \
      org.opencontainers.image.revision="${GHA_REVISION}"

# Security patches the base image was not rebuilt against yet, then the
# runtime deps: tini forwards SIGTERM to the process (stdlib http.server
# does not trap it on its own); ca-certificates lets httpx verify
# api.github.com and psycopg verify the database when sslmode=verify-full.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends tini ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Non-root user. The ingester writes nothing to disk, so the root
# filesystem can be mounted read-only (the chart does that).
RUN groupadd --system --gid 1000 ingester \
    && useradd --system --uid 1000 --gid 1000 --create-home --shell /sbin/nologin ingester

# Install the wheel, then remove pip: it is needed exactly once and its
# CVEs would otherwise show up in every image scan.
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl \
    && rm /tmp/*.whl \
    && pip uninstall -y pip setuptools \
    && rm -rf /root/.cache/pip \
    && PYSP=$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])') \
    && rm -rf "${PYSP}/pip"* "${PYSP}/setuptools"*

USER ingester
WORKDIR /home/ingester

EXPOSE 9619

ENV PYTHONUNBUFFERED=1 \
    GHA_LISTEN_HOST=0.0.0.0 \
    GHA_LISTEN_PORT=9619 \
    GHA_LOG_FORMAT=json

# /healthz answers as soon as the HTTP server is up; readiness (/readyz)
# is the one that waits for the first successful cycle.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9619/healthz', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["github-actions-ingester", "run"]
