# Multi-stage Dockerfile for Agent Orchestration Platform
#
# Builds architecture-specific images for linux/amd64 and linux/arm64.
# The release workflow verifies each arch digest before manifest promotion.
#
# /bounty $5000

# 鈹€鈹€鈹€ Stage 1: Base Python 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 鈹€鈹€鈹€ Stage 2: Builder (uv-based) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
FROM base AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install dependencies first (caching)
COPY pyproject.toml Makefile ./
RUN uv sync --no-dev --frozen

# Copy source
COPY src/ src/

# 鈹€鈹€鈹€ Stage 3: Runtime 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
FROM base AS runtime

RUN addgroup --system --gid 1001 ao && \
    adduser --system --uid 1001 ao --ingroup ao

COPY --from=builder --chown=ao:ao /app /app

USER ao
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AO_HOME=/app

ENTRYPOINT ["uvicorn", "src.api.server:create_app", "--host", "0.0.0.0", "--port", "8000"]
