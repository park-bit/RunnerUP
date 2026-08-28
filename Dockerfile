# syntax=docker/dockerfile:1
#
# Lightweight image for the Discord Python runner.
# Only needed if you deploy via Docker (Render can also build this repo with its
# native Python runtime - see render.yaml). Local runs:
#
#   docker build -t discord-python-runner .
#   docker run --rm -e DISCORD_TOKEN=xxx -e PORT=10000 -p 10000:10000 discord-python-runner

FROM python:3.12-slim

# No .pyc files, unbuffered stdio, and a quiet, cache-free pip.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Create an unprivileged user up front.
RUN useradd --create-home --uid 10001 appuser

# Install dependencies first so this layer caches independently of app changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the application package (see .dockerignore for exclusions).
COPY app ./app

# Drop root.
RUN chown -R appuser:appuser /app
USER appuser

# Render injects $PORT at runtime; default to 10000 for local runs.
ENV PORT=10000
EXPOSE 10000

# Bot + health server share one process/event loop.
CMD ["python", "-m", "app.main"]
