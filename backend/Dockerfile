FROM python:3.12-slim

WORKDIR /app

# Unbuffered logs, so the worker's output shows up in docker/CloudWatch at once
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8000/healthz || exit 1

# Default: the API service. The worker service uses the same image with
# `python -m app.worker` (docker-compose `command`, ECS task definition `command`).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
