#!/usr/bin/env sh
# Mapuje opcje dodatku (/data/options.json wpisywane przez Supervisor HA) na zmienne
# środowiskowe, których używa check_padel.py, i uruchamia monitor w trybie pętli.
set -e

OPT=/data/options.json
opt() { python3 -c "import json;print(json.load(open('$OPT')).get('$1',''))" 2>/dev/null || true; }

export NTFY_TOPIC="$(opt ntfy_topic)"
export CHECK_INTERVAL="$(opt check_interval)"
export FILTERS="$(opt filters)"
export LISTINGS="$(opt listing_url)"
export TIMEZONE="$(opt timezone)"
export STATE_DIR="/data"           # stan (state.json) trwały między restartami dodatku
export CONFIG_PATH="/data/__none__.json"   # brak pliku -> skrypt bierze wszystko z ENV

[ -z "$CHECK_INTERVAL" ] && export CHECK_INTERVAL=60
[ -z "$TIMEZONE" ] && export TIMEZONE="Europe/Warsaw"

echo "[padel-watch] start: listing=${LISTINGS} interval=${CHECK_INTERVAL}s filters='${FILTERS}' tz=${TIMEZONE}"
exec python3 /app/check_padel.py
