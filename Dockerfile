# SpaceMatch Reel Analytics - FastAPI + Playwright (Chromium) in one image.
# Render: create the web service with Runtime = Docker; it sets $PORT.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Python packages, then Chromium + the system libraries it needs (needs root, so done at build time).
COPY requirements.txt .
RUN apt-get update \
 && apt-get install -y --no-install-recommends libatomic1 \
 && pip install -r requirements.txt \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY . .

# Run as an unprivileged user.
RUN useradd --create-home app && chown -R app:app /app /ms-playwright
USER app

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
