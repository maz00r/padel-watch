#!/usr/bin/env sh
# Mapuje opcje dodatku (/data/options.json wpisywane przez Supervisor HA) na zmienne
# środowiskowe, których używa check_padel.py, i uruchamia monitor w trybie pętli.
set -e

OPT=/data/options.json
opt() { python3 -c "import json;print(json.load(open('$OPT')).get('$1',''))" 2>/dev/null || true; }

export NTFY_TOPIC="$(opt ntfy_topic)"
export CHECK_INTERVAL="$(opt check_interval)"
export FILTERS="$(opt filters)"
export INTERVALS="$(opt intervals)"
export LISTINGS="$(opt listing_url)"
export TIMEZONE="$(opt timezone)"
export AUTO_REGISTER="$(opt auto_register)"
export AUTO_REGISTER_DRY_RUN="$(opt auto_register_dry_run)"
export DECATHLON_TOKEN="$(opt decathlon_token)"
export DECATHLON_COOKIE="$(opt decathlon_cookie)"
export AUTO_REGISTER_NAME="$(opt auto_register_name)"
export AUTO_REGISTER_AGE="$(opt auto_register_age)"
export AUTO_REGISTER_PAID="$(opt auto_register_paid)"
export AUTO_REGISTER_MAX="$(opt auto_register_max)"
export AUTO_REGISTER_ORDER="$(opt auto_register_order)"
export CLEAR_STATE="$(opt clear_state)"
# opcja pominięta w UI -> Python zwraca "None"; traktuj jak pustą
[ "$FILTERS" = "None" ] && export FILTERS=""
[ "$INTERVALS" = "None" ] && export INTERVALS=""
[ "$AUTO_REGISTER" = "None" ] && export AUTO_REGISTER=""
[ "$AUTO_REGISTER_DRY_RUN" = "None" ] && export AUTO_REGISTER_DRY_RUN=""
[ "$DECATHLON_TOKEN" = "None" ] && export DECATHLON_TOKEN=""
[ "$DECATHLON_COOKIE" = "None" ] && export DECATHLON_COOKIE=""
[ "$AUTO_REGISTER_NAME" = "None" ] && export AUTO_REGISTER_NAME=""
[ "$AUTO_REGISTER_AGE" = "None" ] && export AUTO_REGISTER_AGE=""
[ "$AUTO_REGISTER_PAID" = "None" ] && export AUTO_REGISTER_PAID=""
[ "$AUTO_REGISTER_MAX" = "None" ] && export AUTO_REGISTER_MAX=""
[ "$AUTO_REGISTER_ORDER" = "None" ] && export AUTO_REGISTER_ORDER=""
[ "$CLEAR_STATE" = "None" ] && export CLEAR_STATE=""
export STATE_DIR="/data"           # stan (state.json) trwały między restartami dodatku
export CONFIG_PATH="/data/__none__.json"   # brak pliku -> skrypt bierze wszystko z ENV

[ -z "$CHECK_INTERVAL" ] && export CHECK_INTERVAL=60
[ -z "$TIMEZONE" ] && export TIMEZONE="Europe/Warsaw"

echo "[padel-watch] start: listing=${LISTINGS} interval=${CHECK_INTERVAL}s filters='${FILTERS}' tz=${TIMEZONE}"
exec python3 /app/check_padel.py
