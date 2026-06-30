# Lekki obraz — skrypt używa wyłącznie biblioteki standardowej Pythona.
FROM python:3.12-slim

# Strefa czasowa wewnątrz kontenera (zoneinfo korzysta z systemowej bazy tz).
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY check_padel.py config.json ./

# Stan trzymany w /data (montowany wolumen -> przetrwa restart kontenera).
ENV STATE_DIR=/data \
    CHECK_INTERVAL=60 \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]

CMD ["python3", "check_padel.py"]
