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


if __name__ == "__main__":
    unittest.main()
