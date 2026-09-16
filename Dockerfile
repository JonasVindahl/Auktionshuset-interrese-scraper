FROM python:3.13-slim

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

# Ingen healthcheck der rammer netværket — auktionshusets vilkår tillader
# kun ét scrape hvert 15. minut, og en healthcheck ville tælle som trafik.
CMD ["python", "-m", "auction_hunter", "run"]
