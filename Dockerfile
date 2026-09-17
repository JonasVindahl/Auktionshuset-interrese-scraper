# syntax=docker/dockerfile:1

# Base-imaget er pinnet til et digest, saa to builds af samme commit giver samme
# fundament. Tag og dato staar her, saa det er tydeligt hvad der skal opdateres:
#   python:3.13-slim (2026-09-02)
# Se DEPLOYMENT.md, "Opgradering", for hvordan digestet opdateres.
ARG BASE_IMAGE=python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
FROM ${BASE_IMAGE}

# Version og revision kommer fra CI eller fra et lokalt build, saa et image kan
# spores tilbage til et commit. Standarderne er aerlige om at vaerdien mangler.
ARG VERSION=0.0.0
ARG REVISION=unknown
ARG CREATED=unknown

LABEL org.opencontainers.image.title="Auktionshuset Hunter" \
      org.opencontainers.image.description="Overvåger auktionshuset.dk for interessante fund og sender dem til Discord." \
      org.opencontainers.image.source="https://github.com/JonasVindahl/Auktionshuset-interrese-scraper" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.licenses="proprietary"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    CONFIG_PATH=/app/config/interests.yml \
    DB_PATH=/app/data/auction_hunter.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Kør som ikke-root. UID/GID kan sættes ved build, så volumenet på værten
# får de rigtige ejerskabsforhold.
ARG UID=10001
ARG GID=10001
RUN groupadd --gid ${GID} hunter \
 && useradd --uid ${UID} --gid ${GID} --create-home hunter

COPY src/ ./src/
COPY config/ ./config/

RUN mkdir -p /app/data /app/reports && chown -R hunter:hunter /app

USER hunter

EXPOSE 8080

# Ingen healthcheck der rammer netværket — auktionshusets vilkår tillader
# kun ét scrape hvert 15. minut, og en healthcheck ville tælle som trafik.
# Web-tjenesten har sin egen healthcheck i docker-compose.yml.
CMD ["python", "-m", "auction_hunter", "run"]
