# ================================================================
# AIMScribe AI Backend - API Server Dockerfile
# ================================================================
# Multi-stage build for optimized production image

FROM python:3.11-slim as builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies. The API's own list, not the whole pipeline's:
# the transcription worker has its own image and its own requirements.txt.
COPY requirements-api.txt .
RUN pip wheel --no-cache-dir --wheel-dir /build/wheels -r requirements-api.txt

# ================================================================
# Production Stage
# ================================================================
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    libpq-dev \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user for security
RUN groupadd -r aimscribe && useradd -r -g aimscribe aimscribe

# Copy wheels from builder and install
COPY --from=builder /build/wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

# Copy application code
COPY src/ ./src/

# Set ownership
RUN chown -R aimscribe:aimscribe /app

# Switch to non-root user
USER aimscribe

# Environment variables
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PROMPTS_DIR=/app/src/prompts \
    LOG_LEVEL=INFO

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${SERVER_PORT:-6060}/health || exit 1

# Expose port
# 6060, not 6000: browsers and Node's fetch both refuse 6000.
EXPOSE 6060

# Run the FastAPI application
CMD ["python", "src/main_fastapi.py"]
