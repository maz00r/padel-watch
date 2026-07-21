#!/usr/bin/env python3
"""PoC: czyta go-sdk-jwt z Chromium przez CDP i raportuje ważność tokenu.

Cel PoC: odpowiedzieć na JEDNO pytanie — czy po jednorazowym zalogowaniu (i przejściu
weryfikacji mailowej) sesja utrzymuje się na serwerze, tzn. czy Decathlon GO sam odnawia
JWT przy kolejnych wczytaniach strony z tego urządzenia.

Nic nie rezerwuje. Tylko wczytuje stronę i odczytuje token z localStorage.
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
READ_INTERVAL = int(os.environ.get("READ_INTERVAL") or 600)
JWT_KEY = "go-sdk-jwt"


def log(*args):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}]", *args, flush=True)


def jwt_expiry(token):
    """exp z JWT (bez weryfikacji podpisu — tylko do raportowania ważności)."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return 0
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        return int(data.get("exp") or 0)
    except Exception:  # noqa: BLE001 - diagnostyka nie może wywrócić PoC
        return 0


def cdp_page_target(retries=30):
    """Czeka na Chromium i zwraca webSocketDebuggerUrl pierwszej zakładki."""
    for _ in range(retries):
        try:
            with urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=5) as r:
                for t in json.loads(r.read().decode("utf-8")):
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            pass
        time.sleep(1)
    return None


class Cdp:
    """Minimalny klient Chrome DevTools Protocol (tyle, ile PoC potrzebuje)."""

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
    """Wczytuje stronę i zwraca (jwt, błąd). Strona sama robi cichy SSO, jeśli sesja żyje."""
    ws_url = cdp_page_target()
    if not ws_url:
        return None, "Chromium nie wystartował (brak CDP)"
    cdp = Cdp(ws_url)
    try:
        cdp.call("Page.enable")
        cdp.call("Page.navigate", url=START_URL)
        time.sleep(6)  # daj SPA czas na cichy SSO i zapis tokenu
        jwt = cdp.evaluate(f"localStorage.getItem({JWT_KEY!r})")
        url = cdp.evaluate("location.href") or ""
        if not jwt:
            if "login" in url or "connect/oauth" in url:
                return None, f"strona przekierowała na logowanie ({url[:70]}) — zaloguj się w panelu"
            return None, f"brak {JWT_KEY} w localStorage (URL: {url[:70]})"
        return jwt, None
    finally:
        cdp.close()


def main():
    log(f"PoC wystartował. Panel: zakładka Ingress. Strona: {START_URL}")
    log(f"Odczyt tokenu co {READ_INTERVAL}s. Zaloguj się w panelu — profil zostaje w /data.")
    while True:
        try:
            jwt, err = read_jwt_once()
            if err:
                log(f"✗ {err}")
            else:
                exp = jwt_expiry(jwt)
                if exp:
                    left = int(exp - time.time())
                    when = datetime.fromtimestamp(exp, timezone.utc).astimezone()
                    log(f"✓ JWT odczytany, ważny do {when:%Y-%m-%d %H:%M:%S} "
                        f"(jeszcze ~{left // 60} min). Długość: {len(jwt)} zn.")
                else:
                    log(f"✓ JWT odczytany, ale nie odczytałem exp. Długość: {len(jwt)} zn.")
        except Exception as e:  # noqa: BLE001 - PoC ma przetrwać każdy błąd
            log(f"! Błąd odczytu: {e!r}")
        time.sleep(READ_INTERVAL)


if __name__ == "__main__":
    main()
