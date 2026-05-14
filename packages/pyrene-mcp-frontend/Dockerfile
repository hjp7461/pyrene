FROM python:3.13-slim

WORKDIR /app

# Install uv for dependency management.
RUN pip install --no-cache-dir uv

# Copy only the frontend package files (ADR-019: no internal pyrene_* deps).
COPY packages/pyrene-mcp-frontend/pyproject.toml /app/pyproject.toml
COPY packages/pyrene-mcp-frontend/src /app/src

# Install runtime deps + the package itself in editable mode.
# Mirrors `pyrene-dashboard/Dockerfile` — no `uv sync --all-packages`
# needed because the frontend has zero workspace dependencies.
RUN uv pip install --system --no-cache \
        "streamlit>=1.36" "httpx>=0.27" "cachetools>=5.3" \
        "pendulum>=3.0" "jsonschema>=4.21" && \
    uv pip install --system --no-cache -e /app

EXPOSE 8502

ENV STREAMLIT_SERVER_PORT=8502
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# PRD-019 F-2: python:3.13-slim has no curl. urllib stays in stdlib.
HEALTHCHECK --interval=10s --timeout=5s --retries=6 --start-period=20s \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8502/_stcore/health', timeout=2); sys.exit(0)" || exit 1

CMD ["streamlit", "run", "src/pyrene_mcp_frontend/app.py", \
     "--server.port=8502", \
     "--server.address=0.0.0.0", \
     "--server.headless=true"]
