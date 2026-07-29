#!/usr/bin/env sh
# Monitor repertuaru Cinema City: mapuje opcje dodatku na ENV i odpala silnik w pętli.
set -e

OPT=/data/options.json
opt() { python3 -c "import json;print(json.load(open('$OPT')).get('$1',''))" 2>/dev/null || true; }

export NTFY_TOPIC="$(opt ntfy_topic)"
export FILM_URL="$(opt film_url)"
export CHECK_INTERVAL="$(opt check_interval)"
export CINEMAS="$(opt cinemas)"
export DAYS_AHEAD="$(opt days_ahead)"
export TIMEZONE="$(opt timezone)"
export CLEAR_STATE="$(opt clear_state)"

# Opcja pominięta w UI -> Python zwraca "None"; traktuj jak pustą.
for v in CINEMAS CLEAR_STATE; do
  eval "val=\$$v"
  [ "$val" = "None" ] && export "$v="
done

case "$CHECK_INTERVAL" in "" | None) CHECK_INTERVAL=3600 ;; esac
case "$DAYS_AHEAD" in "" | None) DAYS_AHEAD=365 ;; esac
case "$TIMEZONE" in "" | None) TIMEZONE="Europe/Warsaw" ;; esac
export CHECK_INTERVAL DAYS_AHEAD TIMEZONE
export TZ="$TIMEZONE"

export STATE_DIR="/data"   # stan przeżywa restarty dodatku

echo "[kino] start: co ${CHECK_INTERVAL}s, link=${FILM_URL}"
exec python3 /app/check_cinema.py
