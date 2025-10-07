# ---- Stage 1: Builder ----
FROM python:3.12-slim AS builder

# Set workdir
WORKDIR /app

# Install build dependencies (for compiling wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc \
 && rm -rf /var/lib/apt/lists/*

# Copy only dependency list first (leverage Docker layer cache)
COPY requirements.txt .

# Install dependencies into a clean folder (not system-wide)
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt -t /app/deps

# ---- Stage 2: Runtime ----
FROM python:3.12-slim

WORKDIR /app

# Copy only runtime deps and app
COPY --from=builder /app/deps /usr/local/lib/python3.12/site-packages
COPY app ./app

# Set environment variables for production
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8001

# Expose FastAPI port
EXPOSE 8001

# Run FastAPI with uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]

