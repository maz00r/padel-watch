#!/usr/bin/env python3
"""Czyta go-sdk-jwt z Chromium przez CDP i udostępnia go monitorowi (plik token.json).

Zasady współpracy z użytkownikiem i stroną:
- Gdy użytkownik jest w trakcie logowania (strona SSO/oauth) — NIE nawigujemy,
  żeby nie wyrzucić go z formularza w połowie wpisywania kodu z maila.
- Gdy token w localStorage jest wciąż ważny — czytamy go BEZ przeładowania strony
  (nawigacja przeszkadza, a strona i tak nie odnowi ważnego tokenu).
- Gdy token wygasł/go brak — wczytujemy stronę: zalogowana sesja odnawia token
  przy ładowaniu (cichy SSO), świeży zapisujemy do pliku.
- Harmonogram jest adaptacyjny: budzimy się tuż PO wygaśnięciu tokenu (wtedy da
  się go odnowić), nie rzadziej niż co READ_INTERVAL.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import websocket  # websocket-client

CDP_URL = "http://127.0.0.1:9222"
START_URL = os.environ.get("START_URL") or "https://go.decathlon.pl"
READ_INTERVAL = int(os.environ.get("READ_INTERVAL") or 300)
JWT_KEY = "go-sdk-jwt"
# Plik wymiany tokenu z monitorem (check_padel.py) w tym samym kontenerze. Gdy pusty,
# działamy jak PoC — tylko raportujemy ważność, niczego nie zapisujemy.
TOKEN_FILE = os.environ.get("DECATHLON_TOKEN_FILE") or ""
# Fragmenty URL-i, na których użytkownik może właśnie się logować — wtedy nie ruszamy strony.
LOGIN_URL_HINTS = ("login", "connect/oauth", "account.decathlon", "logged-in")
# Minimalna przerwa między odczytami (nie młócimy CDP), oraz zapas po wygaśnięciu tokenu.
MIN_SLEEP = 20
RENEW_DELAY = 10


def log(*args):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}]", *args, flush=True)


def write_token_file(jwt, exp):
    """Zapisuje świeży token atomowo (zapis do .tmp + rename), by monitor nie czytał połówki."""
    if not TOKEN_FILE:
        return
    tmp = TOKEN_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"jwt": jwt, "exp": exp}, f)
        os.replace(tmp, TOKEN_FILE)
    except OSError as e:
        log(f"! Nie zapisałem pliku tokenu {TOKEN_FILE}: {e!r}")


def jwt_expiry(token):
    """exp z JWT (bez weryfikacji podpisu — tylko do raportowania ważności)."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return 0
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        return int(data.get("exp") or 0)
    except Exception:  # noqa: BLE001 - diagnostyka nie może wywrócić czytnika
        return 0


def cdp_page_target(retries=30):
    """Czeka na Chromium i zwraca webSocketDebuggerUrl karty.

    Preferuje kartę z otwartym Decathlon GO — użytkownik może mieć w panelu więcej
    zakładek, a token siedzi w localStorage konkretnej domeny.
    """
    for _ in range(retries):
        pages = []
        try:
            with urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=5) as r:
                pages = [t for t in json.loads(r.read().decode("utf-8"))
                         if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            pass
        for t in pages:
            if "go.decathlon" in (t.get("url") or ""):
                return t["webSocketDebuggerUrl"]
        if pages:
            return pages[0]["webSocketDebuggerUrl"]
        time.sleep(1)
    return None


class Cdp:
    """Minimalny klient Chrome DevTools Protocol (tyle, ile potrzebujemy)."""

    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self._id = 0

    def call(self, method, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    def evaluate(self, expression):
        res = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        return (res.get("result") or {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def read_jwt_once():
    """Zwraca (jwt, exp, błąd). Nawiguje TYLKO gdy trzeba (token wygasł) i wolno (nie SSO)."""
    ws_url = cdp_page_target()
    if not ws_url:
        return None, 0, "Chromium nie wystartował (brak CDP)"
    cdp = Cdp(ws_url)
    try:
        url = cdp.evaluate("location.href") or ""
        if any(h in url for h in LOGIN_URL_HINTS):
            # Użytkownik może właśnie wpisywać hasło/kod z maila — nie wyrywamy mu strony.
            return None, 0, f"strona logowania otwarta ({url[:70]}) — czekam, dokończ w panelu"
        jwt = cdp.evaluate(f"localStorage.getItem({JWT_KEY!r})")
        exp = jwt_expiry(jwt)
        if jwt and exp > time.time():
            return jwt, exp, None  # ważny token — bez przeładowania (nie przeszkadzamy)
        # Brak/wygasły -> wczytaj stronę: zalogowana sesja odnowi token przy ładowaniu.
        cdp.call("Page.enable")
        cdp.call("Page.navigate", url=START_URL)
        time.sleep(6)  # daj SPA czas na cichy SSO i zapis tokenu
        jwt = cdp.evaluate(f"localStorage.getItem({JWT_KEY!r})")
        url = cdp.evaluate("location.href") or ""
        if not jwt:
            if any(h in url for h in LOGIN_URL_HINTS):
                return None, 0, f"strona przekierowała na logowanie ({url[:70]}) — zaloguj się w panelu"
            return None, 0, f"brak {JWT_KEY} w localStorage (URL: {url[:70]})"
        return jwt, jwt_expiry(jwt), None
    finally:
        cdp.close()


def next_sleep(exp):
    """Ile spać do kolejnego odczytu.

    Strona odnawia token dopiero PO wygaśnięciu, więc najlepszy moment na odczyt jest
    tuż po exp — wtedy plik tokenu jest przeterminowany najwyżej kilkanaście sekund.
    Nie rzadziej niż READ_INTERVAL, nie częściej niż MIN_SLEEP.
    """
    if not exp:
        return READ_INTERVAL
    until_renewal = exp - time.time() + RENEW_DELAY
    return max(MIN_SLEEP, min(READ_INTERVAL, until_renewal))


def main():
    log(f"Czytnik tokenu wystartował. Strona: {START_URL}, max co {READ_INTERVAL}s.")
    log("Zaloguj się w panelu — profil Chromium zostaje w /data (przeżywa restarty).")
    while True:
        sleep_s = READ_INTERVAL
        try:
            jwt, exp, err = read_jwt_once()
            if err:
                log(f"✗ {err}")
            elif exp and exp <= time.time():
                # Nie nadpisujemy pliku wygasłym tokenem — monitor mógłby zgubić lepszy.
                left = int(time.time() - exp)
                log(f"⚠ JWT w localStorage WYGASŁ {left // 60} min temu — sesja mogła paść; "
                    f"sprawdź panel i zaloguj się ponownie.")
            else:
                write_token_file(jwt, exp)  # udostępnij monitorowi w tym samym kontenerze
                sleep_s = next_sleep(exp)
                if exp:
                    when = datetime.fromtimestamp(exp, timezone.utc).astimezone()
                    log(f"✓ JWT odczytany, ważny do {when:%Y-%m-%d %H:%M:%S} "
                        f"(jeszcze ~{int(exp - time.time()) // 60} min). "
                        f"Kolejny odczyt za ~{int(sleep_s) // 60} min.")
                else:
                    log(f"✓ JWT odczytany, ale nie odczytałem exp. Długość: {len(jwt)} zn.")
        except Exception as e:  # noqa: BLE001 - czytnik ma przetrwać każdy błąd
            log(f"! Błąd odczytu: {e!r}")
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
