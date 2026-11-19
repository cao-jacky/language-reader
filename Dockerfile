FROM python:3.13-slim

LABEL org.opencontainers.image.title="Language Reader" \
      org.opencontainers.image.description="Self-hosted reader for learning Russian and Finnish"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    PORT=8080 \
    HOST=0.0.0.0

WORKDIR /app

# Voikko gives offline Finnish morphology hints. It is small (~10MB) but
# optional: the app runs without it.
ARG WITH_VOIKKO=1
RUN if [ "$WITH_VOIKKO" = "1" ]; then \
      apt-get update && \
      apt-get install -y --no-install-recommends libvoikko1 voikko-fi && \
      rm -rf /var/lib/apt/lists/*; \
    fi

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ "$WITH_VOIKKO" = "1" ]; then pip install --no-cache-dir "libvoikko>=4.3"; fi

COPY language_reader/ ./language_reader/

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4).status==200 else 1)"

CMD ["python", "-m", "language_reader"]
