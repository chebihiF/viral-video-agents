# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.5-python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
COPY apps/dashboard apps/dashboard
RUN uv sync --frozen --no-dev --package dashboard --no-editable

FROM python:3.12-slim-bookworm
RUN useradd --system --uid 10001 app && install -d -o app -g app /data
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} VVA_DB_PATH=/data/vva.db
VOLUME /data
USER app
EXPOSE 8000
# --start-interval (Docker >= 25) accélère la détection au démarrage ; ignoré par les versions plus anciennes.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --start-interval=1s CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz').status!=200)"
CMD ["uvicorn", "dashboard.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
