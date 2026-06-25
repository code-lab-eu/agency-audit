# Multi-stage Docker build for agency-audit
# Stage 1: builder — installs Python deps + packages the project, then discarded
FROM python:3.14-slim AS builder

# Install uv for fast, deterministic installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Install build tools: some packages may not have pre-built wheels
# for Python 3.14 yet and need compilation from source (selectolax,
# cryptography, greenlet, asyncpg, pydantic-core, etc.).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project files needed for installation
COPY pyproject.toml ./
COPY requirements.txt ./
COPY src/ ./src/
COPY scoring_config.yaml ./

# Create a virtualenv and install the project with pinned dependencies.
# Install hatchling first so we can use --no-build-isolation — avoids uv
# creating a separate build venv that may fail on slim base images.
RUN uv venv /opt/venv \
    && uv pip install --no-cache hatchling \
    && uv pip install --no-cache -r requirements.txt \
    && uv pip install --no-cache --no-build-isolation .

# Stage 2: minimal runtime image
FROM python:3.14-slim

# Create non-root app user
RUN groupadd --system app && useradd --system --gid app --create-home app

# Copy virtualenv from builder (includes agency-audit + all deps)
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=app:app /build/scoring_config.yaml /app/scoring_config.yaml

# Install system libraries required by Playwright Chromium, plus curl for healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libglib2.0-0 \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libdbus-1-3 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
        libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright Chromium browser
RUN /opt/venv/bin/playwright install chromium \
    && /opt/venv/bin/playwright install-deps chromium

# Set up environment
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Switch to non-root user
USER app

# Default command: start the web dashboard
CMD ["agency-audit", "serve", "--host", "0.0.0.0", "--port", "8000"]
