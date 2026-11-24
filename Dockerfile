# UI-Build-Stage folgt mit M12 (Svelte + Vite) und wird hier als erste Stage ergänzt.
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
ARG EMBED_MODEL=intfloat/multilingual-e5-small
ENV EMBED_CACHE_DIR=/app/models EMBED_MODEL=${EMBED_MODEL}
# Modell ins Image cachen (offlinefähig); Build-Netz nötig
RUN .venv/bin/python -m smart_bridge.catalog.embedder

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'
RUN useradd --system --uid 10001 --home /app bridge \
    && mkdir -p /app/config /app/data && chown -R bridge /app/data
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/models /app/models
ENV EMBED_CACHE_DIR=/app/models PATH="/app/.venv/bin:$PATH" CONFIG_DIR=/app/config DATA_DIR=/app/data
USER bridge
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["uvicorn", "smart_bridge.api:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080"]
