ARG BUILD_FROM=alpine:3.20
FROM ${BUILD_FROM}

# Chromium + wirtualny ekran (Xvfb) + VNC + noVNC (panel w przeglądarce przez Ingress).
# python3 czyta token z Chromium przez CDP; websocket-client bo stdlib nie ma klienta ws.
RUN apk add --no-cache \
      chromium \
      xvfb \
      x11vnc \
      novnc \
      websockify \
      python3 \
      py3-pip \
      tzdata \
      ttf-dejavu \
    && pip3 install --no-cache-dir --break-system-packages websocket-client

COPY read_token.py /app/read_token.py
COPY run.sh /run.sh
# Strona startowa panelu (Ingress ładuje '/'): autopołączenie noVNC ścieżkami względnymi.
COPY index.html /usr/share/novnc/index.html
RUN chmod a+x /run.sh

ENV CHROME_PROFILE=/data/chrome-profile \
    DISPLAY=:1 \
    PYTHONUNBUFFERED=1

CMD [ "/run.sh" ]
