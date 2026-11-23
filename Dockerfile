# One image for api, worker-llm, worker-cpu, scheduler (beat) and the migrate job (architecture B.3).
FROM python:3.15-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
# Apply Debian security updates on top of the base image (Trivy found a fixed HIGH zlib CVE in python:3.12-slim).
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# An optional build secret "ca" lets pip trust a corporate / sandbox proxy CA; it is not kept in the image.
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    pip install -r requirements.txt
COPY pyproject.toml README.md ./
COPY masader ./masader
COPY config ./config
COPY prompts ./prompts
COPY data/sample ./data/sample
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    pip install --no-deps . \
 && useradd --system --uid 10001 --home-dir /app masader \
 && mkdir -p /data/objects && chown -R masader /data
USER masader
ENV CONFIG_DIR=/app/config PROMPTS_DIR=/app/prompts SAMPLE_DIR=/app/data/sample OBJECT_DIR=/data/objects
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz').status!=200)" || exit 1
CMD ["uvicorn", "masader.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
