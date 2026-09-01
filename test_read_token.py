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

    def call(self, metoda, **kw):
        """`read_jwt_once` steruje stroną przez Page.enable / Page.navigate."""
        self.wykonane.append((metoda, kw))
        return {}

    def close(self):
        self.zamkniete = True


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
                # Cofamy tylko o karencję. Cofnięcie „do zera" przedawniłoby też sam
                # limit (AUTO_LOGIN_RESET_AFTER) i test mierzyłby co innego, niż mówi.
                rt._auto_login_last = time.time() - rt.AUTO_LOGIN_COOLDOWN - 1
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


class ReaderLogLevelTest(unittest.TestCase):
    """Czytnik pisze do tego samego logu co monitor, więc filtr musi działać tak samo.
    Jego bicie serca co 5 minut to ~290 linii dziennie i zero informacji."""

    def zloguj(self, *args, prog="info"):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"LOG_LEVEL": prog}):
            stary, sys.stdout = sys.stdout, buf
            try:
                rt.log(*args)
            finally:
                sys.stdout = stary
        return buf.getvalue()

    def test_glyph_convention_matches_the_monitor(self):
        self.assertIn("✗", self.zloguj("✗ Ciche logowanie nie wystarczyło", prog="error"))
        self.assertIn("⚠", self.zloguj("⚠ JWT WYGASŁ", prog="warn"))
        self.assertEqual("", self.zloguj("✓ JWT odczytany", prog="warn"))

    def test_broken_level_name_does_not_silence_the_reader(self):
        self.assertIn("✓", self.zloguj("✓ JWT odczytany", prog="bzdura"))

    def test_heartbeat_is_quiet_but_recovery_is_loud(self):
        """SEDNO: chcemy zobaczyć POWRÓT tokenu po awarii, nie 290 potwierdzeń, że żyje.

        Pętla główna nadaje udanemu odczytowi „info" tylko wtedy, gdy poprzedni się nie
        udał; kolejne dostają „debug". Tu sprawdzamy oba końce tej decyzji.
        """
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"LOG_LEVEL": "info"}):
            stary, sys.stdout = sys.stdout, buf
            try:
                rt.log("✓ JWT odczytany — powrót po awarii", level="info")
                rt.log("✓ JWT odczytany — rutynowe bicie serca", level="debug")
            finally:
                sys.stdout = stary
        self.assertIn("powrót po awarii", buf.getvalue())
        self.assertNotIn("bicie serca", buf.getvalue())


def jwt_wazny(sekund=3600):
    """JWT z odległym `exp` — czytnik ma go uznać za żywy."""
    import base64 as b64, json as js
    naglowek = b64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    tresc = b64.urlsafe_b64encode(
        js.dumps({"exp": int(time.time()) + sekund}).encode()).rstrip(b"=").decode()
    return f"{naglowek}.{tresc}.podpis"


class SilentLoginRecoversAfterManualLoginTest(unittest.TestCase):
    """SEDNO: po ręcznym zalogowaniu ciche logowanie MUSI znów działać.

    Zgłoszone 01.09: „czemu zdarza się, że konto się wyloguje, a aplikacja nie próbuje
    ponownie kliknąć zaloguj? Robię to manualnie i jest to wystarczające."

    Przyczyna: `_auto_login_tries` zerowało się WYŁĄCZNIE po udanym logowaniu CICHYM.
    Sekwencja, która wyłączała funkcję na zawsze:
      1. sesja pada w nocy, trzy ciche próby w 20 min — wszystkie nieudane,
      2. licznik stoi na 3, funkcja milknie,
      3. użytkownik loguje się ręcznie, dodatek działa tygodniami,
      4. sesja pada znowu — i NIC się nie dzieje, bo licznik nadal stoi na 3.
    """

    def setUp(self):
        rt.zapomnij_nieudane_logowania()
        rt._auto_login_last = 0.0
        self.addCleanup(rt.zapomnij_nieudane_logowania)

    def wyczerp_proby(self):
        """Trzy nieudane próby — dokładnie to, co robi noc z martwą sesją."""
        for _ in range(rt.AUTO_LOGIN_MAX_TRIES):
            rt._auto_login_last = time.time() - rt.AUTO_LOGIN_COOLDOWN - 1
            cdp = FakeCdp(klik="klik", jwty=[None] * 12, url="https://account.decathlon.com/login")
            with mock.patch("sys.stdout", io.StringIO()), \
                    mock.patch.object(rt.time, "sleep"):
                rt.try_silent_login(cdp)
        self.assertEqual(rt._auto_login_tries, rt.AUTO_LOGIN_MAX_TRIES)

    def test_manual_login_re_arms_silent_login(self):
        self.wyczerp_proby()
        rt.zapomnij_nieudane_logowania()   # tak wygląda skutek ręcznego zalogowania
        rt._auto_login_last = time.time() - rt.AUTO_LOGIN_COOLDOWN - 1
        cdp = FakeCdp(klik="klik", jwty=["swiezy-jwt"])
        with mock.patch("sys.stdout", io.StringIO()), mock.patch.object(rt.time, "sleep"):
            jwt, _exp = rt.try_silent_login(cdp)
        self.assertEqual(jwt, "swiezy-jwt", "po ręcznym logowaniu cicha próba nadal milczy")

    def test_a_valid_token_read_is_what_re_arms_it(self):
        """To odczyt ważnego tokenu ma zerować licznik — bez względu na to, kto zalogował."""
        self.wyczerp_proby()
        cdp = FakeCdp(jwty=[jwt_wazny()], url="https://go.decathlon.pl/")
        with mock.patch.object(rt, "cdp_page_target", return_value="ws://x"), \
                mock.patch.object(rt, "Cdp", return_value=cdp), \
                mock.patch("sys.stdout", io.StringIO()):
            jwt, _exp, blad = rt.read_jwt_once()
        self.assertIsNone(blad)
        self.assertIsNotNone(jwt)
        self.assertEqual(rt._auto_login_tries, 0, "ważny token nie skasował licznika prób")

    def test_the_limit_expires_on_its_own_after_a_long_silence(self):
        """Awaria dostawcy tożsamości nie może wyłączyć funkcji aż do restartu dodatku."""
        self.wyczerp_proby()
        rt._auto_login_last = time.time() - rt.AUTO_LOGIN_RESET_AFTER - 1
        cdp = FakeCdp(klik="klik", jwty=["swiezy-jwt"])
        with mock.patch("sys.stdout", io.StringIO()), mock.patch.object(rt.time, "sleep"):
            jwt, _exp = rt.try_silent_login(cdp)
        self.assertEqual(jwt, "swiezy-jwt")

    def test_the_limit_still_holds_inside_the_window(self):
        """Nie znosimy limitu — ma nadal chronić przed młóceniem strony."""
        self.wyczerp_proby()
        rt._auto_login_last = time.time()
        cdp = FakeCdp(klik="klik", jwty=["swiezy-jwt"])
        with mock.patch("sys.stdout", io.StringIO()), mock.patch.object(rt.time, "sleep"):
            jwt, _exp = rt.try_silent_login(cdp)
        self.assertIsNone(jwt, "limit prób przestał chronić")
