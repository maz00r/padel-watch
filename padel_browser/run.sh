#!/usr/bin/env sh
# Startuje: wirtualny ekran -> Chromium -> VNC -> noVNC (przez Ingress) -> czytnik tokenu.
set -e

OPT=/data/options.json
opt() { python3 -c "import json;print(json.load(open('$OPT')).get('$1',''))" 2>/dev/null || true; }

START_URL="$(opt start_url)"
READ_INTERVAL="$(opt read_interval)"
# `case` zamiast `[ ] || [ ] && [ ]` — ta konstrukcja pod `set -e` potrafi ubić skrypt,
# gdy oba warunki są fałszywe (lista AND-OR kończy się statusem != 0).
case "$START_URL" in "" | None) START_URL="https://go.decathlon.pl" ;; esac
case "$READ_INTERVAL" in "" | None) READ_INTERVAL=600 ;; esac
export START_URL READ_INTERVAL

mkdir -p "$CHROME_PROFILE"

echo "[padel-browser] start: url=${START_URL} interval=${READ_INTERVAL}s profil=${CHROME_PROFILE}"

# 1) Wirtualny ekran
Xvfb :1 -screen 0 1280x900x24 -nolisten tcp &
sleep 2

# 2) Chromium z TRWAŁYM profilem (/data przeżywa restarty) + CDP do odczytu tokenu.
#    --no-sandbox jest konieczne w kontenerze bez uprawnień do user namespaces.
chromium-browser \
  --no-sandbox --disable-gpu --disable-dev-shm-usage \
  --user-data-dir="$CHROME_PROFILE" \
  --remote-debugging-port=9222 --remote-allow-origins='*' \
  --window-position=0,0 --window-size=1280,900 \
  --no-first-run --no-default-browser-check \
  "$START_URL" &
sleep 4

# 3) VNC na ekran :1 (tylko lokalnie — na zewnątrz wychodzi wyłącznie noVNC przez Ingress)
x11vnc -display :1 -forever -shared -nopw -quiet -localhost -rfbport 5900 &
sleep 1

# 4) noVNC: websockify serwuje UI i tuneluje websocket na tym samym porcie,
#    dzięki czemu Ingress (który proxuje jeden port) działa bez dodatkowej konfiguracji.
websockify --web=/usr/share/novnc 8099 localhost:5900 &
sleep 1

# 5) Czytnik tokenu (proces pierwszoplanowy — jego wyjście kończy kontener)
exec python3 /app/read_token.py
