#!/usr/bin/env python3
"""Izolowane konteksty przeglądarki — dziesięć kont w JEDNYM procesie Chromium.

Chrome trzyma w jednym procesie wiele kontekstów przeglądarki, z których każdy ma własne
ciasteczka i `localStorage`. To pozwala obsłużyć dziesięć kont Decathlon GO bez dziesięciu
przeglądarek (~3-4,5 GB RAM) i bez dziesięciu profili na dysku (~3 GB na `/data`).

Koszt na konto to nowa karta, nie nowa przeglądarka: ~3-5 s zamiast ~20 s. Przy dziesięciu
kontach zbiórka tokenów skraca się z ~200 s do ~40 s — a to właśnie decyduje, czy zdążymy
odświeżyć wszystko tuż przed publikacją grafiku.

CZEGO TO NIE ROBI: nie wpisuje loginu ani hasła. Sesja bierze się z ciasteczek zapisanych
po JEDNORAZOWYM, ręcznym zalogowaniu w panelu — dokładnie jak na koncie głównym.

TRWAŁOŚĆ ROBIMY SAMI. Konteksty CDP żyją w pamięci i nie przeżywają restartu Chromium.
Dlatego po zalogowaniu eksportujemy ciasteczka do pliku, a przy kolejnym starcie
wstrzykujemy je z powrotem. To jest jedyne nieudowodnione założenie tej konstrukcji:
czy sesja OAuth Decathlona odtwarza się z samych ciasteczek. Sprawdza to `sonda()`.
"""

import json
import os
import time
import urllib.error
import urllib.request

CDP_URL = os.environ.get("CDP_URL") or "http://127.0.0.1:9222"
# Ile czekamy, aż SPA wykona cichy SSO i zapisze token. Dobrane jak w read_token.py:
# odbicie przez dostawcę tożsamości to kilka skoków.
SSO_PROBY = 10
SSO_ODSTEP = 2


def browser_ws(cdp_url=None):
    """Adres websocketu PRZEGLĄDARKI (nie karty).

    Polecenia `Target.*` i `Storage.*` z `browserContextId` działają wyłącznie na tym
    połączeniu — websocket karty ich nie obsłuży.
    """
    baza = cdp_url or CDP_URL
    with urllib.request.urlopen(f"{baza}/json/version", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))["webSocketDebuggerUrl"]


def page_ws(target_id, cdp_url=None, retries=10):
    """Websocket karty o danym `targetId` — z listy, tak jak robi to read_token."""
    baza = cdp_url or CDP_URL
    for _ in range(retries):
        try:
            with urllib.request.urlopen(f"{baza}/json/list", timeout=5) as r:
                for t in json.loads(r.read().decode("utf-8")):
                    if t.get("id") == target_id and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            pass
        time.sleep(0.3)
    return None


def eksportuj_ciasteczka(browser, ctx_id):
    """Ciasteczka kontekstu — komplet, razem z `httpOnly` i sesyjnymi.

    Sesyjne (bez daty wygaśnięcia) też muszą polecieć: to zwykle one trzymają sesję
    u dostawcy tożsamości, a bez nich odtworzenie logowania nie ma szans.
    """
    return (browser.call("Storage.getCookies", browserContextId=ctx_id).get("cookies") or [])


def wstrzyknij_ciasteczka(browser, ctx_id, ciasteczka):
    """Wstrzykuje zapisane ciasteczka do świeżego kontekstu. Zwraca ile wstrzyknięto."""
    czyste = [c for c in (ciasteczka or []) if c.get("name") and c.get("domain")]
    if czyste:
        browser.call("Storage.setCookies", cookies=czyste, browserContextId=ctx_id)
    return len(czyste)


def zapisz_ciasteczka(sciezka, ciasteczka, zapis=None):
    """Zapis atomowy — czytelnik nigdy nie zobaczy połowy pliku."""
    if zapis is None:                       # wstrzykiwane w testach i przez check_padel
        from check_padel import zapisz_json_atomowo as zapis
    return zapis(sciezka, {"cookies": ciasteczka, "at": int(time.time())})


def wczytaj_ciasteczka(sciezka):
    """Ciasteczka konta z dysku. Pusta lista, gdy pliku nie ma albo jest uszkodzony —
    brak sesji to powód do ponownego zalogowania, nie do wywrócenia zbiórki."""
    try:
        with open(sciezka, encoding="utf-8") as f:
            return json.load(f).get("cookies") or []
    except (OSError, ValueError):
        return []


class Kontekst:
    """Izolowany kontekst przeglądarki jako menedżer kontekstu Pythona.

    Sprzątanie w `finally` jest OBOWIĄZKOWE: porzucony kontekst zostaje w pamięci
    Chromium do końca życia procesu. Przy dziesięciu kontach odświeżanych co kilka minut
    wyciek urósłby w ciągu doby do setek kontekstów i zabił przeglądarkę akurat
    w sekundzie publikacji.
    """

    def __init__(self, browser, cdp_url=None, klient=None):
        self.browser = browser
        self.cdp_url = cdp_url
        self._klient = klient          # fabryka Cdp — podmieniana w testach
        self.ctx_id = None
        self.target_id = None
        self.page = None

    def _cdp(self, ws):
        if self._klient:
            return self._klient(ws)
        from read_token import Cdp
        return Cdp(ws)

    def otworz(self, url):
        self.ctx_id = self.browser.call("Target.createBrowserContext").get("browserContextId")
        self.target_id = self.browser.call(
            "Target.createTarget", url=url, browserContextId=self.ctx_id).get("targetId")
        ws = page_ws(self.target_id, self.cdp_url)
        if ws:
            self.page = self._cdp(ws)
        return self

    def zamknij(self):
        if self.page:
            self.page.close()
            self.page = None
        for metoda, params in (("Target.closeTarget", {"targetId": self.target_id}),
                               ("Target.disposeBrowserContext",
                                {"browserContextId": self.ctx_id})):
            if not list(params.values())[0]:
                continue
            try:
                self.browser.call(metoda, **params)
            except Exception:  # noqa: BLE001 - sprzątanie nie może wywrócić zbiórki
                pass
        self.ctx_id = self.target_id = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.zamknij()
        return False


# --------------------------------------------------------------- sonda (jednorazowo)

def _token_z_karty(page, jwt_key="go-sdk-jwt", proby=SSO_PROBY, odstep=SSO_ODSTEP):
    """Czeka, aż SPA zapisze JWT w localStorage tego kontekstu."""
    for _ in range(proby):
        jwt = page.evaluate(f"localStorage.getItem({jwt_key!r})")
        if jwt:
            return jwt
        time.sleep(odstep)
    return ""


def zaloguj(kid, start_url="https://go.decathlon.pl", minuty=10, cdp_url=None, log=print):
    """Otwiera kontekst i TRZYMA go, żebyś zalogował się przez noVNC. Potem zapisuje
    ciasteczka do `/data/cookies-<kid>.json`.

    Nie wpisuje ani loginu, ani hasła — robisz to sam, w prawdziwym formularzu Decathlona.
    """
    from read_token import Cdp
    browser = Cdp(browser_ws(cdp_url))
    try:
        with Kontekst(browser, cdp_url).otworz(start_url) as k:
            if not k.page:
                log("✗ Nie otworzyłem karty w kontekście — sprawdź, czy Chromium żyje.")
                return False
            log(f"→ Kontekst konta „{kid}” otwarty. Wejdź w panelu w zakładkę "
                f"Przeglądarka i zaloguj się. Masz {minuty} min.")
            koniec = time.time() + minuty * 60
            while time.time() < koniec:
                if _token_z_karty(k.page, proby=1, odstep=0):
                    ciastka = eksportuj_ciasteczka(browser, k.ctx_id)
                    zapisz_ciasteczka(sciezka_ciasteczek(kid), ciastka)
                    log(f"✓ Zalogowano konto „{kid}”. Zapisałem {len(ciastka)} ciasteczek.")
                    return True
                time.sleep(3)
            log(f"✗ Minęło {minuty} min bez zalogowania — nic nie zapisałem.")
            return False
    finally:
        browser.close()


def sprawdz(kid, start_url="https://go.decathlon.pl", cdp_url=None, log=print):
    """SONDA: czy sesja odtwarza się z SAMYCH ciasteczek?

    To jedyne nieudowodnione założenie całej konstrukcji. Jeśli tu wyjdzie „nie",
    trzeba wrócić do osobnych profili na dysku — wolniej i grubiej, ale sprawdzone.
    """
    from read_token import Cdp
    ciastka = wczytaj_ciasteczka(sciezka_ciasteczek(kid))
    if not ciastka:
        log(f"✗ Brak zapisanych ciasteczek konta „{kid}” — najpierw je zaloguj.")
        return False
    browser = Cdp(browser_ws(cdp_url))
    try:
        with Kontekst(browser, cdp_url) as k:
            k.ctx_id = browser.call("Target.createBrowserContext").get("browserContextId")
            ile = wstrzyknij_ciasteczka(browser, k.ctx_id, ciastka)
            # Ciasteczka MUSZĄ być wstrzyknięte PRZED nawigacją — inaczej strona zdąży
            # zobaczyć pusty kontekst i odbić na logowanie.
            k.target_id = browser.call("Target.createTarget", url=start_url,
                                       browserContextId=k.ctx_id).get("targetId")
            ws = page_ws(k.target_id, cdp_url)
            k.page = Cdp(ws) if ws else None
            if not k.page:
                log("✗ Nie otworzyłem karty.")
                return False
            jwt = _token_z_karty(k.page)
            url = k.page.evaluate("location.href") or ""
            if jwt:
                log(f"✓ SESJA ODTWORZONA z {ile} ciasteczek — konteksty wystarczą, "
                    f"profile na dysku niepotrzebne.")
                return True
            log(f"✗ Wstrzyknąłem {ile} ciasteczek, ale token się nie pojawił "
                f"(URL: {url[:70]}). Sesja OAuth potrzebuje czegoś więcej — "
                f"wracamy do osobnych profili.")
            return False
    finally:
        browser.close()


def sciezka_ciasteczek(kid):
    katalog = os.environ.get("STATE_DIR") or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(katalog, f"cookies-{kid}.json")


if __name__ == "__main__":
    import sys as _sys
    # Wspolny log dodatku: ten sam znacznik czasu i te same poziomy co reszta.
    # Sonda pisze do Dziennika HA, bo tam jej szukasz — nie w osobnym terminalu.
    try:
        from check_padel import log as _log
    except Exception:  # noqa: BLE001 - sonda nie moze paść na imporcie
        _log = print
    args = _sys.argv[1:]
    if len(args) >= 2 and args[0] == "--zaloguj":
        _sys.exit(0 if zaloguj(args[1], log=_log) else 1)
    if len(args) >= 2 and args[0] == "--sprawdz":
        _sys.exit(0 if sprawdz(args[1], log=_log) else 1)
    print("Użycie:\n"
          "  python3 konteksty.py --zaloguj <id>   # otwiera kontekst, logujesz się w noVNC\n"
          "  python3 konteksty.py --sprawdz <id>   # czy sesja wraca z samych ciasteczek")
    _sys.exit(2)
