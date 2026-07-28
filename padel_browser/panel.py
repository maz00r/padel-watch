#!/usr/bin/env python3
"""Panel dodatku (Home Assistant Ingress): moje rezerwacje + przeglądarka.

Ingress udostępnia dokładnie JEDEN port, a potrzebujemy trzech rzeczy naraz,
więc ten serwer stoi na 8099 i rozdziela ruch:
  * `/`                 -> strona panelu (zakładki: Rezerwacje / Przeglądarka),
  * `/api/*`, `/cal/*`  -> lista rezerwacji, anulowanie, kalendarz .ics,
  * `/websockify`       -> tunel bajt-w-bajt do websockify (obraz z Chromium),
  * reszta              -> pliki statyczne noVNC z dysku.

Cała logika rozmowy z Decathlon GO siedzi w check_padel.py — tutaj tylko HTTP.
"""

import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import check_padel

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - w obrazie zawsze jest
    ZoneInfo = None

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL_PORT = int(os.environ.get("PANEL_PORT") or 8099)
WEBSOCKIFY_PORT = int(os.environ.get("WEBSOCKIFY_PORT") or 6080)
NOVNC_DIR = os.environ.get("NOVNC_DIR") or "/usr/share/novnc"
PANEL_HTML = os.path.join(HERE, "panel.html")
# Lista rezerwacji zmienia się rzadko, a panel odpytywany jest przy każdym wejściu
# w zakładkę — krótki bufor oszczędza serwerowi GO zbędnych zapytań.
CACHE_TTL = 20
ERROR_TTL = 5

MIME = {
    ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".map": "application/json",
}

_cache = {"at": 0.0, "items": None, "error": None}
_cache_lock = threading.Lock()


def log(*args):
    print("[panel]", *args, flush=True)


def _tz():
    name = os.environ.get("TIMEZONE") or "Europe/Warsaw"
    try:
        return ZoneInfo(name) if ZoneInfo else timezone.utc
    except Exception:  # noqa: BLE001 - zła strefa nie może wywrócić panelu
        return timezone.utc


def reservations(force=False):
    """(lista, błąd) z krótkim buforem. force=True po anulowaniu — stan właśnie się zmienił."""
    with _cache_lock:
        ttl = ERROR_TTL if _cache["error"] else CACHE_TTL
        fresh = _cache["at"] > time.time() - ttl
        if not force and fresh and (_cache["items"] is not None or _cache["error"]):
            return _cache["items"], _cache["error"]
    cfg = check_padel.credentials_cfg()
    try:
        items, error = check_padel.reservations_view(cfg, _tz())
    except Exception as e:  # noqa: BLE001 - panel ma pokazać błąd, nie paść
        items, error = None, f"nieoczekiwany błąd: {e!r}"
    if error:
        # Bez tego czerwony komunikat w panelu nie zostawiał ŻADNEGO śladu w Dzienniku
        # i nie dało się dojść, co właściwie się stało.
        log(f"! nie pobrałem rezerwacji: {error}")
    with _cache_lock:
        _cache.update(at=time.time(), items=items, error=error)
    return items, error


def safe_static_path(path):
    """Ścieżka pliku noVNC albo None. Normalizacja ucina każdą próbę wyjścia poza katalog."""
    target = os.path.realpath(os.path.join(NOVNC_DIR, unquote(path).lstrip("/")))
    root = os.path.realpath(NOVNC_DIR)
    if target != root and not target.startswith(root + os.sep):
        return None
    return target if os.path.isfile(target) else None


def public_reservation(res):
    """Rezerwacja w formie, którą rozumie JavaScript panelu (bez obiektów datetime)."""
    out = {k: res.get(k) for k in (
        "id", "state", "cancelled", "past", "when", "date_label", "day", "hours",
        "title", "slot_name", "address", "participants", "minutes", "book_url",
    )}
    out["start"] = res["start_utc"].isoformat() if res.get("start_utc") else None
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "padel-panel"

    def log_message(self, fmt, *args):  # cisza — Dziennik dodatku ma być czytelny
        pass

    # ------------------------------------------------------------- odpowiedzi

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        try:
            doc = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return {}
        return doc if isinstance(doc, dict) else {}

    # ---------------------------------------------------------------- routing

    def do_GET(self):
        path = urlparse(self.path).path
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            return self.tunnel_websocket()
        if path in ("/", "/index.html"):
            return self.serve_panel()
        if path == "/api/reservations":
            return self.api_reservations()
        if path == "/api/raw":
            return self.api_raw()
        if path.startswith("/cal/") and path.endswith(".ics"):
            return self.serve_ics(path[len("/cal/"):-len(".ics")])
        return self.serve_static(path)

    do_HEAD = do_GET

    def do_POST(self):
        if urlparse(self.path).path == "/api/cancel":
            return self.api_cancel()
        self._json({"ok": False, "error": "nieznany endpoint"}, 404)

    # --------------------------------------------------------------- zasoby

    def serve_panel(self):
        try:
            with open(PANEL_HTML, "rb") as f:
                page = f.read()
        except OSError as e:
            return self._send(500, f"Brak panel.html: {e}", "text/plain; charset=utf-8")
        self._send(200, page, "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def serve_static(self, path):
        """Pliki noVNC z dysku (vnc.html, core/*, app/*)."""
        target = safe_static_path(path)
        if not target:
            return self._send(404, "Nie znaleziono", "text/plain; charset=utf-8")
        try:
            with open(target, "rb") as f:
                data = f.read()
        except OSError:
            return self._send(404, "Nie znaleziono", "text/plain; charset=utf-8")
        ctype = MIME.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        self._send(200, data, ctype)

    # ------------------------------------------------------------------- API

    def api_reservations(self):
        force = "refresh=1" in (urlparse(self.path).query or "")
        items, error = reservations(force=force)
        if error:
            return self._json({"ok": False, "error": error})
        self._json({
            "ok": True,
            "items": [public_reservation(r) for r in items],
            "generated": datetime.now(_tz()).strftime("%H:%M:%S"),
        })

    def api_raw(self):
        """Surowa odpowiedź GO — do diagnostyki, gdy panel czegoś nie rozpozna."""
        try:
            raw, error = check_padel.fetch_my_reservations(check_padel.credentials_cfg())
        except Exception as e:  # noqa: BLE001 - diagnostyka ma pokazać błąd, nie 500
            raw, error = None, f"nieoczekiwany błąd: {e!r}"
        self._json({"ok": not error, "error": error, "items": raw or []})

    def api_cancel(self):
        # Anulowanie jest nieodwracalne, więc przyjmujemy je tylko jako JSON. Formularz
        # z obcej strony nie ustawi tego nagłówka bez zgody CORS — a panel i tak siedzi
        # za tokenem Ingressu, którego obcy nie zna.
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json({"ok": False, "message": "wymagany Content-Type: application/json"}, 415)
        tx_id = str(self._body().get("id") or "").strip()
        if not tx_id:
            return self._json({"ok": False, "message": "brak identyfikatora rezerwacji"}, 400)
        log(f"żądanie anulowania rezerwacji {tx_id}")
        ok, message = check_padel.cancel_reservation(tx_id, check_padel.credentials_cfg())
        if not ok:
            log(f"! anulowanie nieudane: {message}")
        if ok:
            reservations(force=True)  # lista właśnie się zmieniła — nie pokazuj starej
        self._json({"ok": ok, "message": message})

    def serve_ics(self, ident):
        items, error = reservations()
        if error:
            return self._send(503, f"Nie mogę pobrać rezerwacji: {error}", "text/plain; charset=utf-8")
        if ident == "all":
            wanted = [r for r in items if not r["cancelled"] and not r["past"]]
            name = "padel-rezerwacje.ics"
        else:
            wanted = [r for r in items if r["id"] == ident]
            name = f"padel-{ident[:8]}.ics"
        if not wanted:
            return self._send(404, "Brak rezerwacji do zapisania", "text/plain; charset=utf-8")
        body = check_padel.reservations_ics(wanted)
        self._send(200, body, "text/calendar; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="{name}"'})

    # -------------------------------------------------------- tunel websocket

    def tunnel_websocket(self):
        """Przepuszcza uścisk dłoni i ramki do websockify — Ingress ma tylko jeden port.

        Po uścisku dłoni to już zwykły strumień bajtów w obie strony, więc nie
        interpretujemy ramek: dwa wątki przepychają dane, aż któraś strona zamknie.
        """
        try:
            upstream = socket.create_connection(("127.0.0.1", WEBSOCKIFY_PORT), timeout=10)
        except OSError as e:
            return self._send(502, f"noVNC jeszcze nie wystartował ({e})",
                              "text/plain; charset=utf-8")
        self.close_connection = True
        try:
            head = [self.raw_requestline.rstrip(b"\r\n")]
            head += [f"{k}: {v}".encode("latin-1", "replace") for k, v in self.headers.items()]
            upstream.sendall(b"\r\n".join(head) + b"\r\n\r\n")
            upstream.settimeout(None)
            self.connection.settimeout(None)
            pump = threading.Thread(target=self._upstream_to_client, args=(upstream,), daemon=True)
            pump.start()
            while True:
                data = self.rfile.read1(65536)
                if not data:
                    break
                upstream.sendall(data)
        except OSError:
            pass
        finally:
            try:
                upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            upstream.close()

    def _upstream_to_client(self, upstream):
        try:
            while True:
                data = upstream.recv(65536)
                if not data:
                    break
                self.wfile.write(data)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            try:
                self.connection.shutdown(socket.SHUT_RD)
            except OSError:
                pass


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), Handler)
    server.daemon_threads = True
    log(f"panel na porcie {PANEL_PORT} (noVNC: {NOVNC_DIR}, websockify: {WEBSOCKIFY_PORT})")
    server.serve_forever()


if __name__ == "__main__":
    main()
