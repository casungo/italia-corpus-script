FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --user-group --create-home --shell /usr/sbin/nologin corpus

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY coverage-requirements.json quality-exceptions.json ./
COPY src/ src/
COPY --chmod=755 docker-entrypoint.sh .

# Il contesto di build può contenere file a 600; l'utente di runtime deve poter leggere codice,
# policy di qualità e coverage. a+rX dà lettura universale senza rendere eseguibili i sorgenti.
RUN chmod -R a+rX /app \
    && mkdir -p /data/work /data/download-cache \
    && chown -R corpus:corpus /data

ENV PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1

USER corpus

ENTRYPOINT ["/app/docker-entrypoint.sh"]
