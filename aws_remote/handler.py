"""Sprint i salwa wykonywane w AWS eu-west-1, obok serwera Decathlona.

Po co: `go.decathlon.pl` to load balancer w eu-west-1. Zmierzone 11.08 z Lambdy
w tym samym regionie (1769 MB, pełny rdzeń): runda **0,5 ms** zamiast ~42 ms z domu,
ciężkie pobranie **39 ms** zamiast ~80 ms. Ścieżka „termin pojawia się na serwerze →
nasze żądanie tam dociera" spada ze ~107 ms do ~32 ms.

Co tu JEST: wykrywanie publikacji (sprint) i rejestracja (salwa).
Czego tu NIE MA i nigdy nie będzie: logowania, przeglądarki, stanu, powiadomień,
kalendarza, panelu. To wszystko zostaje w Home Assistancie.

TOKEN: przychodzi w treści żądania, żyje w pamięci przez jedno wywołanie i **nigdzie
nie jest zapisywany** — ani w zmiennych środowiskowych, ani w Secrets Managerze,
ani w zapisanym zdarzeniu testowym. Trafia natomiast do dziennika tylko jako długość,
nigdy jako wartość.

Cała logika (parser, filtry, salwa, limity) pochodzi z `check_padel.py` dołączonego
do paczki. To celowe: dwie osobne implementacje filtrów albo limitu rezerwacji
rozjechałyby się przy pierwszej zmianie, a skutkiem byłaby rezerwacja terminu,
którego użytkownik nie chce, albo o jeden za dużo.
"""

import base64
import contextlib
import gzip
import io
import json
import os
import time

import check_padel as cp

SEKRET_NAGLOWEK = "x-padel-secret"
# Pamięć, przy której Lambda daje PEŁNY rdzeń. Poniżej dostajesz jego ułamek,
# a moc procesora decyduje tu o wszystkim: TLS, parsowanie 260 KB JSON-a w każdym
# wątku sprintu i równoległe strzały salwy. W sondzie (`aws_probe/`) przy 512 MB
# samo uzgodnienie TLS zajmowało 17 ms zamiast 3 ms.
LAMBDA_PELNY_RDZEN_MB = 1769
# Twardy sufit na okno sprintu. Wywołujący i tak podaje własne, krótsze, ale bez
# sufitu literówka w konfiguracji dodatku potrafiłaby trzymać funkcję do timeoutu.
MAX_SPRINT_SEKUND = 20


def _sekret_ok(event):
    """Nagłówki w Function URL przychodzą małymi literami, ale nie polegamy na tym."""
    oczekiwany = os.environ.get("PADEL_SECRET") or ""
    if not oczekiwany:
        return False, "funkcja nie ma ustawionego PADEL_SECRET"
    naglowki = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    podany = naglowki.get(SEKRET_NAGLOWEK, "")
    # Porównanie odporne na pomiar czasu — sekret jest jedyną bramką tej funkcji.
    import hmac
    if not hmac.compare_digest(podany, oczekiwany):
        return False, "zły sekret"
    return True, ""


def _tresc(event):
    """Ciało żądania: Function URL potrafi je podać zwykłym tekstem albo w base64."""
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body)


def _odpowiedz(dane, kod=200):
    """Spakowana gzipem odpowiedź — dokument kortu ma ~260 KB, spakowany ~25 KB.

    Leci już PO rejestracji, więc nie jest na ścieżce krytycznej, ale nie ma powodu
    ciągnąć ćwierć megabajta przez łącze domowe.
    """
    surowe = json.dumps(dane, ensure_ascii=False, default=str).encode("utf-8")
    return {
        "statusCode": kod,
        "headers": {"Content-Type": "application/json", "Content-Encoding": "gzip"},
        "body": base64.b64encode(gzip.compress(surowe, 6)).decode("ascii"),
        "isBase64Encoded": True,
    }


def poluj(wejscie):
    """Sprint + salwa. Zwraca słownik wyniku; nie podnosi wyjątków sieciowych."""
    started = time.monotonic()
    # Przydział pamięci = przydział CPU. Raportujemy go ZAWSZE, bo bez tej liczby
    # nie da się odróżnić „serwer Decathlona jest wolny" od „nasza funkcja jest
    # wygłodzona" — a to dwa zupełnie różne problemy z różnymi rozwiązaniami.
    pamiec = os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE")
    if pamiec:
        cp.log(f"Lambda: {pamiec} MB" + (
            f" — UWAGA, to UŁAMEK rdzenia. Ustaw {LAMBDA_PELNY_RDZEN_MB} MB, "
            f"inaczej strzały są wolne z NASZEJ winy, nie Decathlona."
            if int(pamiec) < LAMBDA_PELNY_RDZEN_MB else " (pełny rdzeń)"))
    tz = cp.ZoneInfo(wejscie.get("timezone") or "Europe/Warsaw") if cp.ZoneInfo else None
    # Filtry parsujemy TĄ SAMĄ funkcją co dodatek, z tego samego surowego napisu.
    filtry = []
    surowe_filtry = (wejscie.get("filters") or "").strip()
    if surowe_filtry:
        filtry = cp.parse_filters_env(surowe_filtry)

    sekundy = max(1, min(float(wejscie.get("sprint_seconds") or 4), MAX_SPRINT_SEKUND))
    watki = int(wejscie.get("sprint_threads") or 3)
    baseline = set(wejscie.get("baseline_ids") or [])

    # Rozgrzewka połączeń salwy — dokładnie ta sama osłona co w dodatku. W regionie
    # kosztuje kilka ms, a zdejmuje z równania uzgadnianie TLS w chwili strzału.
    # NIE jest wyjaśnieniem wolnych strzałów z 13.08, tylko usunięciem jednej zmiennej.
    salwa = min(int(wejscie.get("salvo") or 0), cp.SALVO_MAX)
    if salwa > 1:
        rozgrzane = time.monotonic()
        cp.warm_connections(cp.salvo_pool(salwa), salwa,
                            cp.LISTING_URL.format(id=cp.listing_id_from_url(wejscie["listing_url"])))
        cp.log(f"Salwa rozgrzana [{int((time.monotonic() - rozgrzane) * 1000)} ms]")

    szukanie = time.monotonic()
    trafienie = cp.run_sprint(szukanie + sekundy, watki, wejscie["listing_url"],
                              baseline, tz, filters=filtry)
    sprint_ms = int((time.monotonic() - szukanie) * 1000)
    if not trafienie:
        return {"ok": True, "doc": None, "listing_id": None,
                "timings": {"sprint_ms": sprint_ms,
                            "total_ms": int((time.monotonic() - started) * 1000)}}

    lid, doc = trafienie
    teraz = cp.datetime.now(cp.timezone.utc)
    sloty = [s for s in cp.free_slots(doc, lid, teraz) if cp.passes_filter(s, filtry, tz)]
    cp.log(f"Sprint: nowe terminy po {sprint_ms} ms — {len(sloty)} pasujących do filtra")

    wyniki, zapisane = {}, set()
    if sloty and wejscie.get("enabled"):
        reg_cfg = {
            "enabled": True,
            "speculative": bool(wejscie.get("speculative")),
            "token": wejscie.get("token") or "",
            # browser_mode=True, mimo że tu ŻADNEJ przeglądarki nie ma. Chodzi
            # o semantykę wygaśnięcia, nie o przeglądarkę:
            #   False -> token uznany za wygasły już na TOKEN_EXPIRY_MARGIN (300 s)
            #            przed czasem i próba serwerowego /auth/refresh, która
            #            w Decathlon GO ZAWSZE kończy się 401,
            #   True  -> wygasły znaczy exp w przeszłości, bez żadnego refreshu.
            # Przy tokenie żyjącym ~15 min ustawienie False wywracało rejestrację
            # przez ostatnią 1/3 jego życia. Tak przepadło 17:00 w dniu 12.08.
            # Czekania na świeższy token z pliku nie ma się co bać: bez TOKEN_FILE
            # `wait_for_fresher_token` wraca natychmiast.
            "browser_mode": True,
            "refresh_cookie": "", "refresh_token": "",
            "name": wejscie.get("name") or "",
            "age": wejscie.get("age"),
            "free_only": bool(wejscie.get("free_only", True)),
            "max_per_run": wejscie.get("max_per_run") or 1,
            "order": wejscie.get("order") or "earliest",
            "salvo": wejscie.get("salvo") or 0,
            # Odstęp musi być IDENTYCZNY po obu stronach — inaczej zapas lokalny
            # strzelałby inaczej niż Irlandia i porównanie logów przestałoby mieć sens.
            "stagger": wejscie.get("stagger", cp.SALVO_STAGGER_MS),
        }
        ceny = {s["id"]: (doc.get("data", {}).get("attributes", {}) or {}).get("price")
                for s in sloty}
        wyniki, zapisane = cp.auto_register_new_slots(sloty, ceny, reg_cfg, set())

    return {
        "ok": True,
        "listing_id": lid,
        "doc": doc,
        "results": {k: [bool(v[0]), v[1]] for k, v in wyniki.items()},
        "registered": sorted(zapisane),
        "timings": {"sprint_ms": sprint_ms,
                    "total_ms": int((time.monotonic() - started) * 1000)},
    }


def lambda_handler(event, context):   # noqa: ARG001 - kontrakt AWS
    ok, powod = _sekret_ok(event or {})
    if not ok:
        # Bez szczegółów w treści: to jest publicznie osiągalny adres.
        print(f"odrzucone żądanie: {powod}")
        return {"statusCode": 403, "body": "forbidden"}

    try:
        wejscie = _tresc(event)
    except (ValueError, TypeError) as e:
        return _odpowiedz({"ok": False, "blad": f"zła treść żądania: {e!r}"}, 400)
    if wejscie.get("warm"):
        # Rozgrzewka: dodatek puka tu na starcie zrywu, ~20 s przed publikacją.
        # Bez tego pierwsze wywołanie dnia płaci zimny start (~165 ms na init
        # plus ~75 ms na import silnika) dokładnie w chwili, gdy liczy się najbardziej.
        print("rozgrzewka")
        return _odpowiedz({"ok": True, "warm": True})
    if not wejscie.get("listing_url"):
        return _odpowiedz({"ok": False, "blad": "brak listing_url"}, 400)

    print(f"start: token {len(wejscie.get('token') or '')} znaków, "
          f"okno {wejscie.get('sprint_seconds')} s, salwa {wejscie.get('salvo')}")

    # Dziennik silnika przechwytujemy, żeby ODESŁAĆ go do Home Assistanta. Bez tego
    # jedyny ślad po sekundzie publikacji zostawałby w CloudWatch, a użytkownik czyta
    # Dziennik dodatku. Kopię i tak drukujemy, więc w CloudWatch też jest.
    bufor = io.StringIO()
    try:
        with contextlib.redirect_stdout(bufor):
            wynik = poluj(wejscie)
    except Exception as e:  # noqa: BLE001 - błąd tutaj nie może zostać bez odpowiedzi
        print(bufor.getvalue())
        print(f"BŁĄD polowania: {e!r}")
        return _odpowiedz({"ok": False, "blad": repr(e), "log": bufor.getvalue().splitlines()}, 500)

    dziennik = bufor.getvalue()
    print(dziennik)
    wynik["log"] = [w for w in dziennik.splitlines() if w.strip()]
    return _odpowiedz(wynik)
