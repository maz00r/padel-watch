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
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 (nie powinno wystąpić w CI)
    ZoneInfo = None

HERE = os.path.dirname(os.path.abspath(__file__))
# CONFIG_PATH / STATE_DIR można nadpisać zmienną środowiskową (przydatne w Dockerze).
CONFIG_PATH = os.environ.get("CONFIG_PATH") or os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(os.environ.get("STATE_DIR") or HERE, "state.json")

LISTING_URL = "https://go.decathlon.pl/api/listing/{id}"  # lekki (~1 KB): kort + datesStats
LISTING_DATES_URL = LISTING_URL + "?include=dates"        # ciężki (~257 KB): + wszystkie terminy
LISTING_PAGE_URL = "https://go.decathlon.pl/l/{id}"       # strona kortu (podąża za 301 na nowe ID)
DECATHLON_API_URL = "https://go.decathlon.pl/api"
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


def log(*args):
    ts = datetime.now(_log_tz()).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}]", *args, flush=True)


# --------------------------------------------------------------------------- IO

def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log(f"(brak {CONFIG_PATH} — używam wartości z ENV)")
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


def save_state(free_ids, registered_ids=None, decathlon_jwt=None):
    old = load_state_doc() or {}
    if registered_ids is None:
        registered_ids = set(old.get("registered_ids", []))
    if decathlon_jwt is None:
        decathlon_jwt = old.get("decathlon_jwt")
    doc = {"free_ids": sorted(free_ids), "registered_ids": sorted(registered_ids)}
    if decathlon_jwt:
        doc["decathlon_jwt"] = clean_decathlon_token(decathlon_jwt)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")


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


def http_get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
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
        seconds = max(10, int(secs))  # min 10 s — nie młócimy API
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


def newer_decathlon_token(config_token, state_token):
    config_token = clean_decathlon_token(config_token)
    state_token = clean_decathlon_token(state_token)
    if not config_token:
        return state_token
    if not state_token:
        return config_token
    return state_token if jwt_expiry(state_token) > jwt_expiry(config_token) else config_token


def refresh_decathlon_token(token, cookie):
    cookie = (cookie or "").strip()
    if not cookie:
        raise ValueError("brak cookie sesji Decathlon GO")
    req = urllib.request.Request(
        f"{DECATHLON_API_URL}/auth/refresh",
        data=json.dumps({}).encode("utf-8"),
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
            "Authorization": f"Bearer {clean_decathlon_token(token)}",
            "Cookie": cookie,
        },
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
    return refreshed


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
    with urllib.request.urlopen(req, timeout=30) as resp:
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
    token = clean_decathlon_token(cfg.get("token"))
    name = (cfg.get("name") or "").strip()
    if not token:
        return False, "brak tokenu Decathlon GO"
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
            if e.code == 401 and attempt == 0 and cfg.get("refresh_cookie"):
                try:
                    token = refresh_decathlon_token(token, cfg.get("refresh_cookie"))
                    cfg["token"] = token
                    cfg["token_refreshed"] = True
                    log("~ Token Decathlon GO odświeżony po HTTP 401; ponawiam rejestrację")
                    continue
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as refresh_error:
                    return False, f"token odrzucony (HTTP 401), refresh nieudany: {refresh_error!r}"
            if e.code in (401, 403):
                return False, f"token odrzucony (HTTP {e.code}) — sprawdź decathlon_cookie / token"
            return False, f"Decathlon HTTP {e.code}: {detail or e.reason}"
        except (urllib.error.URLError, TimeoutError) as e:
            return False, f"Decathlon niedostępny: {e!r}"
    state = doc.get("processState") or doc.get("state") or (doc.get("data") or {}).get("processState")
    if speculative:
        return True, f"walidacja OK (speculative{', ' + state if state else ''})"
    if state == "pending-payment":
        return False, "utworzono rezerwację, ale wymaga płatności"
    return True, state or "zarejestrowano"


AUTH_FAILURE_MARKERS = ("token odrzucony", "brak tokenu")


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

    todo = sorted(
        (s for s in slots if s["id"] not in registered),
        key=lambda s: s["start_utc"],  # najwcześniejszy termin ma pierwszeństwo
    )
    for s in slots:
        if s["id"] in registered:
            results[s["id"]] = (True, "już zarejestrowane")

    if not todo:
        return results, registered
    if limit == 0:
        log("! Auto-rejestracja: limit auto_register_max=0 — pomijam wszystkie.")
        return results, registered

    done = 0
    skipped = []
    for slot in todo:
        sid = slot["id"]
        when = fmt_when(slot["start_utc"].astimezone(_log_tz()), short=True)
        if done >= limit:
            skipped.append(when)
            continue
        ok, msg = register_slot(slot, listing_price_by_id.get(sid), cfg, speculative=speculative)
        results[sid] = (ok, msg)
        if ok:
            done += 1
            if speculative:
                log(f"~ Auto-rejestracja (test, bez rezerwacji): {when} — {msg}")
            else:
                registered.add(sid)
                log(f"✓ Auto-rejestracja: {when} — {msg}")
            continue
        # Twardy błąd autoryzacji -> nie ma sensu próbować kolejnych slotów w tym przebiegu.
        if any(m in msg for m in AUTH_FAILURE_MARKERS):
            remaining = [fmt_when(s["start_utc"].astimezone(_log_tz()), short=True)
                         for s in todo if s["id"] not in results]
            log(f"! Auto-rejestracja przerwana ({msg}). Pominięto {len(remaining)} termin(y) w tym przebiegu.")
            return results, registered
        log(f"! Auto-rejestracja nieudana dla {when}: {msg}")

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

def run_once(announce_startup=False):
    """Zwraca 0 przy powodzeniu, 2 przy błędzie sieci (stan nietknięty)."""
    cfg = load_config()
    state_doc = load_state_doc()
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic") or ""
    reg_cfg = {
        "enabled": boolish(os.environ.get("AUTO_REGISTER") or cfg.get("auto_register")),
        "speculative": boolish(os.environ.get("AUTO_REGISTER_DRY_RUN") or cfg.get("auto_register_dry_run")),
        "token": newer_decathlon_token(
            os.environ.get("DECATHLON_TOKEN") or cfg.get("decathlon_token") or "",
            (state_doc or {}).get("decathlon_jwt") or "",
        ),
        "refresh_cookie": os.environ.get("DECATHLON_COOKIE") or cfg.get("decathlon_cookie") or "",
        "name": os.environ.get("AUTO_REGISTER_NAME") or cfg.get("auto_register_name") or "",
        "age": os.environ.get("AUTO_REGISTER_AGE") or cfg.get("auto_register_age") or None,
        "free_only": not boolish(os.environ.get("AUTO_REGISTER_PAID") or cfg.get("auto_register_paid")),
        "max_per_run": os.environ.get("AUTO_REGISTER_MAX") or cfg.get("auto_register_max") or 1,
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
        # Krok 1: lekki ping (~1 KB) — sprawdź licznik dostępności bez ciężkiego payloadu.
        try:
            light = fetch_listing_light(lid)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            log(f"! Błąd pobierania kortu {lid}: {e} — nie zmieniam stanu, kończę.")
            return 2  # błąd sieci: nie nadpisuj stanu
        attrs = (light.get("data", {}).get("attributes", {}) or {})
        listing_price = attrs.get("price")
        title = attrs.get("title", lid)
        avail = (attrs.get("datesStats") or {}).get("availableListingDates") or 0
        if avail <= 0:
            # Brak jakichkolwiek wolnych terminów -> nie ma czego filtrować ani pobierać.
            log(f"= {title}: 0 dostępnych (lekki ping ~1 KB), pomijam pełne pobranie")
            continue
        # Krok 2: coś jest wolne -> dopiero teraz ciężki payload (~257 KB) i filtr czasowy.
        try:
            doc = fetch_listing(lid)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            log(f"! Błąd pobierania terminów kortu {lid}: {e} — nie zmieniam stanu, kończę.")
            return 2  # błąd sieci: nie nadpisuj stanu
        slots = [s for s in free_slots(doc, lid, now_utc) if passes_filter(s, filters, tz)]
        log(f"= {title}: {avail} dostępnych, {len(slots)} pasujących do filtra")
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

    if prev is None:
        log("Pierwszy bieg — zapisuję baseline, bez alertów o pojedynczych terminach.")
        save_state(current_ids, decathlon_jwt=reg_cfg.get("token"))
        return 0

    new_ids = current_ids - prev
    failed_ids = set()
    registered_ids = load_registered_ids()
    registration_results = {}
    if new_ids:
        log(f"NOWE wolne terminy: {len(new_ids)}")
        new_slots = sorted((current[i] for i in new_ids), key=lambda x: x["start_utc"])
        registration_results, registered_ids = auto_register_new_slots(
            new_slots, listing_price_by_id, reg_cfg, registered_ids
        )
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
    save_state(current_ids - failed_ids, registered_ids, reg_cfg.get("token"))
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

    log(f"Tryb pętli: sprawdzam co {interval}s. Ctrl+C aby zakończyć.")
    first = True  # powiadomienie startowe na pierwszej UDANEJ iteracji procesu
    last_sleep = None
    while True:
        try:
            rc = run_once(announce_startup=first)
            if rc != 2:  # 2 = błąd sieci; ponów próbę startowego powiadomienia później
                first = False
        except Exception as e:  # noqa: BLE001 - pętla ma przetrwać każdy błąd
            log(f"! Nieoczekiwany błąd w iteracji: {e!r} — kontynuuję.")
        sleep_s = current_interval(interval, windows, tz)
        if sleep_s != last_sleep and windows:
            log(f"⏱ aktualny interwał: {sleep_s}s")
            last_sleep = sleep_s
        time.sleep(sleep_s)


if __name__ == "__main__":
    sys.exit(main())
