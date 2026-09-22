#!/usr/bin/env sh
# Mapuje opcje dodatku HA na zmienne środowiskowe silnika.
set -eu

OPT=/data/options.json
opt() { python3 -c "import json; print(json.load(open('$OPT')).get('$1', ''))" 2>/dev/null || true; }

export NTFY_TOPIC="$(opt ntfy_topic)"
export CHECK_INTERVAL="$(opt check_interval)"
export START_DATE="$(opt start_date)"
export END_DATE="$(opt end_date)"
export STATE_DIR=/data
export TZ=Europe/Rome

for variable in NTFY_TOPIC CHECK_INTERVAL START_DATE END_DATE; do
  eval "value=\${$variable}"
  [ "$value" = "None" ] && export "$variable="
done

: "${CHECK_INTERVAL:=60}"
: "${START_DATE:=2026-09-24}"
: "${END_DATE:=2026-09-28}"
export CHECK_INTERVAL START_DATE END_DATE

echo "[watykan] start: ${START_DATE}–${END_DATE}, 5 osób, co ${CHECK_INTERVAL}s"
exec python3 /app/check_vatican.py
