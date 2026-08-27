"""Testy cichego logowania — czytnik tokenu nie ma własnego pliku testów, więc dokładamy tu."""
import importlib, io, os, sys, time, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "padel_browser"))
sys.modules.setdefault("websocket", mock.MagicMock())
rt = importlib.import_module("read_token")


class FakeCdp:
    """Atrapa CDP: oddaje kolejne wartości localStorage i notuje, co wykonano."""

    def __init__(self, klik="klik", jwty=(), url="https://go.decathlon.pl/"):
        self.klik, self.jwty, self.url = klik, list(jwty), url
        self.wykonane = []

    def evaluate(self, expr):
        self.wykonane.append(expr)
        if "a.click()" in expr:
            return self.klik
        if "location.href" in expr:
            return self.url
        return self.jwty.pop(0) if self.jwty else None


class SilentLoginTest(unittest.TestCase):
    """Klikamy WYŁĄCZNIE link „ZALOGUJ SIĘ" we własnej przeglądarce.

    Nigdy nie wpisujemy loginu, hasła ani kodu z maila — jeśli po kliknięciu pojawia
    się formularz, poddajemy się i prosimy użytkownika o ręczne logowanie.
    """

    def setUp(self):
        rt._auto_login_tries = 0
        rt._auto_login_last = 0.0
        self.addCleanup(setattr, rt, "_auto_login_tries", 0)
        self.addCleanup(setattr, rt, "_auto_login_last", 0.0)
        patcher = mock.patch.object(rt, "AUTO_LOGIN", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        sen = mock.patch.object(rt.time, "sleep")
        sen.start()
        self.addCleanup(sen.stop)

    def test_click_recovers_the_session(self):
        cdp = FakeCdp(jwty=[None, "JWT"])
        with mock.patch.object(rt, "jwt_expiry", return_value=9e9), \
                mock.patch("sys.stdout", io.StringIO()):
            jwt, exp = rt.try_silent_login(cdp)
        self.assertEqual(jwt, "JWT")
        self.assertEqual(rt._auto_login_tries, 0, "licznik prób nie wyzerował się po sukcesie")

    def test_never_touches_credential_fields(self):
        """SEDNO BEZPIECZEŃSTWA: jedyna interakcja to kliknięcie linku."""
        cdp = FakeCdp(jwty=[None] * 12, url="https://account.decathlon.com/login")
        with mock.patch("sys.stdout", io.StringIO()):
            rt.try_silent_login(cdp)
        wszystko = " ".join(cdp.wykonane).lower()
        for zakazane in ("password", "value =", ".value=", "input", "submit", "form"):
            self.assertNotIn(zakazane, wszystko, f"czytnik dotknął {zakazane!r}")

    def test_gives_up_and_says_so_when_form_appears(self):
        cdp = FakeCdp(jwty=[None] * 12, url="https://account.decathlon.com/login")
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            jwt, _ = rt.try_silent_login(cdp)
        self.assertIsNone(jwt)
        self.assertIn("zaloguj się ręcznie", buf.getvalue())

    def test_stops_after_max_tries(self):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            for _ in range(rt.AUTO_LOGIN_MAX_TRIES + 2):
                rt._auto_login_last = 0.0        # omijamy karencję, testujemy sam limit
                rt.try_silent_login(FakeCdp(jwty=[None] * 12))
        self.assertEqual(rt._auto_login_tries, rt.AUTO_LOGIN_MAX_TRIES)

    def test_cooldown_blocks_hammering(self):
        with mock.patch("sys.stdout", io.StringIO()):
            rt.try_silent_login(FakeCdp(jwty=[None] * 12))
            pierwsza = rt._auto_login_tries
            rt.try_silent_login(FakeCdp(jwty=[None] * 12))
        self.assertEqual(rt._auto_login_tries, pierwsza, "druga próba mimo karencji")

    def test_disabled_by_option(self):
        with mock.patch.object(rt, "AUTO_LOGIN", False):
            cdp = FakeCdp()
            self.assertEqual(rt.try_silent_login(cdp), (None, 0))
            self.assertEqual(cdp.wykonane, [], "coś wykonano mimo wyłączonej opcji")

    def test_missing_link_is_reported_not_guessed(self):
        cdp = FakeCdp(klik="brak")
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            jwt, _ = rt.try_silent_login(cdp)
        self.assertIsNone(jwt)
        self.assertIn("nie znalazłem linku", buf.getvalue())

    def test_selector_uses_href_not_hashed_class(self):
        """Klasy w tej aplikacji są zahaszowane i zmieniają się przy każdym wydaniu."""
        self.assertIn('a[href="/login"]', rt.LOGIN_LINK_JS)
        self.assertNotIn("Topbar_", rt.LOGIN_LINK_JS)


if __name__ == "__main__":
    unittest.main()
