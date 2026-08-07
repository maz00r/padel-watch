#!/usr/bin/env python3
"""
Monitor wolnych terminów padla (Decathlon GO).

Sprawdza endpoint Decathlon GO dla podanego kortu, wyznacza wolne terminy,
filtruje je po oknach czasowych z config.json, porównuje ze stanem z poprzedniego
biegu (state.json) i wysyła push przez ntfy.sh tylko dla NOWYCH wolnych terminów.

Uruchomienie lokalnie:
    NTFY_TOPIC=twoj-temat python3 check_padel.py

Tylko biblioteka standardowa — brak zależności (działa w GitHub Actions bez pip).
"""

import gzip
import base64
import concurrent.futures
import http.client
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 (nie powinno wystąpić w CI)
    ZoneInfo = None

HERE = os.path.dirname(os.path.abspath(__file__))
# CONFIG_PATH / STATE_DIR można nadpisać zmienną środowiskową (przydatne w Dockerze).
CONFIG_PATH = os.environ.get("CONFIG_PATH") or os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(os.environ.get("STATE_DIR") or HERE, "state.json")
# Gdy monitor działa w tym samym kontenerze co przeglądarka (scalony dodatek), przeglądarka
# zapisuje świeży go-sdk-jwt do tego pliku, a monitor go stąd czyta — bez ręcznego wklejania.
TOKEN_FILE = os.environ.get("DECATHLON_TOKEN_FILE") or ""

LISTING_URL = "https://go.decathlon.pl/api/listing/{id}"  # lekki (~1 KB): kort + datesStats
LISTING_DATES_URL = LISTING_URL + "?include=dates"        # ciężki (~257 KB): + wszystkie terminy
LISTING_PAGE_URL = "https://go.decathlon.pl/l/{id}"       # strona kortu (podąża za 301 na nowe ID)
DECATHLON_API_URL = "https://go.decathlon.pl/api"
# Lekki, uwierzytelniony GET bez skutków ubocznych — służy do sprawdzenia, czy serwer
# akceptuje token (bez tokenu zwraca 403, ze złym 401, z dobrym 200).
DECATHLON_VERIFY_PATH = "/user-consent/my-consents"
UA = "padel-watch/1.0 (+https://go.decathlon.pl)"
UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # weekday(): Mon=0..Sun=6
PL_DAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
PL_DAYS_SHORT = ["pon", "wt", "śr", "czw", "pt", "sob", "niedz"]


def fmt_when(dt, short=False):
    days = PL_DAYS_SHORT if short else PL_DAYS
    return f"{days[dt.weekday()]} {dt:%d.%m %H:%M}"


_LOG_TZ = None


def _log_tz():
    """Strefa czasowa znaczników w logach (TIMEZONE / Europe/Warsaw; fallback UTC)."""
    global _LOG_TZ
    if _LOG_TZ is None:
        name = os.environ.get("TIMEZONE") or "Europe/Warsaw"
        try:
            _LOG_TZ = ZoneInfo(name) if ZoneInfo else timezone.utc
        except Exception:  # noqa: BLE001 - zła nazwa strefy nie może wywrócić logowania
            _LOG_TZ = timezone.utc
    return _LOG_TZ


# W zrywie sekundowa rozdzielczość znaczników przestaje wystarczać: nie da się z niej
# odczytać, czy termin przegraliśmy o 100 ms czy o 900 ms. Poza zrywem milisekundy
# tylko zaśmiecałyby Dziennik, więc włączamy je punktowo.
_LOG_MILLIS = False


def set_log_precision(millis):
    """Włącza/wyłącza milisekundy w znacznikach czasu (na czas zrywu)."""
    global _LOG_MILLIS
    _LOG_MILLIS = bool(millis)


def log(*args):
    now = datetime.now(_log_tz())
    ts = (now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if _LOG_MILLIS
          else now.strftime("%Y-%m-%d %H:%M:%S"))
    print(f"[{ts}]", *args, flush=True)


# --------------------------------------------------------------------------- IO

_config_warned = False


def load_config(quiet=False):
    """Konfiguracja z pliku; w dodatku HA pliku nie ma i wszystko idzie z ENV.

    O braku pliku mówimy RAZ na proces — inaczej ta linia powtarzałaby się przy
    każdej iteracji monitora i przy każdym zapytaniu panelu, zaśmiecając Dziennik.
    """
    global _config_warned
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        if not quiet and not _config_warned:
            log(f"(brak {CONFIG_PATH} — używam wartości z ENV)")
            _config_warned = True
        return {}


def load_state():
    data = load_state_doc()
    if data is None:
        return None
    return set(data.get("free_ids", []))


def load_state_doc():
    if not os.path.exists(STATE_PATH):
        return None  # None = pierwszy bieg (baseline)
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log("! state.json uszkodzony — traktuję jako pierwszy bieg")
        return None


def write_state_doc(doc):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")


def save_state(free_ids, registered_ids=None, decathlon_jwt=None, pending_ids=None,
               auth_alert_sent=None, decathlon_rt=None):
    old = load_state_doc() or {}
    if registered_ids is None:
        registered_ids = set(old.get("registered_ids", []))
    if decathlon_jwt is None:
        decathlon_jwt = old.get("decathlon_jwt")
    if decathlon_rt is None:
        decathlon_rt = old.get("decathlon_rt")
    if pending_ids is None:
        pending_ids = old.get("pending_ids", [])
    if auth_alert_sent is None:
        auth_alert_sent = old.get("auth_alert_sent", False)
    doc = {"free_ids": sorted(free_ids), "registered_ids": sorted(registered_ids)}
    if decathlon_jwt:
        doc["decathlon_jwt"] = clean_decathlon_token(decathlon_jwt)
    if decathlon_rt:
        doc["decathlon_rt"] = decathlon_rt
    if pending_ids:
        doc["pending_ids"] = sorted(pending_ids)  # terminy do ponowienia po naprawie tokenu
    if auth_alert_sent:
        doc["auth_alert_sent"] = True            # nie spamuj alertem o tokenie co iterację
    if old.get("clear_state_applied"):
        doc["clear_state_applied"] = old["clear_state_applied"]  # znacznik musi przetrwać zapis
    write_state_doc(doc)


def apply_clear_state():
    """Jednorazowo czyści zapisany stan wg opcji clear_state: 'registered' albo 'all'.

    Semantyka jednorazowa: w stanie zapamiętujemy zastosowaną wartość, więc kolejne
    restarty NIE czyszczą ponownie. Aby wyczyścić znowu, zmień wartość opcji
    (np. na "" i z powrotem) — dzięki temu włączona opcja nie kasuje stanu w kółko.
    """
    cfg = load_config()
    want = (os.environ.get("CLEAR_STATE") or cfg.get("clear_state") or "").strip().lower()
    if want not in ("registered", "all"):
        return
    doc = load_state_doc()
    if doc is None:
        return  # brak stanu — nie ma czego czyścić
    if doc.get("clear_state_applied") == want:
        return  # już zastosowane dla tej wartości
    n_reg = len(doc.get("registered_ids", []))
    n_free = len(doc.get("free_ids", []))
    new_doc = {
        "free_ids": [] if want == "all" else sorted(doc.get("free_ids", [])),
        "registered_ids": [],
        "clear_state_applied": want,
    }
    if want != "all" and doc.get("decathlon_jwt"):
        new_doc["decathlon_jwt"] = doc["decathlon_jwt"]  # 'all' czyści też zapisany token
    write_state_doc(new_doc)
    if want == "all":
        log(f"🧹 Wyczyszczono CAŁY stan (było: {n_reg} zapisanych terminów, {n_free} śledzonych, token skasowany).")
    else:
        log(f"🧹 Wyczyszczono listę zapisanych terminów ({n_reg} szt.). Śledzone terminy i token zostają.")


# ----------------------------------------------------------------------- helpers

def listing_id_from_url(url):
    """Wyciąga UUID kortu z linku /l/... (bierze ostatni UUID w URL-u)."""
    ids = re.findall(UUID_RE, url)
    if not ids:
        raise ValueError(f"Nie znalazłem ID kortu w URL: {url}")
    return ids[-1]


_ID_CACHE = {}          # seed_id -> (current_id, expires_at)
RESOLVE_TTL = 6 * 3600  # jak często ponownie sprawdzać przekierowanie (sekundy)


def resolve_current_id(seed_id):
    """Zwraca AKTUALNE id kortu, podążając za przekierowaniem strony /l/{id} (301).

    Decathlon czasem przenosi kort pod nowe ID — stary link robi wtedy 301 na nowy.
    Dzięki temu aplikacja sama nadąża za zmianą adresu, bez wpisywania go na sztywno.
    Wynik jest cache'owany na RESOLVE_TTL, by nie odpytywać strony w każdej iteracji.
    """
    now = time.time()
    hit = _ID_CACHE.get(seed_id)
    if hit and hit[1] > now:
        return hit[0]
    try:
        req = urllib.request.Request(LISTING_PAGE_URL.format(id=seed_id), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            found = re.findall(UUID_RE, resp.geturl())  # finalny URL po przekierowaniach
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        log(f"! Nie rozwiązałem aktualnego linku dla {seed_id} ({e!r}) — używam podanego")
        return seed_id  # nie cache'ujemy błędu — spróbujemy ponownie następnym razem
    current = found[-1] if found else seed_id
    if current != seed_id:
        log(f"↪ kort {seed_id} przekierowany na aktualne ID {current}")
    _ID_CACHE[seed_id] = (current, now + RESOLVE_TTL)
    return current


def parse_dt(s):
    """ISO datetime z API -> aware datetime (UTC)."""
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def hm_to_minutes(hm):
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------- transport HTTP

# Każde nowe połączenie to uzgodnienie TCP i TLS — zmierzone ~110 ms dla lekkiego
# pingu i ~200 ms dla pełnych danych. W wyścigu o termin to więcej niż sam transfer
# (pełne dane to raptem 21 KB po kompresji), dlatego połączenia podtrzymujemy.
_conn_local = threading.local()   # panel woła RPC z wielu wątków -> pula na wątek
MAX_REDIRECTS = 3


class _KeepAliveResponse:
    """Odpowiedź udająca tę z urlopen: .read() / .headers / .status i menedżer kontekstu."""

    def __init__(self, status, headers, body, url):
        self.status = status
        self.headers = headers
        self.url = url
        self._body = body

    def read(self):
        return self._body

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _conn_pool():
    pool = getattr(_conn_local, "pool", None)
    if pool is None:
        pool = _conn_local.pool = {}
    return pool


def drop_connection(host=None):
    """Zamyka podtrzymywane połączenie (lub wszystkie) — po błędzie albo na żądanie."""
    pool = _conn_pool()
    for key in ([host] if host else list(pool)):
        conn = pool.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - zamykanie nie może wywrócić biegu
                pass


def open_url(req, timeout=30):
    """urlopen po PODTRZYMYWANYM połączeniu HTTPS.

    Kontrakt zgodny z urlopen: zwraca obiekt z .read()/.headers/.status, a przy
    HTTP >= 400 podnosi urllib.error.HTTPError — dzięki temu obsługa błędów
    w wywołaniach zostaje bez zmian.

    Bezczynne gniazdo bywa zamykane przez serwer, więc zerwane połączenie
    ponawiamy raz na świeżym; dopiero druga wpadka to błąd sieci.
    """
    url = req.full_url
    for _ in range(MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        headers = dict(req.headers)
        status = hdrs = body = None
        for attempt in range(2):
            pool = _conn_pool()
            conn = pool.get(host)
            if conn is None:
                if parts.scheme != "https":
                    raise urllib.error.URLError(f"obsługuję tylko https, nie {parts.scheme!r}")
                conn = pool[host] = http.client.HTTPSConnection(host, timeout=timeout)
            try:
                conn.request(req.get_method(), path, body=req.data, headers=headers)
                resp = conn.getresponse()
                body, status, hdrs = resp.read(), resp.status, resp.headers
                break
            except (http.client.HTTPException, OSError) as e:
                drop_connection(host)
                if attempt == 0:
                    continue   # serwer zamknął bezczynne gniazdo — próbujemy od nowa
                raise urllib.error.URLError(e)
        if hdrs.get("Connection", "").lower() == "close":
            drop_connection(host)
        if status in (301, 302, 303, 307, 308) and hdrs.get("Location"):
            url = urllib.parse.urljoin(url, hdrs["Location"])
            continue
        if status >= 400:
            raise urllib.error.HTTPError(url, status, str(status), hdrs, io.BytesIO(body))
        return _KeepAliveResponse(status, hdrs, body, url)
    raise urllib.error.URLError(f"za dużo przekierowań dla {req.full_url}")


def http_get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Accept": "application/json"}
    )
    with open_url(req, timeout=60) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def fetch_listing_light(listing_id):
    """Lekki ping (~1 KB): kort + datesStats.availableListingDates."""
    return http_get_json(LISTING_URL.format(id=listing_id))


def fetch_listing(listing_id):
    """Ciężki payload (~257 KB): kort + wszystkie terminy w included[]."""
    return http_get_json(LISTING_DATES_URL.format(id=listing_id))


# ------------------------------------------------------------------ core logic

def free_slots(doc, listing_id, now_utc):
    """Zwraca listę słowników opisujących wolne terminy (przyszłe, niezarezerwowane)."""
    out = []
    for item in doc.get("included", []):
        if item.get("type") != "listing-date":
            continue
        a = item.get("attributes", {})
        if a.get("cancelled"):
            continue
        limit = a.get("participantsLimit")
        if limit is None:  # bez limitu miejsc — pomijamy (nie da się ocenić)
            continue
        count = a.get("participantsCount") or 0
        if count >= limit:
            continue
        start = parse_dt(a.get("date"))
        if start is None or start <= now_utc:
            continue
        reg_end = parse_dt(a.get("registrationEndDate"))
        if reg_end is not None and reg_end <= now_utc:
            continue
        out.append(
            {
                "id": f"{listing_id}:{item.get('id')}",
                "date_id": item.get("id"),
                "listing_id": listing_id,
                "start_utc": start,
                "name": a.get("name") or "Termin",
                "price": a.get("price"),
                "count": count,
                "limit": limit,
            }
        )
    return out


def passes_filter(slot, filters, tz):
    """True, jeśli lokalny czas startu mieści się w którymkolwiek z okien.

    Obsługuje okna przez północ: gdy start > end (np. 15:00->02:00), okno trwa od
    `start` danego dnia do `end` następnego dnia. Część porannego ogona (przed `end`)
    przypisana jest do dnia POPRZEDNIEGO (czyli dnia rozpoczęcia okna).
    """
    if not filters:
        return True
    local = slot["start_utc"].astimezone(tz)
    day = DAY_NAMES[local.weekday()]
    prev_day = DAY_NAMES[(local.weekday() - 1) % 7]
    minutes = local.hour * 60 + local.minute
    for win in filters:
        days = [d.lower() for d in win.get("days", DAY_NAMES)]
        start = hm_to_minutes(win.get("start", "00:00"))
        end = hm_to_minutes(win.get("end", "24:00"))
        if start < end:
            # zwykłe okno w obrębie jednej doby
            if day in days and start <= minutes < end:
                return True
        else:
            # okno przez północ (start > end): wieczór dnia + ranek następnego
            if day in days and minutes >= start:
                return True
            if prev_day in days and minutes < end:
                return True
    return False


def parse_days(token):
    """'mon-fri' / 'sat,sun' / 'mon,wed,fri' -> lista nazw dni."""
    out = []
    for part in token.strip().lower().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = (x.strip() for x in part.split("-", 1))
            ai, bi = DAY_NAMES.index(a), DAY_NAMES.index(b)
            out += DAY_NAMES[ai:bi + 1] if ai <= bi else DAY_NAMES[ai:] + DAY_NAMES[:bi + 1]
        else:
            if part not in DAY_NAMES:
                raise ValueError(f"nieznany dzień: {part}")
            out.append(part)
    return out


def parse_filters_env(spec):
    """Parsuje zmienną FILTERS, np. 'mon-fri:15:00-02:00; sat-sun:00:00-24:00'.

    Format: okna oddzielone ';'; każde okno to 'DNI:HH:MM-HH:MM'.
    DNI: zakres ('mon-fri') lub lista ('sat,sun'). Czasy w strefie z config.timezone.
    """
    filters = []
    for chunk in spec.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        days_part, time_part = chunk.split(":", 1)  # pierwszy ':' dzieli dni od czasu
        start, end = (x.strip() for x in time_part.split("-", 1))
        filters.append({"days": parse_days(days_part), "start": start, "end": end})
    return filters


def parse_intervals_env(spec):
    """Parsuje zmienną INTERVALS: okna z własną częstotliwością odświeżania.

    Format jak FILTERS, z doklejonym '=SEKUNDY', np.:
        'mon-fri:15:00-02:00=30; sat-sun:08:00-22:00=60'
    Poza dopasowanymi oknami obowiązuje bazowy CHECK_INTERVAL.
    Okna przez północ działają tak samo jak w FILTERS.
    """
    out = []
    for chunk in spec.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        win, secs = chunk.rsplit("=", 1)
        days_part, time_part = win.split(":", 1)
        start, end = (x.strip() for x in time_part.split("-", 1))
        requested = int(secs)
        seconds = max(MIN_INTERVAL_SECONDS, requested)
        if seconds != requested:
            log(f"! INTERVALS '{win.strip()}': żądano {requested}s — używam {seconds}s "
                f"(minimum, poniżej ryzykujesz blokadę po IP).")
        elif seconds < AGGRESSIVE_INTERVAL_SECONDS:
            log(f"⚠ INTERVALS '{win.strip()}': {seconds}s to bardzo agresywne tempo "
                f"(~{3600 // seconds} zapytań/h) — używaj tylko w wąskich oknach.")
        out.append({"days": parse_days(days_part), "start": start, "end": end, "seconds": seconds})
    return out


def current_interval(default_s, windows, tz, now_utc=None):
    """Interwał obowiązujący TERAZ: sekundy z pierwszego pasującego okna, inaczej default."""
    if not windows:
        return default_s
    now_local = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
    for w in windows:
        if passes_filter({"start_utc": now_local}, [w], tz):
            return w["seconds"]
    return default_s


# --------------------------------------------------------------------- zryw

# Grafik wychodzi o stałej porze (u nas zmierzone ~11:00:53, powtarzalnie co do
# sekundy). Zamiast młócić szybkim taktem przez kwadrans, robimy krótki zryw
# wycelowany w tę sekundę: gęściej, ale przez kilkanaście sekund — co daje MNIEJ
# zapytań łącznie niż stały szybki takt, a wykrycie schodzi do ułamka sekundy.
BURST_MIN_INTERVAL = 0.2    # podłoga taktu w zrywie (poza nim obowiązuje MIN_INTERVAL_SECONDS)
BURST_MAX_SECONDS = 120     # zryw ma być krótki — dłuższy to już zwykłe okno INTERVALS
MIN_SLEEP_SECONDS = 0.05    # zabezpieczenie przed pętlą bez oddechu


def parse_burst_env(spec):
    """'mon-sun:11:00:45' -> {'days': [...], 'at': (godz, min, sek)}. Puste -> None."""
    spec = (spec or "").strip()
    if not spec:
        return None
    days_part, _, time_part = spec.partition(":")
    if not time_part.strip():
        raise ValueError("oczekuję DNI:GG:MM:SS, np. 'mon-sun:11:00:45'")
    bits = [b.strip() for b in time_part.split(":")]
    if len(bits) == 2:
        bits.append("0")          # 'mon-sun:11:00' = równo o pełnej minucie
    if len(bits) != 3:
        raise ValueError(f"zła godzina '{time_part}' — oczekuję GG:MM albo GG:MM:SS")
    try:
        hour, minute, second = (int(b) for b in bits)
    except ValueError:
        raise ValueError(f"zła godzina '{time_part}' — same liczby, np. 11:00:45") from None
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise ValueError(f"godzina '{time_part}' poza zakresem")
    return {"days": parse_days(days_part), "at": (hour, minute, second)}


def burst_bounds(burst, tz, now=None):
    """(początek, koniec) najbliższego zrywu — trwającego albo przyszłego. None, gdy brak."""
    if not burst:
        return None
    now = now or datetime.now(tz)
    hour, minute, second = burst["at"]
    base = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    length = timedelta(seconds=burst["seconds"])
    for delta in range(8):        # dziś, a jeśli dzisiejszy minął — kolejny pasujący dzień
        start = base + timedelta(days=delta)
        if now >= start + length:
            continue
        if DAY_NAMES[start.weekday()] in burst["days"]:
            return start, start + length
    return None


def plan_sleep(default_s, windows, burst, tz, elapsed=0.0, now=None):
    """Ile spać przed kolejnym sprawdzeniem.

    Trzy zasady:
      1. w trwającym zrywie obowiązuje jego własny, gęsty takt,
      2. tuż przed zrywem śpimy DOKŁADNIE do jego początku, żeby go nie przespać,
      3. od reszty odejmujemy czas pracy — inaczej ustawione 2 s dają realnie ~2,4 s,
         bo do każdego cyklu doklejał się czas zapytań.
    """
    now = now or datetime.now(tz)
    elapsed = max(0.0, elapsed)
    bounds = burst_bounds(burst, tz, now)
    if bounds and bounds[0] <= now < bounds[1]:
        return max(MIN_SLEEP_SECONDS, burst["interval"] - elapsed)
    target = current_interval(default_s, windows, tz, now.astimezone(timezone.utc)) - elapsed
    if bounds and now < bounds[0]:
        # Do startu zrywu liczymy od TERAZ — czas pracy już upłynął, więc drugi raz
        # go nie odejmujemy; inaczej budzilibyśmy się ułamek sekundy za wcześnie
        # i pierwsze sprawdzenie zrywu wypadałoby obok celu.
        target = min(target, (bounds[0] - now).total_seconds())
    return max(MIN_SLEEP_SECONDS, target)


def fmt_price(price, listing_default):
    p = price or listing_default
    if not p or p.get("amount") in (None, 0):
        return "za darmo"
    return f"{p['amount'] / 100:.2f} {p.get('currency', '')}".strip()


def boolish(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on", "tak")


def price_amount(slot, listing_default):
    p = slot.get("price") or listing_default or {}
    return p.get("amount") or 0


# ------------------------------------------------------------------ register

def load_registered_ids():
    data = load_state_doc() or {}
    return set(data.get("registered_ids", []))


def clean_decathlon_token(value):
    token = (value or "").strip().strip("\"'")
    for prefix in ("JWT:", "jwt:", "Bearer ", "bearer "):
        if token.startswith(prefix):
            token = token[len(prefix):].strip().strip("\"'")
    return token


def jwt_expiry(token):
    token = clean_decathlon_token(token)
    parts = token.split(".")
    if len(parts) != 3:
        return 0
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:  # noqa: BLE001 - diagnostyka tokena nie może zatrzymać monitoringu
        return 0
    try:
        return int(data.get("exp") or 0)
    except (TypeError, ValueError):
        return 0


def token_from_file():
    """Czyta go-sdk-jwt zapisany przez przeglądarkę (scalony dodatek). Zwraca '' gdy brak.

    Format pliku: {"jwt": "...", "exp": 1721570000}. Plik może chwilowo nie istnieć
    (przeglądarka jeszcze nie zalogowana) — wtedy po prostu spadamy na token z configu.
    """
    path = TOKEN_FILE
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return ""
    return clean_decathlon_token(doc.get("jwt"))


def newer_decathlon_token(config_token, state_token):
    config_token = clean_decathlon_token(config_token)
    state_token = clean_decathlon_token(state_token)
    if not config_token:
        return state_token
    if not state_token:
        return config_token
    return state_token if jwt_expiry(state_token) > jwt_expiry(config_token) else config_token


def refresh_decathlon_token(token, cookie=None, refresh_token=None):
    """Pozyskuje świeży JWT z /api/auth/refresh — dokładnie jak aplikacja Decathlon GO.

    Odwzorowuje jej wywołanie:
        headers: Authorization: Bearer <obecny go-sdk-jwt>
        data:    { unsafeRefreshToken: <go-unsafe-rt> }   # tylko gdy istnieje
        withCredentials: true                              # cookies, jeśli są
    W GO poświadczeniem jest zwykle SAM JWT (localStorage 'go-sdk-jwt') — nie ma
    ciasteczka sesji ani 'go-unsafe-rt'. Cookie i refresh_token są opcjonalne.
    Zwraca (jwt, rt) — serwer bywa, że rotuje refresh token.
    """
    token = clean_decathlon_token(token)
    cookie = (cookie or "").strip()
    refresh_token = (refresh_token or "").strip()
    if not (token or cookie or refresh_token):
        raise ValueError("brak poświadczeń Decathlon GO (token/cookie/refresh token)")
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    body = {"unsafeRefreshToken": refresh_token} if refresh_token else {}
    req = urllib.request.Request(
        f"{DECATHLON_API_URL}/auth/refresh",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    doc = json.loads(raw.decode("utf-8")) if raw else {}
    refreshed = clean_decathlon_token(doc.get("jwt"))
    if not refreshed:
        raise ValueError("Decathlon GO nie zwrócił nowego JWT")
    return refreshed, (doc.get("rt") or "").strip()


def ensure_decathlon_token(cfg):
    """Zwraca (token, błąd). Utrzymuje JWT przy życiu, odświeżając go PROAKTYWNIE.

    W Decathlon GO poświadczeniem jest sam JWT (localStorage `go-sdk-jwt`) — wklejasz
    go RAZ, a dodatek odnawia go zanim wygaśnie, więc łańcuch trwa dopóki działa:
    - token ważny            -> używamy bez ruchu sieciowego,
    - token wygasa/wygasł    -> refresh (JWT + opcjonalnie cookie/refresh token),
    - nie umiemy odczytać exp -> próbujemy jak jest (401 obsłuży fallback).
    """
    token = clean_decathlon_token(cfg.get("token"))
    cookie = (cfg.get("refresh_cookie") or "").strip()
    rt = (cfg.get("refresh_token") or "").strip()
    exp = jwt_expiry(token) if token else 0
    expired = bool(token) and exp > 0 and exp <= time.time() + TOKEN_EXPIRY_MARGIN
    if token and not expired:
        return token, None
    if cfg.get("browser_mode"):
        # Token pochodzi z zalogowanej przeglądarki (plik /data/token.json). To ONA odnawia
        # sesję — serwerowy /auth/refresh i tak zwraca 401, więc nie zawracamy nim głowy.
        # UWAGA na margines: TOKEN_EXPIRY_MARGIN istnieje dla proaktywnego refreshu, którego
        # tu NIE ma. Strona odnawia token dopiero PO wygaśnięciu, więc tokeny spędzają
        # ostatnie minuty życia „w marginesie" — a wciąż działają. Wygasły = faktycznie
        # wygasły (exp w przeszłości), nie „wygasa za chwilę".
        if token and (exp <= 0 or exp > time.time()):
            return token, None
        if not token:
            return None, "brak tokenu — zaloguj się w panelu Padel"
        # DOŁEK ODNOWY: strona odnawia token dopiero po wygaśnięciu, a czytnik budzi się
        # chwilę później — świeżo wygasły token to NORMALNY stan przejściowy (kilkanaście
        # sekund co ~15 min). Alarmujemy dopiero, gdy leży martwy dłużej niż karencja —
        # inaczej każdy cykl odnowy wysyłałby fałszywy push „token wygasł".
        if time.time() - exp <= BROWSER_RENEW_GRACE:
            return token, None
        return token, "token wygasł — zaloguj się w panelu Padel"
    if not (token or cookie or rt):
        return None, "brak tokenu Decathlon GO (wklej go-sdk-jwt w decathlon_token)"
    try:
        fresh, new_rt = refresh_decathlon_token(token, cookie, rt)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        if token and not expired:
            return token, None
        return (token or None), f"nie udało się odświeżyć tokenu: {e!r}"
    cfg["token"] = fresh
    cfg["token_refreshed"] = True
    if new_rt:
        cfg["refresh_token"] = new_rt  # serwer rotuje refresh token — zapamiętaj nowy
    log("~ Token Decathlon GO wygasał — odświeżony." if token
        else "~ Pobrano świeży token Decathlon GO.")
    return fresh, None


def verify_decathlon_token(token):
    """Pyta SERWER, czy token faktycznie działa (GET, bez skutków ubocznych).

    Samo sprawdzenie `exp` z JWT jest lokalne i nic nie dowodzi — token może być
    poprawnie zbudowany i niewygasły, a serwer i tak go odrzuci. Tu robimy prawdziwe
    uwierzytelnione zapytanie: 200 = działa, 401/403 = nie.
    Zwraca (ok, szczegóły), gdzie ok: True/False/None (None = nie dało się ustalić).
    """
    token = clean_decathlon_token(token)
    if not token:
        return False, "brak tokenu"
    req = urllib.request.Request(
        f"{DECATHLON_API_URL}{DECATHLON_VERIFY_PATH}",
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return (200 <= resp.status < 300), f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        try:
            e.close()
        except Exception:  # noqa: BLE001
            pass
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        return None, f"sieć niedostępna: {e!r}"


def check_decathlon_credentials(cfg, topic=None, book_url=None):
    """Test poświadczeń — NIE wymaga wolnego terminu i nic nie rezerwuje.

    Uruchamiany przy starcie (gdy auto_register jest włączone) albo opcją `test_token`.
    Weryfikuje token PRAWDZIWYM zapytaniem do API, a nie tylko lokalnym odczytem `exp`.
    """
    cfg["auth_checked"] = True
    if not (cfg.get("token") or cfg.get("refresh_cookie") or cfg.get("refresh_token")):
        msg = ("brak tokenu — zaloguj się w panelu Padel"
               if cfg.get("browser_mode")
               else "brak tokenu Decathlon GO (wklej go-sdk-jwt w decathlon_token)")
        log(f"✗ Test poświadczeń: {msg} — auto-rezerwacja nie zadziała.")
        cfg["auth_error"] = msg
        return False
    token, err = ensure_decathlon_token(cfg)
    if err:
        log(f"✗ Test poświadczeń: {err}")
        cfg["auth_error"] = err
        if topic:
            notify_auth_problem(topic, err, book_url)
        return False

    ok, detail = verify_decathlon_token(token)
    left_txt = ""
    exp = jwt_expiry(token)
    if exp:
        left = max(0, int(exp - time.time()))
        when = datetime.fromtimestamp(exp, _log_tz()).strftime("%Y-%m-%d %H:%M:%S")
        left_txt = f" Ważny do {when} (jeszcze ~{left // 60} min)."

    if ok is False:
        hint = ("zaloguj się w panelu Padel" if cfg.get("browser_mode")
                else "wklej świeży go-sdk-jwt")
        msg = f"serwer ODRZUCIŁ token ({detail}) — {hint}"
        log(f"✗ Test poświadczeń: {msg}.{left_txt}")
        cfg["auth_error"] = msg
        if topic:
            notify_auth_problem(topic, msg, book_url)
        return False
    if ok is None:
        # Nie wiadomo — nie traktujemy jak błędu auth, żeby awaria sieci nie wywołała alertu.
        log(f"? Test poświadczeń: nie zweryfikowałem tokenu ({detail}).{left_txt}")
        cfg["auth_error"] = None
        return True
    log(f"✓ Test poświadczeń: token DZIAŁA — serwer potwierdził ({detail}).{left_txt}")
    cfg["auth_error"] = None
    return True


# Ile czekać na token odnowiony przez przeglądarkę po HTTP 401. Czytnik budzi się ~10 s
# po wygaśnięciu i potrzebuje jeszcze chwili na nawigację i zapis, więc ~24 s pokrywa
# cały cykl budzik -> nawigacja -> zapis pliku.
TOKEN_WAIT_ATTEMPTS = 8
TOKEN_WAIT_DELAY = 3


def wait_for_fresher_token(token, attempts=TOKEN_WAIT_ATTEMPTS, delay=TOKEN_WAIT_DELAY):
    """Czeka, aż przeglądarka zapisze token INNY niż podany. Zwraca '' gdy się nie doczekał."""
    for _ in range(attempts):
        got = token_from_file()
        if got and got != token:
            return got
        time.sleep(delay)
    return ""


def decathlon_rpc(method, token, payload, extend=None):
    """Wywołuje endpoint RPC Decathlon GO: POST /api/v2/{method} z tokenem sesji."""
    req = urllib.request.Request(
        f"{DECATHLON_API_URL}/v2/{method}",
        data=json.dumps({"input": payload, "extend": extend or {}}).encode("utf-8"),
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    # Podtrzymane połączenie: to jest TO zapytanie, które wygrywa albo przegrywa termin.
    with open_url(req, timeout=30) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    doc = json.loads(raw.decode("utf-8")) if raw else {}
    return doc.get("output", doc)


def register_slot(slot, listing_price, cfg, speculative=False):
    """Zapisuje uczestnika na termin przez Decathlon GO (POST /api/v2/transactions.create).

    speculative=True -> tylko niezobowiązująca wycena/walidacja (nie rezerwuje).
    Domyślnie tylko darmowe terminy (płatne wymagają osobnego kroku płatności).
    Payload i endpoint odtworzone z aplikacji Decathlon GO.
    """
    token, token_error = ensure_decathlon_token(cfg)
    name = (cfg.get("name") or "").strip()
    if token_error:
        return False, token_error
    if not name:
        return False, "brak imienia i nazwiska uczestnika (auto_register_name)"
    if price_amount(slot, listing_price) > 0 and cfg.get("free_only", True):
        return False, f"termin płatny ({fmt_price(slot.get('price'), listing_price)}) — pomijam"
    participant = {
        "name": name,
        "age": cfg.get("age") or None,
        "priceId": None,        # darmowy kort nie ma poziomu cenowego
        "subPriceId": None,
        "isReduced": False,
        "customFormAnswers": [],
        "startList": None,
    }
    payload = {
        "speculative": bool(speculative),
        "listingDateId": slot["date_id"],
        "customer": None,
        "homeDeliveryAddress": None,
        "participants": [participant],
        "invoiceData": None,
        "seatsIOHoldToken": None,
    }
    doc = None
    for attempt in range(2):
        try:
            doc = decathlon_rpc("transactions.create", token, payload)
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:240]
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    e.close()
                except Exception:  # noqa: BLE001
                    pass
            if e.code == 401 and attempt == 0:
                if cfg.get("browser_mode"):
                    # Serwerowy refresh w GO nie działa (zawsze 401). Za to przeglądarka
                    # odnawia token tuż po jego wygaśnięciu (dołek odnowy trwa kilkanaście
                    # sekund) — dlatego czekamy chwilę na świeży token w pliku, zamiast
                    # oddawać gorący termin walkowerem. Czekanie jest ograniczone, żeby
                    # przy faktycznie martwej sesji nie wisieć w nieskończoność.
                    fresh = wait_for_fresher_token(token)
                    if fresh:
                        token = fresh
                        cfg["token"] = fresh
                        log("~ Świeższy token z przeglądarki po HTTP 401; ponawiam rejestrację")
                        continue
                    return False, "token odrzucony (HTTP 401) — zaloguj się w panelu Padel"
                try:
                    token, new_rt = refresh_decathlon_token(
                        token, cfg.get("refresh_cookie"), cfg.get("refresh_token")
                    )
                    cfg["token"] = token
                    cfg["token_refreshed"] = True
                    if new_rt:
                        cfg["refresh_token"] = new_rt
                    log("~ Token Decathlon GO odświeżony po HTTP 401; ponawiam rejestrację")
                    continue
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as refresh_error:
                    return False, f"token odrzucony (HTTP 401), refresh nieudany: {refresh_error!r}"
            if e.code in (401, 403):
                hint = ("zaloguj się w panelu Padel" if cfg.get("browser_mode")
                        else "wklej świeży go-sdk-jwt")
                return False, f"token odrzucony (HTTP {e.code}) — {hint}"
            return False, f"Decathlon HTTP {e.code}: {detail or e.reason}"
        except (urllib.error.URLError, TimeoutError) as e:
            return False, f"Decathlon niedostępny: {e!r}"
    state = doc.get("processState") or doc.get("state") or (doc.get("data") or {}).get("processState")
    # ID transakcji oddajemy przez cfg (nie przez wynik), żeby nie zmieniać kontraktu
    # funkcji. Salwa potrzebuje go, by anulować nadmiarowe rezerwacje ponad limit.
    cfg["transaction_id"] = _ident(_flat(doc).get("id"))
    if speculative:
        return True, f"walidacja OK (speculative{', ' + state if state else ''})"
    if state == "pending-payment":
        return False, "utworzono rezerwację, ale wymaga płatności"
    return True, state or "zarejestrowano"


# ------------------------------------------------------- moje rezerwacje (panel)

# Gdy API nie poda długości terminu — kort padlowy w GO to „Rezerwacja godzinna".
DEFAULT_SLOT_MINUTES = 60
# Ile rezerwacji naraz i ile stron maksymalnie (zabezpieczenie przed pętlą bez końca).
RESERVATIONS_PAGE_SIZE = 100
RESERVATIONS_MAX_PAGES = 10
# Rozszerzenia odpowiedzi: bez nich transakcja to same identyfikatory, a panel
# potrzebuje terminu (data, długość), kortu (nazwa, adres) i listy uczestników.
RESERVATIONS_EXTEND = {"items": {"listingDate": {}, "listing": {}, "participants": {}}}
# Stany, w których rezerwacja już nie obowiązuje (nie ma czego anulować ani wpisywać
# do kalendarza). Nazwy odtworzone z aplikacji GO; nieznane stany traktujemy jak aktywne.
CANCELLED_STATES = ("cancelled", "canceled", "declined", "expired", "payment-expired")


def _ident(value):
    """ID z API bywa stringiem albo {'uuid': …} (starsze endpointy). Zwraca string."""
    if isinstance(value, dict):
        return str(value.get("uuid") or value.get("id") or "")
    return str(value or "")


def _flat(obj):
    """Ujednolica obiekt API: {'id':…, 'attributes':{…}} czytamy tak samo jak płaski {…}."""
    if not isinstance(obj, dict):
        return {}
    attrs = obj.get("attributes")
    if isinstance(attrs, dict):
        merged = dict(attrs)
        merged.setdefault("id", _ident(obj.get("id")))
        return merged
    return obj


def _rpc_error(e, what):
    """Zamienia wyjątek z decathlon_rpc na komunikat dla użytkownika panelu."""
    if isinstance(e, urllib.error.HTTPError):
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                e.close()
            except Exception:  # noqa: BLE001
                pass
        if e.code in (401, 403):
            return f"{what}: sesja odrzucona (HTTP {e.code}) — zaloguj się w zakładce Przeglądarka"
        return f"{what}: Decathlon HTTP {e.code}{': ' + detail if detail else ''}"
    return f"{what}: Decathlon niedostępny ({e!r})"


def credentials_cfg():
    """Poświadczenia dla procesów spoza pętli monitora (panel rezerwacji).

    Ten sam wybór źródeł tokenu co w run_once: wygrywa token o najdalszym exp,
    a w scalonym dodatku odnawia go zalogowana przeglądarka (browser_mode).
    """
    cfg = load_config(quiet=True)  # panel woła to przy każdym zapytaniu — bez gadania do logu
    state_doc = load_state_doc()
    return {
        "token": newer_decathlon_token(
            newer_decathlon_token(
                token_from_file(),
                os.environ.get("DECATHLON_TOKEN") or cfg.get("decathlon_token") or "",
            ),
            (state_doc or {}).get("decathlon_jwt") or "",
        ),
        "refresh_cookie": os.environ.get("DECATHLON_COOKIE") or cfg.get("decathlon_cookie") or "",
        "refresh_token": (state_doc or {}).get("decathlon_rt") or "",
        "browser_mode": bool(TOKEN_FILE),
    }


def panel_rpc(method, cfg, token, payload, extend=None):
    """RPC z jedną próbą ratunku po HTTP 401. Zwraca (odpowiedź, aktualny token).

    Strona odnawia JWT dopiero PO wygaśnięciu, więc trafienie w ten kilkunastosekundowy
    dołek jest normalnym stanem, nie awarią sesji (patrz BROWSER_RENEW_GRACE). Bez tego
    kliknięcie w panelu akurat w dołku kończyło się czerwonym „sesja odrzucona", mimo
    że sesja żyła — dokładnie jak przy rejestracji, która ma tę osłonę od 0.3.2.
    """
    try:
        return decathlon_rpc(method, token, payload, extend), token
    except urllib.error.HTTPError as first:
        if first.code != 401 or not cfg.get("browser_mode"):
            raise
        fresh = wait_for_fresher_token(token)
        if not fresh:
            raise
        log(f"~ Panel: HTTP 401 w dołku odnowy — ponawiam {method} ze świeższym tokenem")
        cfg["token"] = fresh
        return decathlon_rpc(method, fresh, payload, extend), fresh


def decathlon_my_user_id(cfg, token):
    """(ID zalogowanego konta, aktualny token) — filtr listy transakcji."""
    doc, token = panel_rpc("users.getMe", cfg, token, {})
    flat = _flat(doc or {})
    return _ident(flat.get("id") or _flat(flat.get("user")).get("id")), token


def fetch_my_reservations(cfg):
    """Zwraca (surowe transakcje konta, błąd). Nie modyfikuje niczego po stronie GO."""
    token, token_error = ensure_decathlon_token(cfg)
    if token_error:
        return None, token_error
    try:
        user_id, token = decathlon_my_user_id(cfg, token)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as e:
        return None, _rpc_error(e, "odczyt konta")
    if not user_id:
        return None, "nie udało się ustalić konta (users.getMe nie zwrócił id)"

    items, seen, page = [], set(), 0
    while page < RESERVATIONS_MAX_PAGES:
        payload = {"customerId": user_id, "limit": RESERVATIONS_PAGE_SIZE, "page": page}
        try:
            doc, token = panel_rpc("transactions.list", cfg, token, payload, RESERVATIONS_EXTEND)
            doc = doc or {}
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as e:
            return None, _rpc_error(e, "lista rezerwacji")
        batch = doc.get("items") or []
        for raw in batch:
            # Numeracja stron w GO bywa 0- i 1-based, więc pierwsza strona potrafi
            # przyjść dwa razy — odsiewamy po ID zamiast ufać numerom.
            key = _ident(_flat(raw).get("id"))
            if key and key in seen:
                continue
            seen.add(key)
            items.append(raw)
        page += 1
        if not batch or len(items) >= int(doc.get("totalCount") or 0):
            break
    return items, None


def normalize_reservation(raw, now_utc=None):
    """Spłaszcza transakcję z API do pól, których używa panel i kalendarz."""
    tx = _flat(raw)
    date = _flat(tx.get("listingDate"))
    listing = _flat(tx.get("listing"))
    try:
        start = parse_dt(date.get("date"))
    except (ValueError, TypeError):
        start = None
    try:
        minutes = int(date.get("duration") or DEFAULT_SLOT_MINUTES)
    except (ValueError, TypeError):
        minutes = DEFAULT_SLOT_MINUTES
    state = str(tx.get("processState") or tx.get("state") or "")
    participants = [
        n for n in (_flat(p).get("name") for p in (tx.get("participants") or [])) if n
    ]
    now = now_utc or datetime.now(timezone.utc)
    return {
        "id": _ident(tx.get("id")),
        "state": state,
        "cancelled": state.lower() in CANCELLED_STATES or bool(date.get("cancelled")),
        "start_utc": start,
        "minutes": minutes if minutes > 0 else DEFAULT_SLOT_MINUTES,
        "past": bool(start and start < now),
        "title": listing.get("title") or listing.get("name") or date.get("name") or "Rezerwacja",
        "slot_name": date.get("name") or "",
        "address": (_flat(listing.get("location")).get("address") or ""),
        "listing_id": _ident(listing.get("id")),
        "participants": participants,
    }


def reservations_view(cfg, tz, now_utc=None):
    """(lista rezerwacji dla panelu, błąd) — posortowana, najbliższa pierwsza."""
    raw_items, error = fetch_my_reservations(cfg)
    if error:
        return None, error
    now = now_utc or datetime.now(timezone.utc)
    out = []
    for raw in raw_items:
        res = normalize_reservation(raw, now)
        if not res["start_utc"]:
            continue  # transakcja bez terminu (np. sam bilet) — nie ma czego pokazać
        local = res["start_utc"].astimezone(tz)
        end = local + timedelta(minutes=res["minutes"])
        res["when"] = fmt_when(local)                              # pełny opis (potwierdzenia)
        res["date_label"] = f"{PL_DAYS[local.weekday()]} {local:%d.%m}"  # nagłówek karty
        res["day"] = f"{local:%d.%m.%Y}"
        res["hours"] = f"{local:%H:%M}–{end:%H:%M}"
        res["book_url"] = LISTING_PAGE_URL.format(id=res["listing_id"]) if res["listing_id"] else ""
        out.append(res)
    # Najpierw nadchodzące (rosnąco), potem minione (od najświeższych).
    out.sort(key=lambda r: (r["past"], r["start_utc"].timestamp() * (-1 if r["past"] else 1)))
    return out, None


def cancel_reservation(tx_id, cfg):
    """Anuluje rezerwację w Decathlon GO. Zwraca (ok, komunikat). Nieodwracalne."""
    tx_id = (tx_id or "").strip()
    if not tx_id:
        return False, "brak identyfikatora rezerwacji"
    token, token_error = ensure_decathlon_token(cfg)
    if token_error:
        return False, token_error
    try:
        doc, _ = panel_rpc("transactions.cancel", cfg, token, {"id": tx_id})
        doc = _flat(doc or {})
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as e:
        return False, _rpc_error(e, "anulowanie")
    state = doc.get("processState") or doc.get("state") or ""
    log(f"✂ Anulowano rezerwację {tx_id}" + (f" (stan: {state})" if state else ""))
    return True, state or "anulowano"


# ------------------------------------------------------------------ kalendarz ICS


def _ics_escape(text):
    """RFC 5545: przecinek, średnik, backslash i nowa linia mają znaczenie składniowe."""
    return (str(text or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _ics_fold(line):
    """RFC 5545: linia > 75 oktetów łamana i kontynuowana spacją (liczymy w bajtach)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, chunk = [], b""
    for ch in line:
        enc = ch.encode("utf-8")
        # Pierwsza linia ma limit 75 B, kolejne 74 B (jeden oktet zjada wiodąca spacja).
        if len(chunk) + len(enc) > (75 if not out else 74):
            out.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += enc
    out.append(chunk.decode("utf-8"))
    return "\r\n ".join(out)


def reservations_ics(reservations, calname="Padel", alarm_minutes=60):
    """Kalendarz iCalendar z rezerwacjami — telefon dodaje go jednym dotknięciem."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//padel-watch//Decathlon GO//PL",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(calname)}",
    ]
    for res in reservations:
        start = res.get("start_utc")
        if not start:
            continue
        end = start + timedelta(minutes=res.get("minutes") or DEFAULT_SLOT_MINUTES)
        # Osobne linie: kalendarze na telefonie łamią opis czytelnie, a link zostaje klikalny.
        desc = "\n".join(filter(None, [
            res.get("slot_name") or "",
            f"Uczestnicy: {', '.join(res['participants'])}" if res.get("participants") else "",
            res.get("book_url") or "",
        ]))
        lines += [
            "BEGIN:VEVENT",
            f"UID:{_ics_escape(res.get('id') or stamp)}@padel-watch",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
            f"DTEND:{end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
            f"SUMMARY:{_ics_escape('🎾 ' + (res.get('title') or 'Padel'))}",
            f"STATUS:{'CANCELLED' if res.get('cancelled') else 'CONFIRMED'}",
        ]
        if res.get("address"):
            lines.append(f"LOCATION:{_ics_escape(res['address'])}")
        if desc.strip():
            lines.append(f"DESCRIPTION:{_ics_escape(desc.strip())}")
        if res.get("book_url"):
            lines.append(f"URL:{res['book_url']}")
        if alarm_minutes and not res.get("cancelled"):
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                      f"DESCRIPTION:{_ics_escape('Padel za ' + str(alarm_minutes) + ' min')}",
                      f"TRIGGER:-PT{int(alarm_minutes)}M", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(l) for l in lines) + "\r\n"


AUTH_FAILURE_MARKERS = ("token odrzucony", "brak tokenu", "nie udało się odświeżyć tokenu",
                        "w panelu Padel")  # komunikaty trybu przeglądarki (scalony dodatek)
# Odśwież JWT, gdy zostało mniej niż tyle sekund ważności. Musi być WYRAŹNIE większe
# niż check_interval — inaczej token zdąży wygasnąć między jednym a drugim sprawdzeniem,
# a /auth/refresh wygasłego tokenu zwraca 401 (sesja ślizgowa: odnawiamy żywy token).
TOKEN_EXPIRY_MARGIN = 300
# Tryb przeglądarki: ile sekund po wygaśnięciu tokenu czekać na odnowienie przez stronę,
# zanim uznamy to za problem. Czytnik budzi się ~10 s po exp i potrzebuje ~6 s na
# załadowanie strony; karencja kryje też pojedynczy nieudany odczyt (retry po ~45 s).
BROWSER_RENEW_GRACE = 180
LATEST_FIRST_VALUES = ("latest", "last", "desc", "najpozniejszy", "najpóźniejszy")
MIN_INTERVAL_SECONDS = 2         # twarda dolna granica INTERVALS (ochrona przed blokadą IP)
AGGRESSIVE_INTERVAL_SECONDS = 5  # poniżej tego logujemy ostrzeżenie


# ------------------------------------------------------------------- salwa

# Zmierzone: 4 strzały po kolei na jednym połączeniu to ~275 ms, te same 4 równolegle
# na ciepłych połączeniach ~73 ms. Przy kolejności 'latest' strzał w 17:00 szedł
# dziś jako czwarty, czyli ~350 ms po pierwszym — i w tym oknie ginęły wieczorne
# godziny. Salwa wysyła najbardziej pożądane terminy naraz.
SALVO_MAX = 6
_salvo_pool = None


def salvo_pool(size):
    """Pula TRWAŁYCH wątków — każdy trzyma własne, ciepłe połączenie (threading.local).

    Świeży wątek oznaczałby świeże połączenie i ~160 ms na uzgodnienie TLS, czyli
    dokładnie to, co salwa ma wyeliminować. Dlatego pula żyje przez cały proces.
    """
    global _salvo_pool
    if _salvo_pool is None:
        _salvo_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=SALVO_MAX, thread_name_prefix="salwa")
    return _salvo_pool


def warm_salvo_connections(size, url):
    """Rozgrzewa `size` połączeń w RÓŻNYCH wątkach puli (bariera je rozdziela).

    Bez bariery szybkie zadania wykonałyby się na jednym wątku i rozgrzałoby się
    jedno połączenie zamiast wszystkich.
    """
    size = max(1, min(int(size), SALVO_MAX))
    barrier = threading.Barrier(size, timeout=10)

    def rozgrzej(_):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept-Encoding": "gzip"})
            with open_url(req, timeout=15) as resp:
                resp.read()
        except Exception:  # noqa: BLE001 - rozgrzewka nie może wywrócić polowania
            pass
        try:
            barrier.wait()   # trzyma wątek zajęty, aż ruszą wszystkie pozostałe
        except threading.BrokenBarrierError:
            pass

    pool = salvo_pool(size)
    list(pool.map(rozgrzej, range(size)))


def fire_salvo(targets, listing_price_by_id, cfg, speculative, size):
    """Wysyła próby rejestracji RÓWNOLEGLE. Zwraca listę wyników w kolejności `targets`.

    Każdy wątek dostaje własną kopię cfg — inaczej odświeżenie tokenu w jednym
    wątku nadpisywałoby stan pozostałych.
    """
    fired = targets[:size]

    def strzal(slot):
        local = dict(cfg)
        started = time.monotonic()
        try:
            ok, msg = register_slot(slot, listing_price_by_id.get(slot["id"]), local,
                                    speculative=speculative)
        except Exception as e:  # noqa: BLE001
            # KRYTYCZNE: wyjątek z jednego wątku NIE MOŻE wywrócić salwy. Inaczej
            # pool.map podnosi go przy odczycie wyników, a rezerwacje zrobione przez
            # pozostałe wątki zostają nieobsłużone — nieanulowane i niezapisane.
            ok, msg = False, f"nieoczekiwany błąd: {e!r}"
        return {"slot": slot, "ok": ok, "msg": msg,
                "ms": int((time.monotonic() - started) * 1000),
                "tx": local.get("transaction_id") or "",
                "token": local.get("token") or ""}

    pool = salvo_pool(size)
    return list(pool.map(strzal, fired))


def auto_register_new_slots(slots, listing_price_by_id, cfg, already_registered):
    """Zapisuje na NOWE wolne terminy, od najwcześniejszego, z limitem na przebieg.

    Bezpieczniki:
    - `max_per_run` (domyślnie 1) — nigdy nie rezerwuje hurtem całego grafiku,
    - twardy błąd autoryzacji przerywa resztę przebiegu (nie dobijamy się do API),
    - w trybie speculative nic nie jest oznaczane jako zapisane.
    """
    if not cfg.get("enabled"):
        return {}, already_registered
    results = {}
    registered = set(already_registered)
    speculative = bool(cfg.get("speculative"))
    limit = cfg.get("max_per_run", 1)
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 1

    # Kolejność prób: 'earliest' (domyślnie) albo 'latest' — patrz opcja auto_register_order.
    latest_first = str(cfg.get("order") or "earliest").strip().lower() in LATEST_FIRST_VALUES
    todo = sorted(
        (s for s in slots if s["id"] not in registered),
        key=lambda s: s["start_utc"],
        reverse=latest_first,
    )
    for s in slots:
        if s["id"] in registered:
            results[s["id"]] = (True, "już zarejestrowane")

    if not todo:
        return results, registered
    if limit == 0:
        log("! Auto-rejestracja: limit auto_register_max=0 — pomijam wszystkie.")
        return results, registered

    cfg["auth_error"] = None
    cfg["pending_ids"] = []
    done = 0
    skipped = []

    # SALWA: najbardziej pożądane terminy lecą NARAZ. Po kolei strzał w czwarty termin
    # szedł ~350 ms po pierwszym — tyle wystarczy, żeby stracić wieczorną godzinę.
    try:
        salvo = max(0, min(int(cfg.get("salvo") or 0), SALVO_MAX))
    except (TypeError, ValueError):
        salvo = 0
    queue = list(todo)
    if salvo > 1 and len(queue) > 1:
        fired = queue[:salvo]
        queue = queue[salvo:]
        opis = ", ".join(fmt_when(s["start_utc"].astimezone(_log_tz()), short=True) for s in fired)
        log(f"⇉ Salwa: {len(fired)} prób równolegle ({opis})")
        wins, auth_error = [], None
        wyniki = fire_salvo(fired, listing_price_by_id, cfg, speculative, salvo)
        # Któryś wątek mógł odnowić token po HTTP 401 (pracował na kopii cfg).
        # Przejmujemy najświeższy, żeby próby sekwencyjne po salwie nie czekały
        # jeszcze raz na to samo odnowienie.
        for res in wyniki:
            cfg["token"] = newer_decathlon_token(cfg.get("token") or "", res.get("token") or "")
        for res in wyniki:
            slot, msg, ms = res["slot"], res["msg"], res["ms"]
            when = fmt_when(slot["start_utc"].astimezone(_log_tz()), short=True)
            results[slot["id"]] = (res["ok"], msg)
            if res["ok"]:
                wins.append(res)
            elif any(m in msg for m in AUTH_FAILURE_MARKERS):
                auth_error = auth_error or msg
                log(f"! Salwa: {when} — {msg} [{ms} ms]")
            else:
                log(f"! Auto-rejestracja nieudana dla {when}: {msg} [{ms} ms]")

        # Zwycięzcy w kolejności preferencji; nadmiar ponad limit oddajemy od razu,
        # żeby nie blokować terminu innym grającym dłużej niż ułamek sekundy.
        for res in wins:
            slot, msg, ms = res["slot"], res["msg"], res["ms"]
            when = fmt_when(slot["start_utc"].astimezone(_log_tz()), short=True)
            if done < limit:
                done += 1
                if speculative:
                    log(f"~ Auto-rejestracja (test, bez rezerwacji): {when} — {msg} [{ms} ms]")
                else:
                    registered.add(slot["id"])
                    log(f"✓ Auto-rejestracja: {when} — {msg} [{ms} ms]")
                continue
            if speculative:
                results[slot["id"]] = (False, "ponad limit auto_register_max")
                continue
            registered.add(slot["id"])   # żeby kolejny bieg nie złapał go ponownie
            if not res["tx"]:
                # Rezerwacja jest, ale serwer nie oddał jej ID — nie mamy czego anulować.
                # Musi to być GŁOŚNE: inaczej zostaje zajęty kort bez śladu w powiadomieniu.
                results[slot["id"]] = (False, "ponad limit, brak ID transakcji — anuluj ręcznie")
                log(f"! Salwa: {when} ponad limit, ale serwer nie zwrócił ID transakcji "
                    f"— anuluj ręcznie w panelu Padel")
                continue
            ok_cancel, msg_cancel = cancel_reservation(res["tx"], cfg)
            if ok_cancel:
                results[slot["id"]] = (False, f"ponad limit — anulowano ({msg_cancel})")
                log(f"↩ Salwa: {when} ponad limit — anulowano ({msg_cancel})")
            else:
                results[slot["id"]] = (False, f"ponad limit — NIE anulowano: {msg_cancel}")
                log(f"! Salwa: {when} ponad limit, a anulowanie NIE POWIODŁO SIĘ "
                    f"({msg_cancel}) — anuluj ręcznie w panelu Padel")

        if auth_error:
            cfg["auth_error"] = auth_error
            cfg["pending_ids"] = [s["id"] for s in todo[:limit]]
            waiting = [fmt_when(s["start_utc"].astimezone(_log_tz()), short=True)
                       for s in todo[:limit]]
            log(f"! Auto-rejestracja przerwana ({auth_error}). "
                f"Zapamiętano do ponowienia: {', '.join(waiting)}.")
            return results, registered

    for slot in queue:
        sid = slot["id"]
        when = fmt_when(slot["start_utc"].astimezone(_log_tz()), short=True)
        if done >= limit:
            skipped.append(when)
            continue
        # Czas każdej próby: przy wyścigu o termin to najważniejsza liczba w Dzienniku —
        # mówi, ile kosztuje nieudany strzał i ile zostało do zwycięskiego.
        attempt_started = time.monotonic()
        ok, msg = register_slot(slot, listing_price_by_id.get(sid), cfg, speculative=speculative)
        took_ms = int((time.monotonic() - attempt_started) * 1000)
        results[sid] = (ok, msg)
        if ok:
            done += 1
            if speculative:
                log(f"~ Auto-rejestracja (test, bez rezerwacji): {when} — {msg} [{took_ms} ms]")
            else:
                registered.add(sid)
                log(f"✓ Auto-rejestracja: {when} — {msg} [{took_ms} ms]")
            continue
        # Twardy błąd autoryzacji -> nie ma sensu próbować kolejnych slotów w tym przebiegu.
        # Zapamiętujemy tylko tyle terminów, ile i tak byśmy zapisali (limit), żeby po
        # naprawieniu tokenu ponowić próbę — bez hurtowego nadrabiania zaległości.
        if any(m in msg for m in AUTH_FAILURE_MARKERS):
            cfg["auth_error"] = msg
            cfg["pending_ids"] = [s["id"] for s in todo[:limit]]
            waiting = [fmt_when(s["start_utc"].astimezone(_log_tz()), short=True) for s in todo[:limit]]
            log(f"! Auto-rejestracja przerwana ({msg}). "
                f"Zapamiętano do ponowienia: {', '.join(waiting)}.")
            return results, registered
        log(f"! Auto-rejestracja nieudana dla {when}: {msg} [{took_ms} ms]")

    if skipped:
        log(f"= Auto-rejestracja: limit {limit}/przebieg wykorzystany; "
            f"czeka {len(skipped)} termin(ów): {', '.join(skipped[:5])}"
            f"{' …' if len(skipped) > 5 else ''}")
    return results, registered


# ----------------------------------------------------------------------- notify

def ntfy_post(topic, title, message, click=None, priority="high", tags="tennis"):
    topic = (topic or "").strip()
    if "://" in topic:  # ktoś wkleił pełny URL zamiast nazwy tematu -> weź ostatni segment
        topic = topic.rstrip("/").split("/")[-1]
    if not topic:
        log("! Pusty temat ntfy — pomijam wysyłkę.")
        return None
    url = f"https://ntfy.sh/{urllib.parse.quote(topic, safe='')}"
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": priority,
        "Tags": tags,
        "User-Agent": UA,
    }
    if click:
        headers["Click"] = click
    req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    # Wysyłka jest NIEBLOKUJĄCA: błąd ntfy nie może wywrócić iteracji ani blokować
    # zapisu stanu (inaczej notyfikacja powtarza się w nieskończoność).
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        log(f"! ntfy {e.code} dla tematu '{topic}': {detail} — popraw nazwę tematu (NTFY_TOPIC / opcja ntfy_topic).")
        return None
    except (urllib.error.URLError, TimeoutError) as e:
        log(f"! ntfy nieosiągalny ({e!r}) — pomijam to powiadomienie.")
        return None


def notify_new(topic, slots, tz, listing_price, book_url, registration_results=None):
    """Powiadom o nowych wolnych terminach. Pojedynczo, a przy wielu — zbiorczo.

    Zwraca zbiór id slotów, których NIE udało się wysłać — wywołujący nie zapisuje
    ich do stanu, więc wysyłka zostanie ponowiona w następnej iteracji.
    """
    if not topic:
        log("! Brak NTFY_TOPIC — pomijam wysyłkę (tryb testowy).")
        return set()  # tryb testowy: nie ma czego ponawiać
    if len(slots) > 6:
        lines = [
            f"• {fmt_when(s['start_utc'].astimezone(tz), short=True)} — {fmt_price(s['price'], listing_price)}"
            for s in slots
        ]
        if registration_results:
            ok_count = sum(1 for s in slots if registration_results.get(s["id"], (False, ""))[0])
            lines.append(f"Auto-rejestracja: {ok_count}/{len(slots)} udanych.")
        status = ntfy_post(
            topic,
            f"🎾 {len(slots)} nowych wolnych terminów padla!",
            "\n".join(lines) + f"\nRezerwuj: {book_url}",
            click=book_url,
        )
        return set() if status else {s["id"] for s in slots}
    failed = set()
    for s in slots:
        when = s["start_utc"].astimezone(tz)
        extra = ""
        if registration_results and s["id"] in registration_results:
            ok, msg = registration_results[s["id"]]
            extra = f"\nAuto-rejestracja: {'OK' if ok else 'nie'} — {msg}"
        status = ntfy_post(
            topic,
            "🎾 Wolny kort padel!",
            f"{fmt_when(when)}\n{s['name']} — {fmt_price(s['price'], listing_price)}{extra}\nRezerwuj: {book_url}",
            click=book_url,
        )
        if status is None:
            failed.add(s["id"])
    return failed


def notify_auth_problem(topic, detail, book_url=None):
    """Alert, że auto-rezerwacja nie działa przez token/cookie (raz na incydent)."""
    if not topic:
        log("! Brak NTFY_TOPIC — pomijam alert o tokenie (tryb testowy).")
        return None
    fix_hint = ("otwórz panel Padel w Home Assistant i zaloguj się ponownie"
                if TOKEN_FILE else
                "wklej świeży go-sdk-jwt w opcję decathlon_token "
                "(DevTools → Application → Local Storage)")
    msg = (
        f"Auto-rezerwacja NIE działa — {fix_hint}.\nMonitorowanie i powiadomienia o wolnych "
        f"terminach działają normalnie.\n\nSzczegóły: {detail}"
    )
    return ntfy_post(
        topic,
        "⚠️ Token Decathlon wygasł",
        msg,
        click=book_url,
        priority="high",
        tags="warning",
    )


def notify_startup(topic, count, tz, book_url=None):
    if not topic:
        log("! Brak NTFY_TOPIC — pomijam powiadomienie startowe (tryb testowy).")
        return
    msg = f"Obserwuję wolne terminy. Aktualnie pasujących wolnych: {count}."
    if book_url:
        msg += f"\nStrona rezerwacji: {book_url}"
    ntfy_post(
        topic,
        "✅ Monitor padla uruchomiony",
        msg,
        click=book_url,
        priority="default",
        tags="white_check_mark",
    )


# -------------------------------------------------------------------------- main

def run_once(announce_startup=False, skip_light=False):
    """Zwraca 0 przy powodzeniu, 2 przy błędzie sieci (stan nietknięty)."""
    cfg = load_config()
    state_doc = load_state_doc()
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic") or ""
    reg_cfg = {
        "enabled": boolish(os.environ.get("AUTO_REGISTER") or cfg.get("auto_register")),
        "speculative": boolish(os.environ.get("AUTO_REGISTER_DRY_RUN") or cfg.get("auto_register_dry_run")),
        # Źródła tokenu: przeglądarka (plik) / ręcznie wklejony w opcjach / zapamiętany
        # w stanie. Wygrywa ten o NAJDALSZYM exp — nie kolejność. Dzięki temu ręcznie
        # wklejony świeży token działa nawet, gdy plik z przeglądarki trzyma stary
        # (np. sesja padła), i odwrotnie.
        "token": newer_decathlon_token(
            newer_decathlon_token(
                token_from_file(),
                os.environ.get("DECATHLON_TOKEN") or cfg.get("decathlon_token") or "",
            ),
            (state_doc or {}).get("decathlon_jwt") or "",
        ),
        "refresh_cookie": os.environ.get("DECATHLON_COOKIE") or cfg.get("decathlon_cookie") or "",
        # rt bywa zwracany przez serwer przy odświeżaniu i zapisywany w stanie (rotacja).
        "refresh_token": (state_doc or {}).get("decathlon_rt") or "",
        # Scalony dodatek: token odnawia przeglądarka, więc monitor NIE próbuje /auth/refresh.
        "browser_mode": bool(TOKEN_FILE),
        "name": os.environ.get("AUTO_REGISTER_NAME") or cfg.get("auto_register_name") or "",
        "age": os.environ.get("AUTO_REGISTER_AGE") or cfg.get("auto_register_age") or None,
        "free_only": not boolish(os.environ.get("AUTO_REGISTER_PAID") or cfg.get("auto_register_paid")),
        "max_per_run": os.environ.get("AUTO_REGISTER_MAX") or cfg.get("auto_register_max") or 1,
        "order": os.environ.get("AUTO_REGISTER_ORDER") or cfg.get("auto_register_order") or "earliest",
        "salvo": os.environ.get("AUTO_REGISTER_SALVO") or cfg.get("auto_register_salvo") or 0,
    }
    tzname = os.environ.get("TIMEZONE") or cfg.get("timezone") or "Europe/Warsaw"
    tz = ZoneInfo(tzname) if ZoneInfo else timezone.utc
    filters_env = os.environ.get("FILTERS")
    if filters_env:
        try:
            filters = parse_filters_env(filters_env)
        except Exception as e:  # noqa: BLE001 - błędny env nie może wywrócić procesu
            log(f"! Błędny FILTERS '{filters_env}': {e} — używam filtrów z config.json")
            filters = cfg.get("filters", [])
    else:
        filters = cfg.get("filters", [])
    now_utc = datetime.now(timezone.utc)

    listings_env = os.environ.get("LISTINGS")
    if listings_env:
        listings = [u.strip() for u in re.split(r"[,\s]+", listings_env) if u.strip()]
    else:
        listings = cfg.get("listings", [])
    book_url = None  # kanoniczny link do rezerwacji (budowany z aktualnego ID)

    current = {}  # id -> slot
    book_url_by_id = {}
    listing_price_by_id = {}

    for url in listings:
        # Podążaj za przekierowaniem -> aktualne ID kortu (do monitoringu i linku).
        lid = resolve_current_id(listing_id_from_url(url))
        canon_url = LISTING_PAGE_URL.format(id=lid)
        if book_url is None:
            book_url = canon_url
        # Krok 1: lekki ping (~1 KB) — licznik dostępności bez ciężkiego payloadu.
        # W ZRYWIE go pomijamy: oszczędza transfer, ale kosztuje całą rundę do serwera
        # (~110 ms), a pełne dane i tak są potrzebne po identyfikatory terminów —
        # i niosą te same atrybuty kortu, więc nic przez to nie tracimy.
        doc = None
        fetch_started = time.monotonic()
        try:
            if skip_light:
                doc = fetch_listing(lid)
                attrs = (doc.get("data", {}).get("attributes", {}) or {})
            else:
                attrs = (fetch_listing_light(lid).get("data", {}).get("attributes", {}) or {})
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            log(f"! Błąd pobierania kortu {lid}: {e} — nie zmieniam stanu, kończę.")
            return 2  # błąd sieci: nie nadpisuj stanu
        fetch_ms = int((time.monotonic() - fetch_started) * 1000)
        listing_price = attrs.get("price")
        title = attrs.get("title", lid)
        avail = (attrs.get("datesStats") or {}).get("availableListingDates") or 0
        if avail <= 0 and doc is None:
            # Brak jakichkolwiek wolnych terminów -> nie ma czego filtrować ani pobierać.
            log(f"= {title}: 0 dostępnych (lekki ping ~1 KB), pomijam pełne pobranie")
            continue
        # Krok 2: coś jest wolne -> dopiero teraz ciężki payload (~21 KB gzip) i filtr.
        if doc is None:
            try:
                doc = fetch_listing(lid)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                log(f"! Błąd pobierania terminów kortu {lid}: {e} — nie zmieniam stanu, kończę.")
                return 2  # błąd sieci: nie nadpisuj stanu
        slots = [s for s in free_slots(doc, lid, now_utc) if passes_filter(s, filters, tz)]
        # W zrywie dokładamy czas pobrania — pozwala oddzielić opóźnienie sieci
        # od opóźnienia wykrycia przy analizie logu po polowaniu.
        log(f"= {title}: {avail} dostępnych, {len(slots)} pasujących do filtra"
            + (f" (pobranie {fetch_ms} ms)" if skip_light else ""))
        for s in slots:
            current[s["id"]] = s
            book_url_by_id[s["id"]] = canon_url
            listing_price_by_id[s["id"]] = listing_price
        for s in sorted(slots, key=lambda x: x["start_utc"]):
            log(f"   - {fmt_when(s['start_utc'].astimezone(tz), short=True)}  {s['name']}  {s['count']}/{s['limit']}")

    current_ids = set(current.keys())
    prev = None if state_doc is None else set(state_doc.get("free_ids", []))

    # Powiadomienie startowe: przy każdym uruchomieniu aplikacji (announce_startup)
    # oraz przy pierwszym biegu bez zapisanego stanu.
    if announce_startup or prev is None:
        notify_startup(topic, len(current_ids), tz, book_url)

    # Test poświadczeń Decathlon GO — NIE wymaga wolnego terminu. Uruchamiany przy
    # starcie procesu (gdy auto_register włączone) albo na żądanie opcją test_token.
    test_token = boolish(os.environ.get("TEST_TOKEN") or cfg.get("test_token"))
    if (announce_startup or test_token) and (reg_cfg.get("enabled") or test_token):
        check_decathlon_credentials(reg_cfg, topic, book_url)
    elif reg_cfg.get("enabled"):
        # PODTRZYMANIE SESJI: token trzeba odnawiać w KAŻDEJ iteracji, nie tylko gdy jest
        # co rezerwować. Inaczej po dłuższej ciszy (brak wolnych terminów) JWT wygasa, a
        # /auth/refresh wygasłego tokenu zwraca 401 — i pierwsza okazja przepada.
        # Wywołanie jest tanie: bez ruchu sieciowego, dopóki do wygaśnięcia > marginesu.
        _tok, _err = ensure_decathlon_token(reg_cfg)
        reg_cfg["auth_checked"] = True  # pozwala skasować alert, gdy token znów działa
        if _err:
            reg_cfg["auth_error"] = _err
            log(f"! Podtrzymanie sesji Decathlon GO nieudane: {_err}")

    if prev is None:
        log("Pierwszy bieg — zapisuję baseline, bez alertów o pojedynczych terminach.")
        save_state(current_ids, decathlon_jwt=reg_cfg.get("token"),
                   decathlon_rt=reg_cfg.get("refresh_token"))
        return 0

    new_ids = current_ids - prev
    failed_ids = set()
    registered_ids = load_registered_ids()
    registration_results = {}

    # Auto-rejestracja: kandydaci to NOWE terminy + te zapamiętane po awarii tokenu
    # (pending), o ile nadal są wolne i jeszcze niezapisane. Dzięki temu naprawienie
    # cookie sprawia, że automat dogoni termin, którego wcześniej nie mógł zająć.
    pending_prev = set(state_doc.get("pending_ids", []))
    candidate_ids = ((new_ids | pending_prev) & current_ids) - registered_ids
    retried = (pending_prev & candidate_ids) - new_ids
    if candidate_ids and reg_cfg.get("enabled"):
        if retried:
            log(f"↻ Ponawiam auto-rejestrację dla {len(retried)} zapamiętanego(-ych) "
                f"terminu(-ów) po wcześniejszym błędzie tokenu.")
        registration_results, registered_ids = auto_register_new_slots(
            [current[i] for i in candidate_ids], listing_price_by_id, reg_cfg, registered_ids
        )

    # Alert o tokenie: raz na incydent (kasowany, gdy token znów działa).
    auth_error = reg_cfg.get("auth_error")
    auth_alert_sent = bool(state_doc.get("auth_alert_sent"))
    auth_verified = bool(candidate_ids) or reg_cfg.get("auth_checked")
    if auth_error and not auth_alert_sent:
        notify_auth_problem(topic, auth_error, book_url)
        auth_alert_sent = True
    elif not auth_error and auth_alert_sent and auth_verified and reg_cfg.get("enabled"):
        log("✓ Token Decathlon znów działa — kasuję alert.")
        auth_alert_sent = False

    if new_ids:
        log(f"NOWE wolne terminy: {len(new_ids)}")
        new_slots = sorted((current[i] for i in new_ids), key=lambda x: x["start_utc"])
        # grupuj powiadomienia per listing (book_url)
        by_url = {}
        for s in new_slots:
            by_url.setdefault(book_url_by_id[s["id"]], []).append(s)
        for url, slots in by_url.items():
            failed_ids |= notify_new(
                topic, slots, tz, listing_price_by_id[slots[0]["id"]], url, registration_results
            )
        if failed_ids:
            log(f"! Nie wysłano {len(failed_ids)} powiadomień — ponowię w następnej iteracji.")
    else:
        log("Brak nowych wolnych terminów.")

    # Sloty z nieudaną wysyłką NIE trafiają do stanu -> następna iteracja
    # potraktuje je znów jako nowe i ponowi powiadomienie.
    save_state(
        current_ids - failed_ids,
        registered_ids,
        reg_cfg.get("token"),
        pending_ids=reg_cfg.get("pending_ids") or [],
        auth_alert_sent=auth_alert_sent,
        decathlon_rt=reg_cfg.get("refresh_token"),
    )
    return 0


def main():
    """Jednorazowo (domyślnie) albo w pętli, jeśli CHECK_INTERVAL > 0 (sekundy).

    Tryb pętli jest przeznaczony do kontenera Docker / własnego serwera — proces
    żyje cały czas i sprawdza terminy co CHECK_INTERVAL sekund. Pojedynczy błąd
    nie zabija procesu — logujemy i próbujemy ponownie w kolejnej iteracji.
    """
    try:
        interval = int(os.environ.get("CHECK_INTERVAL", "0"))
    except ValueError:
        interval = 0

    # Czyszczenie stanu wykonujemy RAZ przy starcie procesu, nie w każdej iteracji.
    try:
        apply_clear_state()
    except OSError as e:
        log(f"! Nie udało się wyczyścić stanu: {e!r} — kontynuuję.")

    if interval <= 0:
        run_once()
        return 0  # tryb jednorazowy — nie wywracaj wywołującego

    # Opcjonalne okna z inną częstotliwością (INTERVALS), w strefie TIMEZONE.
    tzname = os.environ.get("TIMEZONE") or "Europe/Warsaw"
    try:
        tz = ZoneInfo(tzname) if ZoneInfo else timezone.utc
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    windows = []
    intervals_env = os.environ.get("INTERVALS", "")
    if intervals_env.strip():
        try:
            windows = parse_intervals_env(intervals_env)
            desc = "; ".join(f"{','.join(w['days'])} {w['start']}-{w['end']} co {w['seconds']}s" for w in windows)
            log(f"Okna częstotliwości: {desc} (poza nimi co {interval}s)")
        except Exception as e:  # noqa: BLE001 - błędny env nie może wywrócić procesu
            log(f"! Błędny INTERVALS '{intervals_env}': {e} — używam stałego {interval}s")

    # Zryw: krótkie, gęste sprawdzanie wycelowane w sekundę publikacji grafiku.
    burst = None
    burst_env = os.environ.get("BURST", "")
    if burst_env.strip():
        try:
            burst = parse_burst_env(burst_env)
            burst["seconds"] = max(1, min(int(os.environ.get("BURST_SECONDS") or 15),
                                          BURST_MAX_SECONDS))
            burst["interval"] = max(BURST_MIN_INTERVAL,
                                    float(os.environ.get("BURST_INTERVAL") or 0.5))
            hour, minute, second = burst["at"]
            log(f"⚡ Zryw: {','.join(burst['days'])} o {hour:02d}:{minute:02d}:{second:02d}, "
                f"przez {burst['seconds']}s co {burst['interval']}s "
                f"(bez lekkiego pingu, po podtrzymanym połączeniu)")
        except Exception as e:  # noqa: BLE001 - błędna opcja nie może wywrócić monitora
            log(f"! Błędny BURST '{burst_env}': {e} — zryw wyłączony")
            burst = None

    # Salwa: ile prób rejestracji wysyłać naraz i pod jakim adresem rozgrzewać połączenia.
    try:
        salvo_size = max(0, min(int(os.environ.get("AUTO_REGISTER_SALVO") or 0), SALVO_MAX))
    except ValueError:
        salvo_size = 0
    warm_url = ""
    first_listing = [u for u in re.split(r"[,\s]+", os.environ.get("LISTINGS", "")) if u.strip()]
    if first_listing:
        warm_url = LISTING_URL.format(id=listing_id_from_url(first_listing[0]))
    if salvo_size > 1:
        log(f"⇉ Salwa włączona: do {salvo_size} prób rejestracji równolegle "
            f"(nadmiar ponad auto_register_max jest anulowany).")

    log(f"Tryb pętli: sprawdzam co {interval}s. Ctrl+C aby zakończyć.")
    first = True  # powiadomienie startowe na pierwszej UDANEJ iteracji procesu
    last_sleep = None
    in_burst = False
    burst_window = None   # granice trwającego zrywu — do uczciwego opisu w linii „koniec"
    while True:
        started = time.monotonic()
        now_local = datetime.now(tz)
        bounds = burst_bounds(burst, tz, now_local)
        active = bool(bounds and bounds[0] <= now_local < bounds[1])
        if active != in_burst:
            set_log_precision(True)   # obie linie przejścia z milisekundami — wyznaczają okno
            if active:
                burst_window = bounds
                log(f"⚡ Zryw START — co {burst['interval']}s przez {burst['seconds']}s")
                # Połączenia salwy stygną między polowaniami (serwer zamyka bezczynne),
                # więc rozgrzewamy je TERAZ — inaczej pierwszy strzał zapłaciłby ~160 ms
                # za uzgodnienie TLS, czyli dokładnie to, co salwa ma wyeliminować.
                if salvo_size > 1 and warm_url:
                    warmed = time.monotonic()
                    warm_salvo_connections(salvo_size, warm_url)
                    log(f"⇉ Salwa gotowa: {salvo_size} ciepłych połączeń "
                        f"[{int((time.monotonic() - warmed) * 1000)} ms]")
            else:
                # Ta linia leci dopiero przy NASTĘPNYM sprawdzeniu, czyli sporo po końcu
                # okna — dlatego podajemy faktyczne granice, a nie sugerujemy „teraz".
                okno = (f" (okno {burst_window[0]:%H:%M:%S}–{burst_window[1]:%H:%M:%S})"
                        if burst_window else "")
                log(f"⚡ Zryw koniec{okno} — wracam do zwykłego taktu")
                burst_window = None
            set_log_precision(active)  # milisekundy zostają tylko na czas zrywu
            in_burst = active
            last_sleep = None
        try:
            rc = run_once(announce_startup=first, skip_light=active)
            if rc != 2:  # 2 = błąd sieci; ponów próbę startowego powiadomienia później
                first = False
        except Exception as e:  # noqa: BLE001 - pętla ma przetrwać każdy błąd
            log(f"! Nieoczekiwany błąd w iteracji: {e!r} — kontynuuję.")
        # Czas pracy odejmujemy od uśpienia: bez tego ustawione 2s dawały realnie ~2,4s.
        elapsed = time.monotonic() - started
        sleep_s = plan_sleep(interval, windows, burst, tz, elapsed)
        if not active and windows:
            shown = current_interval(interval, windows, tz)
            if shown != last_sleep:
                log(f"⏱ aktualny interwał: {shown}s")
                last_sleep = shown
        time.sleep(sleep_s)


if __name__ == "__main__":
    sys.exit(main())
