#!/usr/bin/env python3
"""Testy izolowanych kontekstów przeglądarki. `python3 -m unittest -v test_konteksty`

Dziesięć kont w jednym Chromium stoi na dwóch rzeczach: żeby konteksty były naprawdę
sprzątane (inaczej wyciek zabije przeglądarkę w sekundzie publikacji) i żeby sesja
odtwarzała się z zapisanych ciasteczek (inaczej po każdym restarcie dziesięć logowań).
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "padel_browser"))
sys.modules.setdefault("websocket", mock.MagicMock())
import konteksty as kx  # noqa: E402


class FakeBrowser:
    """Atrapa połączenia z przeglądarką: notuje polecenia i udaje konteksty."""

    def __init__(self, ciasteczka=None):
        self.wywolania = []
        self.zywe_konteksty = set()
        self.ciasteczka = ciasteczka or []
        self.wstrzykniete = []
        self._n = 0

    def call(self, metoda, **params):
        self.wywolania.append((metoda, params))
        if metoda == "Target.createBrowserContext":
            self._n += 1
            kid = f"ctx{self._n}"
            self.zywe_konteksty.add(kid)
            return {"browserContextId": kid}
        if metoda == "Target.createTarget":
            return {"targetId": f"target-{params.get('browserContextId')}"}
        if metoda == "Target.disposeBrowserContext":
            self.zywe_konteksty.discard(params.get("browserContextId"))
            return {}
        if metoda == "Storage.getCookies":
            return {"cookies": self.ciasteczka}
        if metoda == "Storage.setCookies":
            self.wstrzykniete = params.get("cookies") or []
            return {}
        return {}


class FakePage:
    def __init__(self, ws):
        self.ws = ws
        self.zamknieta = False

    def close(self):
        self.zamknieta = True


class KontekstLifecycleTest(unittest.TestCase):
    """Porzucony kontekst zostaje w pamięci Chromium do końca życia procesu.
    Przy dziesięciu kontach odświeżanych co kilka minut wyciek urósłby w dobę do setek."""

    def kontekst(self, browser):
        with mock.patch.object(kx, "page_ws", return_value="ws://karta"):
            return kx.Kontekst(browser, klient=FakePage)

    def test_opening_creates_a_context_and_a_tab_inside_it(self):
        b = FakeBrowser()
        with mock.patch.object(kx, "page_ws", return_value="ws://karta"):
            with kx.Kontekst(b, klient=FakePage).otworz("https://go.decathlon.pl") as k:
                self.assertEqual(k.ctx_id, "ctx1")
                # Karta MUSI powstać w tym kontekście, inaczej dzieli ciasteczka z resztą.
                utworz = dict(b.wywolania)["Target.createTarget"]
                self.assertEqual(utworz["browserContextId"], "ctx1")

    def test_the_context_is_disposed_on_the_way_out(self):
        b = FakeBrowser()
        with mock.patch.object(kx, "page_ws", return_value="ws://karta"):
            with kx.Kontekst(b, klient=FakePage).otworz("https://x") as k:
                strona = k.page
        self.assertEqual(b.zywe_konteksty, set(), "kontekst wyciekł")
        self.assertTrue(strona.zamknieta, "karta została otwarta")

    def test_an_exception_still_disposes_the_context(self):
        """SEDNO: błąd w środku zbiórki nie może zostawić wycieku."""
        b = FakeBrowser()
        with mock.patch.object(kx, "page_ws", return_value="ws://karta"):
            with self.assertRaises(RuntimeError):
                with kx.Kontekst(b, klient=FakePage).otworz("https://x"):
                    raise RuntimeError("zbiórka padła")
        self.assertEqual(b.zywe_konteksty, set(), "wyciek po wyjątku")

    def test_ten_accounts_leave_nothing_behind(self):
        b = FakeBrowser()
        with mock.patch.object(kx, "page_ws", return_value="ws://karta"):
            for _ in range(10):
                with kx.Kontekst(b, klient=FakePage).otworz("https://x"):
                    pass
        self.assertEqual(b.zywe_konteksty, set())

    def test_cleanup_survives_a_broken_connection(self):
        """Sprzątanie nie może wywrócić zbiórki, nawet gdy przeglądarka już nie odpowiada."""
        b = FakeBrowser()
        with mock.patch.object(kx, "page_ws", return_value="ws://karta"):
            k = kx.Kontekst(b, klient=FakePage).otworz("https://x")
        b.call = mock.Mock(side_effect=RuntimeError("websocket padł"))
        k.zamknij()          # nie może rzucić


class CookieRoundTripTest(unittest.TestCase):
    """Konteksty CDP nie przeżywają restartu — trwałość robimy sami."""

    CIASTKA = [
        {"name": "sid", "value": "abc", "domain": ".decathlon.pl", "path": "/",
         "httpOnly": True, "secure": True, "session": True},
        {"name": "idp", "value": "xyz", "domain": ".account.decathlon.com", "path": "/",
         "httpOnly": True, "secure": True, "expires": 1800000000},
    ]

    def test_session_cookies_are_exported_too(self):
        """Sesyjne ciasteczka zwykle TRZYMAJĄ sesję u dostawcy tożsamości —
        pominięcie ich to gwarantowany brak odtworzenia logowania."""
        b = FakeBrowser(ciasteczka=self.CIASTKA)
        wyeksportowane = kx.eksportuj_ciasteczka(b, "ctx1")
        self.assertEqual(len(wyeksportowane), 2)
        self.assertTrue(any(c.get("session") for c in wyeksportowane))

    def test_injection_keeps_httponly_and_domain(self):
        b = FakeBrowser()
        kx.wstrzyknij_ciasteczka(b, "ctx1", self.CIASTKA)
        self.assertEqual(len(b.wstrzykniete), 2)
        self.assertTrue(all(c["domain"] for c in b.wstrzykniete))
        self.assertTrue(b.wstrzykniete[0]["httpOnly"])

    def test_garbage_entries_are_dropped_not_crashed_on(self):
        b = FakeBrowser()
        ile = kx.wstrzyknij_ciasteczka(b, "ctx1", [{"name": "ok", "domain": "x"},
                                                   {"value": "bez nazwy"}, {}])
        self.assertEqual(ile, 1)

    def test_nothing_to_inject_means_no_call(self):
        b = FakeBrowser()
        self.assertEqual(kx.wstrzyknij_ciasteczka(b, "ctx1", []), 0)
        self.assertNotIn("Storage.setCookies", dict(b.wywolania))

    def test_round_trip_through_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            plik = os.path.join(d, "cookies-marek.json")

            def zapis(sciezka, dane):
                with open(sciezka, "w", encoding="utf-8") as f:
                    json.dump(dane, f)
                return True

            kx.zapisz_ciasteczka(plik, self.CIASTKA, zapis=zapis)
            self.assertEqual(kx.wczytaj_ciasteczka(plik), self.CIASTKA)

    def test_a_missing_or_broken_file_is_not_a_crash(self):
        """Brak sesji to powód do ponownego zalogowania, nie do wywrócenia zbiórki."""
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(kx.wczytaj_ciasteczka(os.path.join(d, "nie-ma.json")), [])
            zly = os.path.join(d, "zly.json")
            with open(zly, "w", encoding="utf-8") as f:
                f.write("{to nie jest JSON")
            self.assertEqual(kx.wczytaj_ciasteczka(zly), [])


if __name__ == "__main__":
    unittest.main()
