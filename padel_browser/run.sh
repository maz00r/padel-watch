#!/usr/bin/env sh
# Scalony dodatek: przeglądarka (logowanie + podtrzymanie sesji) + monitor terminów.
#   1) Xvfb -> Chromium -> VNC -> noVNC (panel przez Ingress) — logujesz się raz.
#   2) read_token.py (w tle) czyta świeży go-sdk-jwt i zapisuje go do /data/token.json.
#   3) check_padel.py (pierwszoplanowo) monitoruje terminy i bierze token z tego pliku.
set -e

OPT=/data/options.json
opt() { python3 -c "import json;print(json.load(open('$OPT')).get('$1',''))" 2>/dev/null || true; }

# --- opcje przeglądarki ---
START_URL="$(opt start_url)"
READ_INTERVAL="$(opt read_interval)"
# `case` zamiast `[ ] || [ ] && [ ]` — ta konstrukcja pod `set -e` potrafi ubić skrypt.
case "$START_URL" in "" | None) START_URL="https://go.decathlon.pl" ;; esac
case "$READ_INTERVAL" in "" | None) READ_INTERVAL=300 ;; esac
export START_URL READ_INTERVAL

# --- opcje monitora (mapowane na ENV, których używa check_padel.py) ---
export NTFY_TOPIC="$(opt ntfy_topic)"
export CHECK_INTERVAL="$(opt check_interval)"
export FILTERS="$(opt filters)"
export INTERVALS="$(opt intervals)"
export LISTINGS="$(opt listing_url)"
export TIMEZONE="$(opt timezone)"
export AUTO_REGISTER="$(opt auto_register)"
export AUTO_REGISTER_DRY_RUN="$(opt auto_register_dry_run)"
export AUTO_REGISTER_NAME="$(opt auto_register_name)"
export AUTO_REGISTER_AGE="$(opt auto_register_age)"
export AUTO_REGISTER_PAID="$(opt auto_register_paid)"
export AUTO_REGISTER_MAX="$(opt auto_register_max)"
export AUTO_REGISTER_ORDER="$(opt auto_register_order)"
export CLEAR_STATE="$(opt clear_state)"
export TEST_TOKEN="$(opt test_token)"
export DECATHLON_TOKEN="$(opt decathlon_token)"
export DECATHLON_COOKIE="$(opt decathlon_cookie)"
# opcja pominięta w UI -> Python zwraca "None"; traktuj jak pustą
for v in FILTERS INTERVALS AUTO_REGISTER AUTO_REGISTER_DRY_RUN AUTO_REGISTER_NAME \
         AUTO_REGISTER_AGE AUTO_REGISTER_PAID AUTO_REGISTER_MAX AUTO_REGISTER_ORDER \
         CLEAR_STATE TEST_TOKEN DECATHLON_TOKEN DECATHLON_COOKIE; do
  eval "val=\$$v"
  [ "$val" = "None" ] && export "$v="
done

export STATE_DIR="/data"                       # stan (state.json) trwały między restartami
export CONFIG_PATH="/data/__none__.json"       # brak pliku -> check_padel bierze wszystko z ENV
export DECATHLON_TOKEN_FILE="/data/token.json" # wymiana tokenu: przeglądarka -> monitor
case "$CHECK_INTERVAL" in "" | None) CHECK_INTERVAL=60 ;; esac
case "$TIMEZONE" in "" | None) TIMEZONE="Europe/Warsaw" ;; esac
export CHECK_INTERVAL TIMEZONE

mkdir -p "$CHROME_PROFILE"
echo "[padel] start: url=${START_URL} read=${READ_INTERVAL}s check=${CHECK_INTERVAL}s listing=${LISTINGS}"

# 1) Wirtualny ekran
Xvfb :1 -screen 0 1280x900x24 -nolisten tcp &
sleep 2

# 2) Chromium z TRWAŁYM profilem (/data przeżywa restarty) + CDP do odczytu tokenu.
#    Flagi GPU wymuszają rendering programowy i wyciszają szum błędów Vulkan/GPU-process.
chromium-browser \
  --no-sandbox --disable-dev-shm-usage \
  --disable-gpu --disable-gpu-compositing \
  --enable-logging=stderr --log-level=3 \
  --user-data-dir="$CHROME_PROFILE" \
  --remote-debugging-port=9222 --remote-allow-origins='*' \
  --window-position=0,0 --window-size=1280,900 \
  --no-first-run --no-default-browser-check \
  "$START_URL" &
sleep 4

# 3) VNC na ekran :1 (tylko lokalnie — na zewnątrz wychodzi wyłącznie noVNC przez Ingress)
x11vnc -display :1 -forever -shared -nopw -quiet -localhost -rfbport 5900 &
sleep 1

# 4) noVNC: websockify serwuje panel i tuneluje websocket na tym samym porcie (Ingress).
websockify --web=/usr/share/novnc 8099 localhost:5900 &
sleep 1

# 5) Czytnik tokenu w tle (z autorestartem — gdyby kiedyś padł, plik tokenu nie zamrze).
( while true; do
    python3 /app/read_token.py || echo "[read_token] proces zakończony — restart za 5s"
    sleep 5
  done ) &

# 6) Monitor terminów (proces pierwszoplanowy — jego wyjście kończy kontener)
exec python3 /app/check_padel.py
