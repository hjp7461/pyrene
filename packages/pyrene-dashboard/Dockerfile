FROM python:3.13-slim

WORKDIR /app

# Install uv for dependency management
RUN pip install --no-cache-dir uv

# Copy the dashboard package + the single allowed workspace dep.
# ADR-025/F-20: pyrene-ui-common is a leaf-utility (httpx/streamlit only,
# zero domain dep) — the sole permitted pyrene_* dependency. Domain
# packages (gateway/core/...) are still NOT copied (ADR-019 boundary).
COPY packages/pyrene-dashboard/pyproject.toml /app/pyproject.toml
COPY packages/pyrene-dashboard/src /app/src
COPY packages/pyrene-ui-common /app/pyrene-ui-common

# Install runtime deps, then the leaf-utility first (satisfies the
# `-e /app` resolution of `pyrene-ui-common`), then the package itself.
RUN uv pip install --system --no-cache "streamlit>=1.36" "httpx>=0.27" "cachetools>=5.3" "pendulum>=3.0" && \
    uv pip install --system --no-cache /app/pyrene-ui-common && \
    uv pip install --system --no-cache -e /app

EXPOSE 8501

# Streamlit configuration: disable telemetry, set server settings
ENV STREAMLIT_SERVER_PORT=8501
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# PRD-019 F-2 / PRD-054 F-3: python:3.13-slim has no curl — use stdlib urllib
# (mirrors pyrene-mcp-frontend/Dockerfile).
HEALTHCHECK --interval=10s --timeout=5s --retries=6 --start-period=20s \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=2); sys.exit(0)" || exit 1

CMD ["streamlit", "run", "src/pyrene_dashboard/app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true"]
