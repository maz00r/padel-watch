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
# Po nieudanym odczycie (błąd CDP, wygasły token, czekanie na logowanie) próbujemy
# szybciej niż READ_INTERVAL — monitor daje ~3 min karencji na dołek odnowy i ten
# retry musi się w niej zmieścić nawet po pojedynczej wpadce.
ERROR_RETRY = 45

# CICHE ZALOGOWANIE. Gdy sesja w Decathlon GO wygaśnie, ale sesja u dostawcy tożsamości
# jeszcze żyje, wystarczy kliknąć „ZALOGUJ SIĘ” — przeglądarka odbija się przez OAuth
# i wraca zalogowana, bez wpisywania czegokolwiek. Robimy dokładnie to samo kliknięcie
# we WŁASNEJ, wcześniej zalogowanej przeglądarce.
#
# CZEGO TO NIE ROBI I ROBIĆ NIE BĘDZIE: nie wpisuje loginu, hasła ani kodu z maila,
# nie dotyka formularzy, nie obchodzi żadnego zabezpieczenia. Jeśli po kliknięciu
# pojawi się formularz, poddajemy się i prosimy o ręczne logowanie.
AUTO_LOGIN = (os.environ.get("AUTO_LOGIN") or "true").strip().lower() not in ("false", "0", "no")
# Selektor jest po ATRYBUCIE href, nie po klasie: klasy w tej aplikacji są zahaszowane
# (`Topbar_navbarLogin__4Hfnb`) i zmieniają się przy każdym wydaniu.
LOGIN_LINK_JS = """
(() => {
  const a = document.querySelector('a[href="/login"], a[href$="/login"]');
  if (!a) return "brak";
  a.click();
  return "klik";
})()
"""
# Nie próbujemy w kółko: po serii nieudanych prób sesja u dostawcy też padła
# i potrzebne jest ręczne logowanie. Dalsze klikanie tylko męczy stronę.
AUTO_LOGIN_MAX_TRIES = 3
AUTO_LOGIN_COOLDOWN = 600      # s między próbami
_auto_login_tries = 0
_auto_login_last = 0.0


def try_silent_login(cdp):
    """Klika „ZALOGUJ SIĘ” i czeka na powrót. Zwraca (jwt, exp) albo (None, 0).

    Wywoływane WYŁĄCZNIE wtedy, gdy tokenu nie ma i nie stoimy na stronie logowania.
    """
    global _auto_login_tries, _auto_login_last
    if not AUTO_LOGIN:
        return None, 0
    if _auto_login_tries >= AUTO_LOGIN_MAX_TRIES:
        return None, 0
    if time.time() - _auto_login_last < AUTO_LOGIN_COOLDOWN:
        return None, 0
    _auto_login_last = time.time()
    _auto_login_tries += 1

    wynik = cdp.evaluate(LOGIN_LINK_JS)
    if wynik != "klik":
        log('~ Ciche logowanie: nie znalazłem linku „ZALOGUJ SIĘ” na stronie.')
        return None, 0
    log(f'~ Ciche logowanie: kliknąłem „ZALOGUJ SIĘ” '
        f'(próba {_auto_login_tries}/{AUTO_LOGIN_MAX_TRIES}) — czekam na powrót.')
    # Przekierowanie przez dostawcę tożsamości i z powrotem to kilka skoków.
    for _ in range(10):
        time.sleep(2)
        jwt = cdp.evaluate(f"localStorage.getItem({JWT_KEY!r})")
        if jwt:
            _auto_login_tries = 0     # udało się — licznik prób od nowa
            log("✓ Ciche logowanie zadziałało — sesja odzyskana bez wpisywania czegokolwiek.")
            return jwt, jwt_expiry(jwt)
    url = cdp.evaluate("location.href") or ""
    if any(h in url for h in LOGIN_URL_HINTS):
        log("✗ Ciche logowanie nie wystarczyło — Decathlon prosi o dane. "
            "Otwórz panel Padel i zaloguj się ręcznie.")
    else:
        log(f"✗ Ciche logowanie bez skutku (URL: {url[:70]}).")
    return None, 0


def fmt_left(seconds):
    seconds = int(seconds)
    return f"~{seconds} s" if seconds < 90 else f"~{seconds // 60} min"


# Te same poziomy i ta sama konwencja znaków co w check_padel.py — czytnik pisze do
# tego samego logu dodatku, więc filtr musi działać na obu procesach tak samo.
POZIOMY = {"debug": 10, "info": 20, "warn": 30, "error": 40}
WAGA_ZNAKU = {"✗": "error", "!": "warn", "⚠": "warn"}


def _prog_logu():
    nazwa = (os.environ.get("LOG_LEVEL") or "info").strip().lower()
    return POZIOMY.get(nazwa, POZIOMY["info"])


def log(*args, level=None):
    if level is None:
        pierwszy = next((str(a).strip() for a in args if str(a).strip()), "")
        level = WAGA_ZNAKU.get(pierwszy[:1], "info")
    if POZIOMY.get(level, POZIOMY["info"]) < _prog_logu():
        return
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
            # Sesja GO wygasła, ale u dostawcy tożsamości może jeszcze żyć — wtedy
            # samo kliknięcie „ZALOGUJ SIĘ” wystarczy. Nic nie wpisujemy.
            jwt, exp = try_silent_login(cdp)
            if jwt:
                return jwt, exp, None
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
    ostatni_blad = True  # start traktujemy jak „nie mieliśmy tokenu" -> pierwszy odczyt widoczny
    while True:
        # Domyślnie szybki retry — pełny READ_INTERVAL tylko po UDANYM odczycie.
        sleep_s = ERROR_RETRY
        try:
            jwt, exp, err = read_jwt_once()
            if err:
                log(f"✗ {err}")
                ostatni_blad = True
            elif exp and exp <= time.time():
                # Nie nadpisujemy pliku wygasłym tokenem — monitor mógłby zgubić lepszy.
                log(f"⚠ JWT w localStorage WYGASŁ {fmt_left(time.time() - exp)} temu — sesja "
                    f"mogła paść; sprawdź panel i zaloguj się ponownie.")
                ostatni_blad = True
            else:
                write_token_file(jwt, exp)  # udostępnij monitorowi w tym samym kontenerze
                sleep_s = next_sleep(exp)
                poziom, ostatni_blad = ("info" if ostatni_blad else "debug"), False
                if exp:
                    when = datetime.fromtimestamp(exp, timezone.utc).astimezone()
                    log(f"✓ JWT odczytany, ważny do {when:%Y-%m-%d %H:%M:%S} "
                        f"(jeszcze {fmt_left(exp - time.time())}). "
                        f"Kolejny odczyt za {fmt_left(sleep_s)}.", level=poziom)
                else:
                    log(f"✓ JWT odczytany, ale nie odczytałem exp. Długość: {len(jwt)} zn.",
                        level=poziom)
        except Exception as e:  # noqa: BLE001 - czytnik ma przetrwać każdy błąd
            log(f"! Błąd odczytu: {e!r}")
            ostatni_blad = True
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
