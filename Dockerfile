# syntax=docker/dockerfile:1
# Agency Audit — production Docker image for the web dashboard.
#
# Build:
#   docker build -t agency-audit .
# Run:
#   docker run -p 8000:8000 --env-file .env agency-audit

FROM python:3.12-slim

# Keep Python from writing .pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (layer caching)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy the application source
COPY src/ src/

# The dashboard is the default command
EXPOSE 8000
CMD ["uv", "run", "--no-dev", "agency-audit", "serve", "--host", "0.0.0.0", "--port", "8000"]

# Verify the service is ready via the health endpoint.
# Docker will mark the container unhealthy if this fails 3 times in a row.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c \
  "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
  || exit 1
