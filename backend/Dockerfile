# ================================================================
# AIMScribe Backend - Render.com Deployment (FREE)
# Runs API + Worker in single container
# ================================================================

FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Create temp directory
RUN mkdir -p /tmp

# Render uses PORT env var (default 10000)
ENV PORT=10000

# Expose port
EXPOSE ${PORT}

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Start supervisor
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
