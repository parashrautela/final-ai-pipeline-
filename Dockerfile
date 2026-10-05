# ── Stage 1: dependency builder ──────────────────────────────────────────────
# Installs all Python packages into an isolated prefix so the final image
# contains no pip, wheel, or build artefacts.
FROM python:3.11-slim AS builder

WORKDIR /build

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: production runtime ───────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Security: non-root user with explicit UID (predictable in k8s / cloud run)
RUN useradd --create-home --shell /bin/bash --uid 1001 appuser

WORKDIR /app

# Pull compiled packages from the builder stage — no compiler tools stay behind
COPY --from=builder /install /usr/local

# Only ship the application package; no .env, .venv, migrations, or dev files
COPY --chown=appuser:appuser app/ ./app/

# ── Runtime environment ───────────────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=random \
    # Required so `from app.xxx import` resolves from /app
    PYTHONPATH=/app \
    ENVIRONMENT=production \
    PORT=8000

USER appuser
# Bake the verified public encoder into the image; no model download during a
# customer search. The private product index lives only in the server cache.
ENV IMAGE_SEARCH_MODEL_PATH=/home/appuser/.cache/jewel-image-search/vision.onnx
RUN python -m app.services.catalogue_search
EXPOSE 8000

# Lightweight health check using stdlib urllib — no extra binaries needed.
# /health returns {"status":"ok"} and is rate-limited at 30/min (safe to poll).
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
    || exit 1

# Single worker only — app.worker.worker_loop() runs inside the same event loop.
# Multiple uvicorn workers would spawn duplicate background pipeline workers
# and cause duplicate Supabase jobs and wasted AI API calls.
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--loop", "asyncio", \
     "--log-level", "info", \
     "--no-access-log"]
# Bake the verified foreground model alongside the image encoder.
RUN python -m app.services.jewellery_subject
