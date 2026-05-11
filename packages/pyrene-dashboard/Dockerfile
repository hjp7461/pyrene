FROM python:3.13-slim

WORKDIR /app

# Install uv for dependency management
RUN pip install --no-cache-dir uv

# Copy only the dashboard package files
COPY packages/pyrene-dashboard/pyproject.toml /app/pyproject.toml
COPY packages/pyrene-dashboard/src /app/src

# Install the package and its dependencies
RUN uv pip install --system --no-cache "streamlit>=1.36" "httpx>=0.27" "cachetools>=5.3" "pendulum>=3.0" && \
    uv pip install --system --no-cache -e /app

EXPOSE 8501

# Streamlit configuration: disable telemetry, set server settings
ENV STREAMLIT_SERVER_PORT=8501
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "src/pyrene_dashboard/app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true"]
