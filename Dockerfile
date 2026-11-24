# UI-Build-Stage folgt mit M12 (Svelte + Vite) und wird hier als erste Stage ergänzt.
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --system --uid 10001 --home /app bridge \
    && mkdir -p /app/config /app/data && chown -R bridge /app/data
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
ENV PATH="/app/.venv/bin:$PATH" CONFIG_DIR=/app/config DATA_DIR=/app/data
USER bridge
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["uvicorn", "smart_bridge.api:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080"]
