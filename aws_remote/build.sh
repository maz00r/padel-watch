#!/usr/bin/env bash
# Buduje paczkę ZIP do wgrania w konsoli Lambdy.
#
# Do paczki wchodzi handler ORAZ silnik `check_padel.py` z dodatku. To celowe:
# zdalna strona ma używać DOKŁADNIE tego samego parsera, filtrów, salwy i limitów
# co strona lokalna. Dwie osobne implementacje rozjechałyby się przy pierwszej
# zmianie, a skutkiem byłaby rezerwacja terminu, którego nie chcesz — albo o jeden
# za dużo. Żadnych zależności z pipa: silnik korzysta wyłącznie z biblioteki
# standardowej, więc paczka waży kilkadziesiąt kilobajtów.
set -euo pipefail

KATALOG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KORZEN="$(dirname "$KATALOG")"
WYJSCIE="$KATALOG/padel-remote.zip"

rm -f "$WYJSCIE"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cp "$KATALOG/handler.py" "$TMP/"
cp "$KORZEN/padel_browser/check_padel.py" "$TMP/"

( cd "$TMP" && zip -q -r "$WYJSCIE" handler.py check_padel.py )

echo "Gotowe: $WYJSCIE ($(du -h "$WYJSCIE" | cut -f1))"
echo "Handler w konsoli Lambdy ustaw na: handler.lambda_handler"
