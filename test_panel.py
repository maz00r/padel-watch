#!/usr/bin/env python3
"""Testy panelu (Ingress). Uruchomienie: python3 -m unittest -v test_panel

Panel nie miał ani jednego testu, a jest jedynym miejscem w całym systemie, które
potrafi ANULOWAĆ rezerwację — działanie nieodwracalne. Do tego serwuje pliki z dysku
i tuneluje websocket, więc odpowiada też za to, żeby przez Ingress nie wyciekło nic
spoza katalogu noVNC.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "padel_browser"))
import check_padel as cp  # noqa: E402
import panel  # noqa: E402


class FakeSocket(io.BytesIO):
    """Gniazdo, które zapamiętuje wysłaną odpowiedź zamiast wysyłać ją w świat."""

    def makefile(self, *args, **kwargs):
        return self


class Zapytanie:
    """Uruchamia handler panelu bez podnoszenia serwera HTTP."""

    def __init__(self, metoda, sciezka, tresc=None, ctype="application/json"):
        naglowki = f"{metoda} {sciezka} HTTP/1.1\r\nHost: x\r\n"
        cialo = b""
        if tresc is not None:
            cialo = json.dumps(tresc).encode()
            naglowki += f"Content-Type: {ctype}\r\nContent-Length: {len(cialo)}\r\n"
        self.surowe = naglowki.encode() + b"\r\n" + cialo

    def wykonaj(self):
        we = FakeSocket(self.surowe)
        wy = FakeSocket()

        class Handler(panel.Handler):
            def __init__(self):          # pomijamy setup socketu
                self.rfile, self.wfile = we, wy
                self.client_address = ("127.0.0.1", 0)
                self.requestline, self.request_version, self.command = "", "HTTP/1.1", ""
                self.handle_one_request()

            def log_message(self, *a):   # cisza w testach
                pass

        with mock.patch("sys.stdout", io.StringIO()):
            Handler()
        return wy.getvalue().decode("utf-8", "replace")


class CancelEndpointTest(unittest.TestCase):
    """`/api/cancel` — jedyne nieodwracalne działanie w całym systemie."""

    def test_cancel_requires_json_content_type(self):
        """Formularz z obcej strony nie ustawi tego nagłówka bez zgody CORS.
        To jedyna bariera przed anulowaniem rezerwacji cudzym żądaniem."""
        with mock.patch.object(cp, "cancel_reservation") as anuluj:
            odp = Zapytanie("POST", "/api/cancel", {"id": "tx1"},
                            ctype="application/x-www-form-urlencoded").wykonaj()
        self.assertIn("415", odp)
        anuluj.assert_not_called()

    def test_cancel_without_id_does_not_call_the_api(self):
        with mock.patch.object(cp, "cancel_reservation") as anuluj:
            odp = Zapytanie("POST", "/api/cancel", {"id": "   "}).wykonaj()
        self.assertIn("400", odp)
        anuluj.assert_not_called()

    def test_cancel_passes_the_identifier_through(self):
        with mock.patch.object(cp, "cancel_reservation",
                               return_value=(True, "cancelled")) as anuluj, \
                mock.patch.object(cp, "credentials_cfg", return_value={}), \
                mock.patch.object(panel, "reservations", return_value=([], None)):
            odp = Zapytanie("POST", "/api/cancel", {"id": "tx-42"}).wykonaj()
        anuluj.assert_called_once()
        self.assertEqual(anuluj.call_args[0][0], "tx-42")
        self.assertIn('"ok": true', odp)

    def test_a_failed_cancel_is_reported_not_swallowed(self):
        with mock.patch.object(cp, "cancel_reservation",
                               return_value=(False, "HTTP 404")), \
                mock.patch.object(cp, "credentials_cfg", return_value={}):
            odp = Zapytanie("POST", "/api/cancel", {"id": "tx-42"}).wykonaj()
        self.assertIn('"ok": false', odp)
        self.assertIn("404", odp)

    def test_unknown_post_endpoint_is_refused(self):
        self.assertIn("404", Zapytanie("POST", "/api/cokolwiek", {}).wykonaj())


class StaticPathTest(unittest.TestCase):
    """Panel serwuje pliki z dysku — przez Ingress nie może wyjść poza katalog noVNC."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        os.makedirs(os.path.join(self.dir.name, "core"), exist_ok=True)
        with open(os.path.join(self.dir.name, "core", "rfb.js"), "w") as f:
            f.write("// noVNC")
        self.tajne = os.path.join(os.path.dirname(self.dir.name), "tajne.txt")
        with open(self.tajne, "w") as f:
            f.write("token")
        self.addCleanup(lambda: os.path.exists(self.tajne) and os.unlink(self.tajne))
        pat = mock.patch.object(panel, "NOVNC_DIR", self.dir.name)
        pat.start()
        self.addCleanup(pat.stop)

    def test_a_real_file_is_served(self):
        self.assertTrue(panel.safe_static_path("/core/rfb.js"))

    def test_traversal_is_refused(self):
        for zla in ("/../tajne.txt", "/core/../../tajne.txt", "/%2e%2e/tajne.txt"):
            self.assertIsNone(panel.safe_static_path(zla), f"wyszło poza katalog: {zla}")

    def test_a_missing_file_is_not_an_error_page_leak(self):
        self.assertIsNone(panel.safe_static_path("/nie-ma-takiego.js"))

    def test_a_directory_is_not_served(self):
        self.assertIsNone(panel.safe_static_path("/core"))


class HuntsEndpointTest(unittest.TestCase):
    """`/api/hunts` czyta wyłącznie plik z dysku — nie może odpalać polowania."""

    def test_a_corrupt_journal_does_not_break_the_panel(self):
        with mock.patch.object(cp, "load_hunts", side_effect=ValueError("uszkodzony")):
            odp = Zapytanie("GET", "/api/hunts").wykonaj()
        self.assertTrue(odp.startswith("HTTP/1.0 5") or "500" in odp or '"ok": false' in odp,
                        f"panel nie poradził sobie z uszkodzonym dziennikiem: {odp[:120]}")


class AccountsEndpointTest(unittest.TestCase):
    def test_accounts_endpoint_returns_public_state(self):
        with mock.patch.object(panel, "accounts_view", return_value=[{
                "id": "ania", "name": "Ania", "alive": True}]):
            odp = Zapytanie("GET", "/api/accounts").wykonaj()
        self.assertIn('"id": "ania"', odp)
        self.assertNotIn("jwt", odp.lower())

    def test_login_request_is_written_atomically(self):
        accounts = [{"id": "glowne", "main": True}, {"id": "ania", "main": False}]
        with mock.patch.object(cp, "load_config", return_value={}), \
                mock.patch.object(cp, "konta_z_konfiguracji", return_value=accounts), \
                mock.patch.object(cp, "zapisz_json_atomowo") as write:
            odp = Zapytanie("POST", "/api/account-login", {"id": "ania"}).wykonaj()
        self.assertIn('"ok": true', odp)
        self.assertEqual(write.call_args.args[1]["state"], "pending")
        self.assertEqual(write.call_args.args[1]["id"], "ania")

    def test_main_account_is_not_sent_to_the_extra_browser(self):
        accounts = [{"id": "glowne", "main": True}]
        with mock.patch.object(cp, "load_config", return_value={}), \
                mock.patch.object(cp, "konta_z_konfiguracji", return_value=accounts), \
                mock.patch.object(cp, "zapisz_json_atomowo") as write:
            odp = Zapytanie("POST", "/api/account-login", {"id": "glowne"}).wykonaj()
        self.assertIn("400", odp)
        write.assert_not_called()

    def test_another_active_login_is_not_overwritten(self):
        accounts = [{"id": "glowne", "main": True}, {"id": "ania", "main": False}]
        with mock.patch.object(cp, "load_config", return_value={}), \
                mock.patch.object(cp, "konta_z_konfiguracji", return_value=accounts), \
                mock.patch.object(panel.zbieracz, "wczytaj_json",
                                  return_value={"id": "marek", "state": "active"}), \
                mock.patch.object(cp, "zapisz_json_atomowo") as write:
            odp = Zapytanie("POST", "/api/account-login", {"id": "ania"}).wykonaj()
        self.assertIn("409", odp)
        write.assert_not_called()

    def test_extra_websocket_has_a_separate_port(self):
        with mock.patch.object(panel, "WEBSOCKIFY_PORT", 6080), \
                mock.patch.object(panel, "EXTRA_WEBSOCKIFY_PORT", 6081):
            self.assertEqual(panel.websocket_port("/websockify"), 6080)
            self.assertEqual(panel.websocket_port("/prefix/websockify-extra"), 6081)


class ReservationsOfflineCacheTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = os.path.join(self.directory.name, "reservations-cache.json")
        patcher = mock.patch.object(panel, "RESERVATIONS_CACHE_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        panel._cache.update(at=0, items=None, error=None)
        self.addCleanup(
            lambda: panel._cache.update(at=0, items=None, error=None)
        )

    @staticmethod
    def reservation(ident):
        return {
            "id": ident,
            "state": "confirmed",
            "cancelled": False,
            "past": False,
            "start_utc": datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc),
            "minutes": 60,
            "when": "śr 16.09 17:00",
            "date_label": "śr 16.09",
            "day": "16.09.2026",
            "hours": "17:00–18:00",
            "title": "Padel",
            "slot_name": "Kort 1",
            "address": "Warszawa",
            "participants": ["Ania"],
            "book_url": "https://go.decathlon.pl/l/kort",
        }

    def test_logged_out_account_keeps_its_last_successful_reservations(self):
        accounts = [
            {"id": "glowne", "name": "Główne", "main": True},
            {"id": "ania", "name": "Ania", "main": False},
        ]
        offline = {"ania": False}

        def credentials(account=None):
            return {"account_id": account["id"] if account else "glowne"}

        def view(cfg, _tz):
            kid = cfg["account_id"]
            if kid == "ania" and offline["ania"]:
                return None, "brak tokenu — zaloguj się"
            return ([self.reservation("tx-ania")] if kid == "ania" else []), None

        with mock.patch.object(cp, "load_config", return_value={}), \
                mock.patch.object(cp, "konta_z_konfiguracji", return_value=accounts), \
                mock.patch.object(cp, "credentials_cfg", side_effect=credentials), \
                mock.patch.object(cp, "reservations_view", side_effect=view):
            first, error = panel.reservations(force=True)
            self.assertIsNone(error)
            self.assertEqual([row["id"] for row in first], ["tx-ania"])

            # Symulacja restartu panelu i wygasłego tokenu konta dodatkowego.
            panel._cache.update(at=0, items=None, error=None)
            offline["ania"] = True
            second, error = panel.reservations(force=True)

        self.assertEqual([row["id"] for row in second], ["tx-ania"])
        self.assertEqual(second[0]["account_name"], "Ania")
        self.assertIn("ostatni poprawny stan", error)

    def test_reservations_endpoint_reports_warning_instead_of_fatal_error(self):
        with mock.patch.object(panel, "reservations", return_value=([], "Ania: brak tokenu")):
            response = Zapytanie("GET", "/api/reservations").wykonaj()
        self.assertIn('"ok": true', response)
        self.assertIn('"warning": "Ania: brak tokenu"', response)

    def test_calendar_uses_available_items_despite_an_offline_account(self):
        item = self.reservation("tx-ania")
        with mock.patch.object(
            panel, "reservations", return_value=([item], "Główne: brak tokenu")
        ):
            response = Zapytanie("GET", "/cal/all.ics").wykonaj()
        self.assertIn("200", response.splitlines()[0])
        self.assertIn("BEGIN:VCALENDAR", response)
        self.assertIn("UID:tx-ania@padel-watch", response)

    def test_calendar_still_fails_when_nothing_was_ever_loaded(self):
        with mock.patch.object(panel, "reservations", return_value=([], "brak tokenu")):
            response = Zapytanie("GET", "/cal/all.ics").wykonaj()
        self.assertIn("503", response.splitlines()[0])

    def test_cache_is_refreshed_once_just_after_the_hunt(self):
        publication = datetime(2026, 9, 9, 11, 0, tzinfo=timezone.utc)
        target = publication.timestamp() + 75 + panel.POST_HUNT_CACHE_DELAY
        panel._post_hunt_cached_on = None
        self.addCleanup(setattr, panel, "_post_hunt_cached_on", None)
        with mock.patch.object(cp, "burst_start_today", return_value=publication), \
                mock.patch.object(panel.zbieracz, "wymagany_exp",
                                  return_value=publication.timestamp() + 75), \
                mock.patch.object(panel, "reservations", return_value=([], None)) as refresh:
            self.assertTrue(panel.refresh_reservations_after_hunt(target))
            self.assertFalse(panel.refresh_reservations_after_hunt(target + 1))
        refresh.assert_called_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
