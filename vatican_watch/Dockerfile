ARG BUILD_FROM=alpine:3.20
FROM ${BUILD_FROM}

# stdlib + tzdata: bez przeglądarki, konta, danych osobowych ani płatności.
RUN apk add --no-cache python3 tzdata

COPY check_vatican.py /app/check_vatican.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV PYTHONUNBUFFERED=1
CMD [ "/run.sh" ]
