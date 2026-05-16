FROM python:3.12-slim AS base

WORKDIR /app

# Install curl for healthcheck/debugging
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# Copy source code
COPY src/ src/

# Start bot worker
CMD ["python", "-m", "src.main"]
