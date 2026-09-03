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
# Okno sprintu musi POKRYĆ rozrzut pory publikacji, a ten okazał się ogromny:
# 11 dni od 23.08 do 03.09 dało pory od 11:00:13 do 11:00:42. Dziesięciosekundowe okno
# trafiało w 5 dni na 11; w pozostałe strzelaliśmy z domu, gdzie zapis trwa 448-1303 ms
# zamiast 66-178 ms z regionu. Limit musi więc pozwolić na okno rzędu 40 s.
# Timeout funkcji w konsoli AWS musi być WIĘKSZY niż to okno — inaczej Lambda zostanie
# ubita w trakcie obserwacji i nie odda nawet tego, co zdążyła zarezerwować.
MAX_SPRINT_SEKUND = 45


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

    # KONFIGURACJA REJESTRACJI — budowana RAZ, przed pętlą partii. Musi przeżyć
    # wszystkie rundy, bo niesie token (odświeżony przez którykolwiek strzał)
    # i licznik zdobyczy.
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
        # Strzał czołowy musi działać TAK SAMO po obu stronach — inaczej zapas
        # lokalny testowałby inną hipotezę niż Irlandia i porównanie logów
        # przestałoby cokolwiek znaczyć.
        "lead": wejscie.get("lead", False),
        "hedge": wejscie.get("hedge") or 1,
    }
    limit = reg_cfg["max_per_run"]
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 1

    # PĘTLA PARTII. Publikacja nie przychodzi naraz — 01.09 grafik sypnął dwiema partiami
    # w odstępie ~450 ms. Stara wersja kończyła się na PIERWSZYM trafieniu: sprint
    # przestawał obserwować, rejestrował, wracał do domu, dodatek przetwarzał wynik
    # i dopiero wtedy wołał Irlandię ponownie. Powstawało ~620 ms ślepoty dokładnie
    # w kaskadzie publikacji — i w tym oknie 18:00 oraz 20:00 pojawiły się i zniknęły.
    # Nie przegraliśmy ich w wyścigu; nie oddaliśmy w nie ANI JEDNEGO strzału.
    #
    # Teraz jedno wywołanie obserwuje do końca okna i rejestruje każdą partię, jaka
    # się pojawi. Powrót do domu następuje raz, po wszystkim.
    szukanie = time.monotonic()
    koniec = szukanie + sekundy
    widziane = set(baseline)
    wyniki, zapisane = {}, set()
    lid, doc, partie = None, None, 0

    while time.monotonic() < koniec:
        runda = time.monotonic()
        trafienie = cp.run_sprint(koniec, watki, wejscie["listing_url"],
                                  widziane, tz, filters=filtry)
        if not trafienie:
            break                      # okno minęło bez nowych terminów
        lid, doc, zobaczone = trafienie
        partie += 1
        teraz = cp.datetime.now(cp.timezone.utc)
        wszystkie = cp.free_slots(doc, lid, teraz)
        sloty = [s for s in wszystkie if cp.passes_filter(s, filtry, tz)]
        nowe = [s for s in sloty if s["id"] not in widziane]
        # GWARANCJA POSTĘPU: do punktu odniesienia trafia KAŻDY wolny termin z tego
        # dokumentu, także odfiltrowany. `run_sprint` wyzwala się na podzbiorze tego
        # zbioru, więc po tej aktualizacji nie może trafić drugi raz w to samo.
        # Bez tego runda, w której wszystko odpadło na filtrze, wracałaby w kółko
        # do końca okna i paliła procesor zamiast obserwować.
        widziane |= {s["id"] for s in wszystkie}
        cp.log(f"Sprint: partia {partie} po {int((time.monotonic() - runda) * 1000)} ms "
               f"— {len(nowe)} nowych pasujących do filtra")
        if not nowe or not wejscie.get("enabled"):
            continue
        # Limit obowiązuje CAŁE wywołanie, nie pojedynczą partię. Bez odejmowania
        # zdobyczy trzy partie przy max_per_run=2 dałyby sześć rezerwacji.
        reg_cfg["max_per_run"] = max(0, limit - len(zapisane))
        if reg_cfg["max_per_run"] == 0:
            # Nie ma po co dalej patrzeć: i tak nic więcej nie zapiszemy, a dobijanie
            # do końca okna kosztuje czas Lambdy. Dalszą obserwację przejmuje zryw
            # w domu, który i tak leci co 0,2 s.
            cp.log(f"Limit {limit} wykorzystany — wracam do domu")
            break
        # Wiek danych liczymy od chwili, w której TA partia przyszła.
        reg_cfg["seen_at"] = zobaczone
        ceny = {s["id"]: (doc.get("data", {}).get("attributes", {}) or {}).get("price")
                for s in nowe}
        # Ten sam rdzeń co strona lokalna: rejestrujemy, patrząc RÓWNOLEGLE na to,
        # co pojawia się w trakcie zapisu. Zapis stąd trwa ~100–200 ms, ale publikacja
        # sypie partiami co ~450 ms — więc i tutaj mieściła się cała partia.
        wyniki_partii, zapisane_partii, druga, swiezy = cp.rejestruj_obserwujac(
            lid, nowe, ceny, reg_cfg, set(zapisane), filtry, tz, set(widziane))
        wyniki.update(wyniki_partii)
        zapisane |= zapisane_partii
        if druga:
            widziane |= set(druga)
            partie += 1
            if swiezy is not None:
                doc = swiezy          # do domu jedzie najświeższy obraz grafiku
        if reg_cfg.get("auth_error"):
            cp.log(f"Przerywam partie: {reg_cfg['auth_error']}")
            break

    sprint_ms = int((time.monotonic() - szukanie) * 1000)
    if doc is None:
        return {"ok": True, "doc": None, "listing_id": None,
                "timings": {"sprint_ms": sprint_ms, "batches": 0,
                            "total_ms": int((time.monotonic() - started) * 1000)}}

    return {
        "ok": True,
        "listing_id": lid,
        "doc": doc,
        "results": {k: [bool(v[0]), v[1]] for k, v in wyniki.items()},
        "registered": sorted(zapisane),
        # `shots` doklada sie samo przez cale wywolanie — reg_cfg zyje ponad partiami.
        "shots": reg_cfg.get("shots") or [],
        "timings": {"sprint_ms": sprint_ms, "batches": partie,
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
