ARG BUILD_FROM=python:3.12-alpine
FROM ${BUILD_FROM}

# python3: HA buduje na swoim bazowym obrazie (alpine+s6), który NIE ma pythona,
# więc instalujemy go jawnie. tzdata: strefy czasowe (zoneinfo -> Europe/Warsaw).
RUN apk add --no-cache python3 tzdata

COPY check_padel.py /app/check_padel.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
