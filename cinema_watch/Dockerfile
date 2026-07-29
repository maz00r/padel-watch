ARG BUILD_FROM=alpine:3.20
FROM ${BUILD_FROM}

# Silnik to czysty Python (stdlib) — bez przeglądarki, bez logowania, bez zależności.
RUN apk add --no-cache python3 tzdata

COPY check_cinema.py /app/check_cinema.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV PYTHONUNBUFFERED=1

CMD [ "/run.sh" ]
