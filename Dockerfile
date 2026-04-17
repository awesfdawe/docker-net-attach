FROM python:3.14-alpine AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev \
    && find /app/.venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; true

FROM python:3.14-alpine

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

COPY --from=builder /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH"

ENV PYTHONUNBUFFERED=1
ENV DOCKER_SOCKET=unix:///var/run/docker.sock
ENV LABEL_PREFIX=docker-net-attach
ENV DRY_RUN=false

CMD ["python", "-m", "src.main"]