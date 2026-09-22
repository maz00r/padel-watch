#!/usr/bin/env python3
"""Read-only monitor dostępności zwykłych biletów do Muzeów Watykańskich.

Korzysta wyłącznie z publicznego endpointu, nie loguje się i nie składa rezerwacji.
Uruchomienie lokalne (bez powiadomień):
    STATE_DIR=/tmp/vatican NTFY_TOPIC='' python3 check_vatican.py
"""

import email.utils
import gzip
import json
import os
import random
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - obraz dodatku zawiera zoneinfo
    ZoneInfo = None

VERSION = "0.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(os.environ.get("STATE_DIR") or HERE, "vatican_state.json")
API_URL = "https://tickets.museivaticani.va/api/search/result"
HOME_URL = "https://tickets.museivaticani.va/home"
USER_AGENT = f"vatican-watch/{VERSION} (read-only availability monitor)"
ROME = "Europe/Rome"
TARGET_NAME = "Musei Vaticani - Biglietti d'ingresso"
AVAILABLE_STATES = {"AVAILABLE", "LOW_AVAILABILITY"}
KNOWN_STATES = AVAILABLE_STATES | {"SOLD_OUT", "NOT_ALLOWED", "MISSING"}
FAILURE_ALERT_AFTER = 3
DATE_GAP_SECONDS = 0.4
MAX_BACKOFF_SECONDS = 3600

_TZ = None


class VaticanError(Exception):
    """Błąd odpowiedzi lub połączenia, który nie może zmienić dostępności."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def rome_tz():
    global _TZ
    if _TZ is None:
        if ZoneInfo is None:
            raise ValueError("Python nie udostępnia danych stref czasowych")
        _TZ = ZoneInfo(ROME)
    return _TZ


def log(message):
    stamp = datetime.now(rome_tz()).strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{stamp}] {message}", flush=True)


def parse_date(value, option_name):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{option_name} musi mieć format RRRR-MM-DD") from exc


def load_config():
    """Odczyt opcji dodatku; termin i liczba osób są celowo ograniczone do planu."""
    start = parse_date(os.environ.get("START_DATE") or "2026-09-24", "start_date")
    end = parse_date(os.environ.get("END_DATE") or "2026-09-28", "end_date")
    if end < start:
        raise ValueError("end_date nie może być wcześniejszy niż start_date")
    try:
        interval = int(os.environ.get("CHECK_INTERVAL") or "60")
    except ValueError as exc:
        raise ValueError("check_interval musi być liczbą całkowitą") from exc
    if not 30 <= interval <= 3600:
        raise ValueError("check_interval musi być w zakresie 30–3600 sekund")
    # Walidujemy strefę teraz, aby błąd obrazu/configu nie dawał pozornie poprawnych dat.
    rome_tz()
    return {
        "start_date": start,
        "end_date": end,
        "interval": interval,
        "topic": (os.environ.get("NTFY_TOPIC") or "").strip(),
        "visitors": 5,
    }


def active_dates(cfg, today=None):
    """Daty z zakresu, które nie minęły jeszcze w strefie Muzeów Watykańskich."""
    today = today or datetime.now(rome_tz()).date()
    first = max(cfg["start_date"], today)
    if first > cfg["end_date"]:
        return []
    return [first + timedelta(days=offset)
            for offset in range((cfg["end_date"] - first).days + 1)]


def normalize_name(value):
    """Porównanie nazw niezależne od akcentów, odstępów i typograficznego myślnika."""
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(value.casefold().replace("–", "-").split())


def is_target_product(product):
    """Tylko zwykły bilet, bez dostępnych sugestii, wycieczek i innych obiektów."""
    if not isinstance(product, dict):
        return False
    if str(product.get("suggestion") or "").strip():
        return False
    name = normalize_name(product.get("name"))
    if name != normalize_name(TARGET_NAME):
        return False
    # Druga, jawna blokada na wypadek przyszłej zmiany części nazwy po stronie API.
    forbidden = ("guidat", "didatt", "univers", "pellegrin", "castel", "palazzo", "giardin")
    return not any(word in name for word in forbidden)


def select_target_product(payload):
    """Zwraca produkt albo None, gdy zwykły bilet nie jest danego dnia wystawiony."""
    if not isinstance(payload, dict):
        raise VaticanError("API zwróciło JSON inny niż obiekt")
    visits = payload.get("visits")
    if not isinstance(visits, list):
        raise VaticanError("API nie zawiera listy visits")
    matches = [item for item in visits if is_target_product(item)]
    if not matches:
        return None
    if len(matches) > 1:
        raise VaticanError("więcej niż jeden zwykły bilet Muzeów Watykańskich w odpowiedzi API")
    return matches[0]


def status_from_product(product):
    status = str(product.get("availability") or "").strip().upper()
    return status if status in KNOWN_STATES else "MISSING"


def parse_retry_after(value, now=None):
    """Retry-After w sekundach lub jako data HTTP; ujemny wynik oznacza natychmiast."""
    if not value:
        return None
    try:
        return max(0, int(str(value).strip()))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return max(0, int((parsed - now).total_seconds()))
    except (TypeError, ValueError, IndexError):
        return None


def http_get_json(url):
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    })
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as exc:
        retry = parse_retry_after(exc.headers.get("Retry-After"))
        kind = "limit odpowiedzi" if exc.code == 429 else "błąd serwera" if exc.code >= 500 else "błąd HTTP"
        raise VaticanError(f"{kind} {exc.code}", retry_after=retry) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VaticanError(f"błąd połączenia: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VaticanError("API zwróciło niepoprawny JSON") from exc


def result_url(day, visitors=5):
    """Oficjalna trasa Angulara wyników, nie sam formularz /home z query stringiem."""
    local_midnight = datetime(day.year, day.month, day.day, tzinfo=rome_tz())
    epoch_ms = int(local_midnight.timestamp() * 1000)
    return f"{HOME_URL}/visit/{visitors}/{epoch_ms}/1/"


def fetch_status(day, visitors=5):
    url = API_URL + "?" + urllib.parse.urlencode({
        "lang": "it", "visitorNum": str(visitors), "visitDate": day.strftime("%d/%m/%Y"),
        "area": "1", "who": "", "page": "0",
    })
    product = select_target_product(http_get_json(url))
    if product is None:
        return "MISSING", ""
    return status_from_product(product), str(product.get("id") or "")


def default_state():
    return {"last_observed": {}, "last_notified": {}, "last_missing": {},
            "failure": {"count": 0, "alerted": False}}


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as state_file:
            loaded = json.load(state_file)
    except FileNotFoundError:
        return default_state()
    except (OSError, ValueError):
        log("⚠ Uszkodzony stan — zaczynam od nowego, nie traktuję go jako awarii API.")
        return default_state()
    if not isinstance(loaded, dict):
        log("⚠ Stan ma niewłaściwy format — zaczynam od nowego.")
        return default_state()
    state = default_state()
    for key in ("last_observed", "last_notified", "last_missing"):
        if isinstance(loaded.get(key), dict):
            state[key] = {str(day): str(status) for day, status in loaded[key].items()}
    if isinstance(loaded.get("failure"), dict):
        try:
            state["failure"]["count"] = max(0, int(loaded["failure"].get("count") or 0))
        except (TypeError, ValueError):
            log("⚠ Stan awarii ma niewłaściwy format — zeruję licznik błędów.")
        state["failure"]["alerted"] = bool(loaded["failure"].get("alerted"))
    return state


def save_state(state):
    """Zapis atomowy w tym samym katalogu — os.replace nie pozostawi połowy JSON-a."""
    directory = os.path.dirname(STATE_PATH) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".vatican_state.", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, ensure_ascii=False, sort_keys=True)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(tmp_path, STATE_PATH)
    except OSError as exc:
        log(f"⚠ Nie zapisałem stanu {STATE_PATH}: {exc}")
        try:
            os.unlink(tmp_path)
        except (OSError, UnboundLocalError):
            pass


def ntfy_post(topic, title, message, click=None, priority="default", tags="ticket"):
    """True oznacza dostarczenie albo świadomy tryb bez powiadomień."""
    if not topic:
        log("Brak ntfy_topic — tryb bez powiadomień.")
        return True
    headers = {
        "Title": title.encode("utf-8"), "Priority": priority, "Tags": tags,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if click:
        headers["Click"] = click
    request = urllib.request.Request(
        "https://ntfy.sh/" + urllib.parse.quote(topic, safe=""),
        data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20):
            return True
    except urllib.error.HTTPError as exc:
        log(f"⚠ ntfy HTTP {exc.code}; alert zostanie ponowiony.")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log(f"⚠ ntfy niedostępne ({exc}); alert zostanie ponowiony.")
    return False


def describe_alert(days, observed, visitors):
    lines = [f"{day.strftime('%d.%m.%Y')}: {observed[day.isoformat()]}" for day in days]
    return ("Dostępne zwykłe bilety do Muzeów Watykańskich dla "
            f"{visitors} osób:\n" + "\n".join(lines) + "\nKup ręcznie w oficjalnym systemie.")


def retry_delay(interval, failure_count, retry_after=None):
    exponential = min(MAX_BACKOFF_SECONDS, 30 * (2 ** min(failure_count, 7)))
    return min(MAX_BACKOFF_SECONDS, max(interval, exponential, retry_after or 0))


def record_failure(state, cfg, error):
    """Zapisuje tylko zdrowie API, nigdy dostępność; alarm po trzech błędach."""
    failure = state["failure"]
    failure["count"] += 1
    log(f"⚠ API Watykanu: {error}. Kolejny błąd z rzędu: {failure['count']}.")
    if failure["count"] >= FAILURE_ALERT_AFTER and not failure["alerted"]:
        sent = ntfy_post(cfg["topic"], "⚠ Watykan Watch: problem z API",
                         "Nie udało się sprawdzić dostępności w oficjalnym API. "
                         "Stan biletów nie został zmieniony; monitor ponowi próbę.",
                         priority="default", tags="warning")
        if sent:
            failure["alerted"] = True
    save_state(state)
    return retry_delay(cfg["interval"], failure["count"], getattr(error, "retry_after", None))


def run_once(announce_startup=False, today=None):
    """Jeden pełny cykl. Zwraca (kod, sugerowane opóźnienie); 2 oznacza błąd API/push."""
    try:
        cfg = load_config()
    except ValueError as exc:
        log(f"✗ Błędna konfiguracja: {exc}")
        return 1, None
    days = active_dates(cfg, today=today)
    if not days:
        log("Zakres monitorowania zakończony — brak dni do sprawdzenia.")
        return 0, None

    state = load_state()
    observed = {}
    product_ids = {}
    try:
        for index, day in enumerate(days):
            status, product_id = fetch_status(day, cfg["visitors"])
            observed[day.isoformat()] = status
            product_ids[day.isoformat()] = product_id
            if index + 1 < len(days):
                time.sleep(DATE_GAP_SECONDS)
    except VaticanError as exc:
        return 2, record_failure(state, cfg, exc)

    previous_failure = state["failure"]
    if previous_failure["alerted"]:
        if not ntfy_post(cfg["topic"], "✅ Watykan Watch: API znów działa",
                         "Oficjalne API znów odpowiada. Monitor kontynuuje sprawdzanie biletów.",
                         priority="default", tags="white_check_mark"):
            # Zachowujemy statusy, ale ponowimy informację o odzyskaniu w kolejnym cyklu.
            return 2, cfg["interval"]
        log("API odzyskało działanie.")
    state["failure"] = {"count": 0, "alerted": False}

    if announce_startup:
        ntfy_post(cfg["topic"], "🎟️ Watykan Watch uruchomiony",
                  f"Monitoruję zwykłe bilety do Muzeów Watykańskich dla {cfg['visitors']} osób: "
                  f"{cfg['start_date']:%d.%m.%Y}–{cfg['end_date']:%d.%m.%Y}. "
                  f"Sprawdzanie co {cfg['interval']} s.", priority="default", tags="ticket")

    alerts = [day for day in days
              if observed[day.isoformat()] in AVAILABLE_STATES
              and day.isoformat() not in state["last_notified"]]
    if alerts:
        earliest = min(alerts)
        body = describe_alert(alerts, observed, cfg["visitors"])
        log("DOSTĘPNE: " + ", ".join(
            f"{day:%d.%m} ({observed[day.isoformat()]}, produkt {product_ids[day.isoformat()] or '?'})"
            for day in alerts))
        if not ntfy_post(cfg["topic"], "🎟️ Watykan: bilety dostępne", body,
                         click=result_url(earliest, cfg["visitors"]), priority="high", tags="ticket"):
            # Celowo nie zatwierdzamy żadnej zmiany dostępności: alert nie może zginąć.
            return 2, cfg["interval"]

    # Tylko pełny, poprawny odczyt może aktualizować dostępność. SOLD_OUT usuwa
    # potwierdzenie alertu, dzięki czemu następne pojawienie się znów go wywoła.
    # Dla niedziel i innych dni, w których API świadomie nie wystawia zwykłego
    # produktu, MISSING jest informacją diagnostyczną. Nie wolno nim zastąpić
    # poprzedniego SOLD_OUT/AVAILABLE ani skasować pamięci wysłanego alertu.
    missing = [day for day in days if observed[day.isoformat()] == "MISSING"]
    for day in missing:
        key = day.isoformat()
        if key not in state["last_missing"]:
            log(f"⚠ {day:%d.%m.%Y}: API nie wystawia zwykłego biletu (MISSING); "
                "pozostawiam poprzedni stan i będę sprawdzać dalej.")
        state["last_missing"][key] = "MISSING"
    state["last_missing"] = {key: value for key, value in state["last_missing"].items()
                             if key in observed and observed[key] == "MISSING"}
    state["last_observed"] = {
        **{key: value for key, value in state["last_observed"].items()
           if key in observed and observed[key] == "MISSING"},
        **{key: value for key, value in observed.items() if value != "MISSING"},
    }
    state["last_notified"] = {
        day: status for day, status in state["last_notified"].items()
        if day in observed and observed[day] in (AVAILABLE_STATES | {"MISSING"})
    }
    for day in alerts:
        state["last_notified"][day.isoformat()] = observed[day.isoformat()]
    save_state(state)
    summary = ", ".join(f"{day:%d.%m}: {observed[day.isoformat()]}" for day in days)
    log("Statusy: " + summary)
    return 0, cfg["interval"]


def main():
    try:
        cfg = load_config()
    except ValueError as exc:
        log(f"✗ Błędna konfiguracja: {exc}")
        return 1
    log(f"Start Watykan Watch {VERSION}: {cfg['start_date']:%d.%m.%Y}–{cfg['end_date']:%d.%m.%Y}, "
        f"{cfg['visitors']} osób, co {cfg['interval']} s (Europe/Rome).")
    first = True
    while True:
        code, requested_delay = run_once(announce_startup=first)
        if code != 2:
            first = False
        if not active_dates(cfg):
            return 0
        delay = requested_delay if requested_delay is not None else cfg["interval"]
        # Mały jitter sprawia, że kolejne cykle nie tworzą idealnie okresowego ruchu.
        delay += random.uniform(0, min(5.0, delay * 0.1))
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
