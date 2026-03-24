# ── Stage 1: dependency layer ─────────────────────────────────────────────────
FROM python:3.11-slim AS base

WORKDIR /app

# System dependencies required by Playwright / Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
        # Playwright install-deps covers these, but listing for clarity
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium and all its system deps in one step
RUN playwright install --with-deps chromium


# ── Stage 2: application ───────────────────────────────────────────────────────
FROM base AS app

WORKDIR /app

# Copy application code (secrets are excluded via .dockerignore)
COPY . .

# Data directory for runtime-mounted volumes
RUN mkdir -p /data

EXPOSE 5000

# Healthcheck — hits the lightweight /api/status endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fs http://localhost:5000/api/status || exit 1

CMD ["python", "app.py"]
