FROM python:3.12-slim

LABEL maintainer="Deep Gori"
LABEL description="Inferra — Autonomous debugging engine with OTLP receiver"

WORKDIR /app

# Install system deps for tree-sitter compilation (if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Copy and install Python package
COPY pyproject.toml README.md ./
COPY inferra/ ./inferra/
COPY async_content_tracer/ ./async_content_tracer/
COPY report_html.py ./

RUN pip install --no-cache-dir ".[treesitter]"

# Default port for OTLP/HTTP
EXPOSE 4318

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4318/healthz')" || exit 1

# Data directory for SQLite persistence
VOLUME ["/data"]
ENV INFERRA_DB_PATH=/data/history.db

# Default: start the OTLP receiver
# Override with: docker run inferra serve --project /code
ENTRYPOINT ["inferra"]
CMD ["serve", "--port", "4318"]
