FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY coverage-requirements.json quality-exceptions.json ./
COPY src/ src/
COPY docker-entrypoint.sh .

RUN chmod 755 docker-entrypoint.sh \
    && mkdir -p /data/work /data/download-cache

ENV PYTHONPATH=/app/src

ENTRYPOINT ["/app/docker-entrypoint.sh"]
