#!/usr/bin/env python3
"""Pomiar opóźnienia do API Decathlon GO — z domu i z AWS eu-west-1.

TEN SAM plik uruchamia się na dwa sposoby, żeby liczby dało się porównywać:

    python3 aws_probe/probe.py            # z serwera w domu
    (handler `lambda_handler`)            # jako funkcja Lambda w eu-west-1

Mierzy KAŻDĄ WARSTWĘ OSOBNO, bo tylko wtedy widać, ile z naszych ~80 ms pobrania
i ~70 ms strzału to sieć, którą da się usunąć przeprowadzką:

    DNS         — rozwiązanie nazwy (potrafi wejść w pomiar RTT niezauważone)
    TCP         — jedna runda do serwera, do KONKRETNEGO adresu IP
    TLS         — uzgodnienie szyfrowania na świeżym połączeniu
    HTTP zimny  — pierwsze pobranie na nowym połączeniu
    HTTP ciepły — kolejne pobrania po podtrzymanym połączeniu  <- TO decyduje o strzale

Powód powstania: `measure_rtt` w dodatku raportował 61 ms rundy do serwera, a w tej
samej sekundzie uwierzytelniony POST na PODTRZYMANYM połączeniu zajmował 48 ms.
Zapytanie na ciepłym gnieździe nie może być szybsze niż jedna runda — któraś z tych
liczb jest błędna. Zanim wydamy pieniądze na przeprowadzkę, trzeba wiedzieć która,
bo od tego zależy, ile ta przeprowadzka realnie kupuje.

Wysyła wyłącznie GET-y bez uwierzytelnienia — nie dotyka konta i niczego nie rezerwuje.
CELOWO nie przyjmuje tokenu: zapisany event testowy w konsoli AWS przechowywałby
żywą sesję Decathlona, a tego chcemy uniknąć.
"""

import http.client
import json
import socket
import ssl
import statistics
import time

HOST = "go.decathlon.pl"
# Ten sam ciężki payload (~21 KB na drucie), którego używa sprint — mierzymy to,
# co naprawdę robimy w sekundzie publikacji, a nie lekki ping.
PATH = "/api/listing/{id}?include=dates"
DOMYSLNY_LISTING = "1c0ec93e-ca77-44b9-a3a6-c72a99d050dd"
UA = "padel-watch-probe/1.0 (+https://go.decathlon.pl)"
PROBEK = 7
# Pamięć, przy której Lambda daje pełny rdzeń. Poniżej dostajesz jego ułamek.
LAMBDA_PELNY_RDZEN_MB = 1769


def _ms(od):
    return (time.monotonic() - od) * 1000


def zmierz_cpu():
    """Ile ms zajmuje ustalona porcja liczenia. NIE dotyka sieci.

    Bez tego nie da się odróżnić „serwer jest wolny" od „nasz procesor jest wolny",
    a w Lambdzie moc CPU przydziela się PROPORCJONALNIE DO PAMIĘCI. Przy 512 MB
    dostajemy ułamek rdzenia, więc deszyfrowanie TLS i czytanie 25 KB potrafi
    kosztować dziesiątki ms — i wygląda to w pomiarze jak wolny serwer.

    Skala dobrana tak, żeby na normalnym rdzeniu wyszło kilkadziesiąt ms — wtedy
    wygłodzony rdzeń Lambdy odstaje o rząd wielkości i widać to na pierwszy rzut oka.
    """
    import hashlib
    dane = b"padel" * 200
    start = time.monotonic()
    for _ in range(60000):
        dane = hashlib.sha256(dane).digest() + dane[:995]
    return _ms(start)


def zmierz_dns(host):
    """(ms, lista adresów IP). DNS osobno, bo socket.connect(nazwa) go w sobie ukrywa."""
    start = time.monotonic()
    info = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    czas = _ms(start)
    return czas, sorted({a[4][0] for a in info})


def zmierz_tcp(ip, probek=PROBEK):
    """Czyste rundy TCP do KONKRETNEGO adresu — bez DNS, bez TLS, bez API."""
    czasy = []
    for _ in range(probek):
        sock = socket.socket()
        sock.settimeout(5)
        start = time.monotonic()
        try:
            sock.connect((ip, 443))
            czasy.append(_ms(start))
        except OSError:
            pass
        finally:
            sock.close()
    return czasy


def zmierz_tls(ip, host):
    """Uzgodnienie TLS na świeżym połączeniu (bez czasu samego TCP)."""
    ctx = ssl.create_default_context()
    sock = socket.create_connection((ip, 443), timeout=10)
    try:
        start = time.monotonic()
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            czas = _ms(start)
            wersja = tls.version()
        return czas, wersja
    finally:
        try:
            sock.close()
        except OSError:
            pass


def zmierz_http(ip, host, path, probek=PROBEK):
    """(zimne, [ciepłe...], bajty). Zimne = nowe połączenie, ciepłe = podtrzymane.

    Ciepłe pobranie to najważniejsza liczba w całym pomiarze: dokładnie tyle kosztuje
    nas jedno zapytanie w sprincie i jeden strzał w salwie.
    """
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(ip, 443, timeout=15, context=ctx)
    # Łączymy się po IP (żeby mierzyć KONKRETNY adres), ale nazwa musi trafić do SNI
    # i do nagłówka Host — inaczej ELB nie wie, o którą usługę chodzi, a certyfikat
    # się nie zweryfikuje. http.client bierze oba z `conn.host`, więc podmieniamy je
    # PO utworzeniu połączenia; jawny nagłówek Host dokładałby drugi, sprzeczny.
    conn.host = host
    naglowki = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    czasy, bajty = [], 0
    try:
        for _ in range(probek):
            start = time.monotonic()
            conn.request("GET", path, headers=naglowki)
            resp = conn.getresponse()
            dane = resp.read()
            czasy.append(_ms(start))
            bajty = len(dane)   # NA DRUCIE (gzip) — celowo nie dekompresujemy
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
    finally:
        conn.close()
    return czasy[0], czasy[1:], bajty


def zmierz(listing_id=DOMYSLNY_LISTING):
    """Pełny pomiar. Zwraca słownik gotowy do porównania dom vs Irlandia."""
    path = PATH.format(id=listing_id)
    cpu_ms = zmierz_cpu()   # PRZED siecią, żeby nie mierzyć rozgrzanego już rdzenia
    dns_ms, adresy = zmierz_dns(HOST)
    if not adresy:
        return {"blad": f"brak adresów dla {HOST}"}

    # Mierzymy KAŻDY adres ELB osobno: DNS oddaje je na zmianę, więc jeśli różnią się
    # opóźnieniem, uśredniony wynik kłamie — i to mogło namieszać w naszych liczbach.
    per_ip = {}
    for ip in adresy:
        czasy = zmierz_tcp(ip)
        if czasy:
            per_ip[ip] = {
                "tcp_mediana_ms": round(statistics.median(czasy), 1),
                "tcp_min_ms": round(min(czasy), 1),
                "tcp_max_ms": round(max(czasy), 1),
            }

    najlepszy = min(per_ip, key=lambda i: per_ip[i]["tcp_mediana_ms"]) if per_ip else adresy[0]
    tls_ms, tls_wersja = zmierz_tls(najlepszy, HOST)
    zimny_ms, cieple, bajty = zmierz_http(najlepszy, HOST, path)

    return {
        "gdzie": gdzie_jestem(),
        "host": HOST,
        "adresy": adresy,
        "dns_ms": round(dns_ms, 1),
        "tcp_per_ip": per_ip,
        "mierzony_ip": najlepszy,
        "tls_ms": round(tls_ms, 1),
        "tls_wersja": tls_wersja,
        "http_zimny_ms": round(zimny_ms, 1),
        "http_cieply_mediana_ms": round(statistics.median(cieple), 1) if cieple else None,
        "http_cieply_min_ms": round(min(cieple), 1) if cieple else None,
        "http_cieply_max_ms": round(max(cieple), 1) if cieple else None,
        "http_cieple_kolejno": [round(c, 1) for c in cieple],
        "odpowiedz_bajtow_na_drucie": bajty,
        "cpu_ms": round(cpu_ms, 1),
        "pamiec_mb": pamiec_mb(),
    }


def pamiec_mb():
    """Przydział pamięci Lambdy (czyli w praktyce przydział CPU). None poza Lambdą."""
    import os
    wartosc = os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE")
    return int(wartosc) if wartosc else None


def gdzie_jestem():
    """Etykieta do porównania. W Lambdzie region bierzemy ze zmiennej środowiskowej."""
    import os
    region = os.environ.get("AWS_REGION")
    if region:
        return f"lambda:{region}"
    return "lokalnie"


def podsumuj(w):
    """Czytelny raport. To jest wyjście, które się porównuje między lokalizacjami."""
    if w.get("blad"):
        return f"BŁĄD: {w['blad']}"
    linie = [
        f"Miejsce pomiaru : {w['gdzie']}",
        f"Adresy ELB      : {', '.join(w['adresy'])}",
        f"DNS             : {w['dns_ms']} ms",
    ]
    for ip, d in sorted(w["tcp_per_ip"].items()):
        linie.append(f"  TCP {ip:<16}: {d['tcp_mediana_ms']} ms "
                     f"(min {d['tcp_min_ms']}, max {d['tcp_max_ms']})")
    linie += [
        f"TLS             : {w['tls_ms']} ms ({w['tls_wersja']})",
        f"HTTP zimny      : {w['http_zimny_ms']} ms   (nowe połączenie)",
        f"HTTP CIEPŁY     : {w['http_cieply_mediana_ms']} ms   "
        f"(min {w['http_cieply_min_ms']}, max {w['http_cieply_max_ms']})  "
        f"<- tyle kosztuje jedno zapytanie sprintu i jeden strzał salwy",
        f"  kolejno       : {w['http_cieple_kolejno']} ms",
        f"Odpowiedź       : {w['odpowiedz_bajtow_na_drucie']} B na drucie (gzip)",
        f"CPU (bez sieci) : {w['cpu_ms']} ms na ustaloną porcję liczenia"
        + (f", pamięć {w['pamiec_mb']} MB" if w['pamiec_mb'] else ""),
    ]
    # Ostrzegamy WYŁĄCZNIE na podstawie przydziału pamięci, nie czasu liczenia.
    # Pierwsza wersja porównywała cpu_ms z progiem skalibrowanym na laptopie
    # (Apple Silicon, 36 ms) i krzyczała także przy 1769 MB, gdzie rdzeń jest już
    # pełny, a x86 w chmurze i tak liczy ~220 ms. Kazała podnieść pamięć do wartości,
    # która była już ustawiona — bezużyteczna rada z fałszywego pomiaru.
    pamiec = w.get("pamiec_mb")
    if pamiec and pamiec < LAMBDA_PELNY_RDZEN_MB:
        linie.append("")
        linie.append(f"UWAGA: {pamiec} MB to UŁAMEK rdzenia — w Lambdzie moc CPU skaluje się "
                     f"z pamięcią. Podnieś do {LAMBDA_PELNY_RDZEN_MB} MB i powtórz, bo teraz "
                     f"mierzysz własne wygłodzenie, a nie serwer Decathlonu.")
    ciepl = w["http_cieply_mediana_ms"]
    tcp = min(d["tcp_mediana_ms"] for d in w["tcp_per_ip"].values()) if w["tcp_per_ip"] else None
    if ciepl and tcp:
        linie.append("")
        linie.append(f"Rezerwacja to DWIE rundy: podłoga sieciowa ~{2 * tcp:.0f} ms, "
                     f"a serwer dokłada ~{max(0, ciepl - tcp):.0f} ms na zapytanie.")
    return "\n".join(linie)


def lambda_handler(event, context):   # noqa: ARG001 - kontrakt AWS
    wynik = zmierz((event or {}).get("listing_id") or DOMYSLNY_LISTING)
    print(podsumuj(wynik))            # ląduje w CloudWatch Logs
    return {"statusCode": 200, "body": json.dumps(wynik, ensure_ascii=False)}


if __name__ == "__main__":
    import sys
    print(podsumuj(zmierz(sys.argv[1] if len(sys.argv) > 1 else DOMYSLNY_LISTING)))
