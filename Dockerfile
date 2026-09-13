FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LOCKED=1

# onnxruntime (via fastembed) needs libgomp at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY memos ./memos
COPY skills ./skills
COPY README.md AGENT_INSTRUCTIONS.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

ENV MEMOS_DATA=/data \
    MEMOS_HOST=0.0.0.0 \
    MEMOS_PORT=8765

VOLUME ["/data"]
EXPOSE 8765

ENTRYPOINT ["uv", "run", "--no-sync", "memos"]
CMD ["--host", "0.0.0.0", "--port", "8765"]
