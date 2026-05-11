# PLAN-018 Day 1 — unified pyrene-api container.
#
# Composes all Phase 2 backend routers into a single FastAPI app:
#   auth, agents, gateway, rbac, data-rbac, metering, audit, budget,
#   mcp-tools. The entrypoint is `deploy/api/app.py:app`.
#
# We use the same uv workspace layout as the dashboard image so the build is
# reproducible from a clean checkout.
FROM python:3.13-slim

WORKDIR /app
RUN pip install --no-cache-dir uv

# Copy workspace metadata + package sources.
COPY pyproject.toml /app/pyproject.toml
COPY uv.lock /app/uv.lock
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY packages /app/packages
COPY deploy/api/app.py /app/app.py

# Install workspace (editable) so every pyrene-* package is importable.
RUN uv sync --frozen --no-dev

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=12 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/health', timeout=2); sys.exit(0)" || exit 1

CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
