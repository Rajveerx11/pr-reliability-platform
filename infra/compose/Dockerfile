FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

COPY --from=ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY pyproject.toml uv.lock ./
COPY migrations/ migrations/
COPY apps/ apps/
COPY packages/ packages/
COPY workers/ workers/

RUN uv sync --frozen --no-dev --no-editable

USER app

CMD ["uvicorn", "--factory", "pr_reliability_api.app:create_app_from_environment", "--host", "0.0.0.0", "--port", "8000"]
