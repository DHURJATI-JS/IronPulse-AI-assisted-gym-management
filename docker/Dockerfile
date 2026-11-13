# syntax=docker/dockerfile:1
# IronPulse web app image (python:3.12-slim + Tesseract OCR). No secrets are baked in: configuration comes from
# environment variables at run time (docker/compose.yml passes ../.env via env_file).
#
#   docker compose --env-file .env -f docker/compose.yml --profile app up -d --build
#   docker build -f docker/Dockerfile -t ironpulse-app .          (build context = the project folder)

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Tesseract OCR (English) for ID-card cross-validation; curl-free image (healthcheck uses Python)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# unprivileged runtime user
RUN groupadd --system ironpulse \
    && useradd --system --gid ironpulse --home-dir /app --no-create-home --shell /usr/sbin/nologin ironpulse

WORKDIR /app

# dependencies first (better layer caching)
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

# application code (read-only for the app user); only var/ (uploads, mail spool, logs) is writable.
# An empty .env stops app/__main__.py from generating one - real settings come from the environment.
COPY . .
RUN mkdir -p /app/var \
    && touch /app/.env \
    && chown -R ironpulse:ironpulse /app/var

ENV HOST=0.0.0.0 \
    PORT=10000 \
    TESSERACT_CMD=/usr/bin/tesseract

USER ironpulse
EXPOSE 10000
VOLUME ["/app/var"]

# GET /healthz from inside the container (docker/healthcheck.py, standard library only). It sends X-Forwarded-Proto:
# https from 127.0.0.1 (in the default TRUSTED_PROXIES) and the first ALLOWED_HOSTS name as Host, so it also passes
# with ENVIRONMENT=production, where FORCE_HTTPS (on by default) would answer plain HTTP with a 308.
HEALTHCHECK --interval=30s --timeout=6s --start-period=60s --retries=3 \
    CMD ["python", "docker/healthcheck.py"]

CMD ["python", "-m", "app"]
