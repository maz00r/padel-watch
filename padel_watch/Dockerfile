ARG BUILD_FROM=python:3.12-alpine
FROM ${BUILD_FROM}

# tzdata: potrzebne dla stref czasowych (zoneinfo) — przeliczanie na Europe/Warsaw.
RUN apk add --no-cache tzdata

COPY check_padel.py /app/check_padel.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
