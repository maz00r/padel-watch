#!/usr/bin/env python3
"""Monitor zmian w ofertach oficjalnej kasy Muzeów Watykańskich.

Korzysta wyłącznie z publicznego endpointu, nie loguje się i nie składa rezerwacji.
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
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

VERSION = "0.3.0"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(os.environ.get("STATE_DIR") or HERE, "vatican_state.json")
API_URL = "https://tickets.museivaticani.va/api/search/result"
HOME_URL = "https://tickets.museivaticani.va/home"
USER_AGENT = f"vatican-watch/{VERSION} (read-only offer change monitor)"
ROME = "Europe/Rome"
AVAILABLE_STATES = {"AVAILABLE", "LOW_AVAILABILITY"}
FAILURE_ALERT_AFTER = 3
DATE_GAP_SECONDS = 0.4
PAGE_GAP_SECONDS = 0.15
MAX_PAGES = 20
MAX_BACKOFF_SECONDS = 3600
MAX_CHANGES_IN_NOTIFICATION = 18

_TZ = None


class VaticanError(Exception):
    """Błąd odpowiedzi lub połączenia, który nie może zmienić zapisanego stanu."""

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
    start = parse_date(os.environ.get("START_DATE") or "2026-09-24", "start_date")
    end = parse_date(os.environ.get("END_DATE") or "2026-09-28", "end_date")
    if end < start:
        raise ValueError("end_date nie może być wcześniejszy niż start_date")
    try:
        interval = int(os.environ.get("CHECK_INTERVAL") or "60")
        visitors = int(os.environ.get("VISITORS") or "1")
    except ValueError as exc:
        raise ValueError("check_interval i visitors muszą być liczbami całkowitymi") from exc
    if not 30 <= interval <= 3600:
        raise ValueError("check_interval musi być w zakresie 30–3600 sekund")
    if not 1 <= visitors <= 20:
        raise ValueError("visitors musi być w zakresie 1–20")
    rome_tz()
    return {
        "start_date": start,
        "end_date": end,
        "interval": interval,
        "topic": (os.environ.get("NTFY_TOPIC") or "").strip(),
        "visitors": visitors,
        "ticket_types": parse_selection(os.environ.get("TICKET_TYPES"), ";", normalized),
        "ticket_statuses": parse_selection(os.environ.get("TICKET_STATUSES"), ",", lambda s: s.upper()),
    }


def active_dates(cfg, today=None):
    today = today or datetime.now(rome_tz()).date()
    first = max(cfg["start_date"], today)
    if first > cfg["end_date"]:
        return []
    return [first + timedelta(days=offset)
            for offset in range((cfg["end_date"] - first).days + 1)]


def clean_text(value):
    return " ".join(str(value or "").split())


def normalized(value):
    value = unicodedata.normalize("NFKD", clean_text(value))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value.casefold().replace("–", "-").replace("—", "-")


def parse_selection(value, separator, transform):
    """Puste pole oznacza wszystkie; usuwa powtórzenia i zbędne odstępy."""
    return sorted({transform(clean_text(part)) for part in (value or "").split(separator)
                   if clean_text(part)})


def selected_product(product, cfg):
    types = cfg["ticket_types"]
    return not types or any(part in normalized(product["name"]) for part in types)


def selected_change(change, cfg):
    statuses = cfg["ticket_statuses"]
    if not statuses:
        return True
    return any(item is not None and item["availability"] in statuses
               for item in (change.get("before"), change.get("after")))


def selection_key(cfg):
    return {"ticket_types": cfg["ticket_types"], "ticket_statuses": cfg["ticket_statuses"]}


def selection_description(cfg):
    if not cfg["ticket_types"] and not cfg["ticket_statuses"]:
        return "wszystkie oferty"
    types = "; ".join(cfg["ticket_types"]) or "wszystkie rodzaje"
    statuses = ", ".join(cfg["ticket_statuses"]) or "wszystkie statusy"
    return f"{types}; {statuses}"


def canonical_product(product):
    """Semantyczny zapis oferty; pomija losowe ID i nazwy obrazków."""
    if not isinstance(product, dict):
        raise VaticanError("API zawiera produkt inny niż obiekt")
    name = clean_text(product.get("name"))
    if not name:
        raise VaticanError("API zawiera produkt bez nazwy")
    who = product.get("who") or []
    if not isinstance(who, list):
        raise VaticanError(f"produkt '{name}' ma niepoprawne pole who")
    visitor_types = []
    for item in who:
        if not isinstance(item, dict):
            raise VaticanError(f"produkt '{name}' ma niepoprawny typ odwiedzającego")
        visitor_types.append(f"{clean_text(item.get('id'))}:{clean_text(item.get('description'))}")
    result = {
        "name": name,
        "availability": clean_text(product.get("availability")).upper() or "UNKNOWN",
        "description": clean_text(product.get("description")),
        "message": clean_text(product.get("descrAvailability")),
        "price": clean_text(product.get("priceFrom")),
        "suggestion": clean_text(product.get("suggestion")),
        "participants": clean_text(product.get("numberParticipants")),
        "visitor_types": sorted(visitor_types),
    }
    # ID zmienia się nawet dla tej samej oferty. Klucz zawiera wyłącznie treść
    # opisującą rodzaj produktu, nie jego bieżący stan.
    result["key"] = "\x1f".join((normalized(result["name"]),
                                    normalized(result["suggestion"]),
                                    normalized(result["description"])))
    return result


def snapshot_from_payload(payload):
    if not isinstance(payload, dict):
        raise VaticanError("API zwróciło JSON inny niż obiekt")
    visits = payload.get("visits")
    if not isinstance(visits, list):
        raise VaticanError("API nie zawiera listy visits")
    products = [canonical_product(item) for item in visits]
    keys = [item["key"] for item in products]
    if len(keys) != len(set(keys)):
        raise VaticanError("API zwróciło dwie nierozróżnialne oferty")
    return sorted(products, key=lambda item: item["key"])


def parse_retry_after(value, now=None):
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
        "User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip",
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


def search_url(day, visitors, page):
    return API_URL + "?" + urllib.parse.urlencode({
        "lang": "it", "visitorNum": str(visitors), "visitDate": day.strftime("%d/%m/%Y"),
        "area": "1", "who": "", "page": str(page),
    })


def result_url(day, visitors=1):
    local_midnight = datetime(day.year, day.month, day.day, tzinfo=rome_tz())
    epoch_ms = int(local_midnight.timestamp() * 1000)
    return f"{HOME_URL}/visit/{visitors}/{epoch_ms}/1/"


def fetch_snapshot(day, visitors=1):
    """Pobiera wszystkie strony wyników; brak paginacji ukrywałby część zmian."""
    all_products = []
    total_results = None
    for page in range(MAX_PAGES):
        payload = http_get_json(search_url(day, visitors, page))
        page_products = snapshot_from_payload(payload)
        if total_results is None:
            try:
                total_results = max(0, int(payload.get("totalResults", len(page_products))))
            except (TypeError, ValueError) as exc:
                raise VaticanError("API ma niepoprawne totalResults") from exc
        all_products.extend(page_products)
        if len(all_products) >= total_results:
            break
        if not page_products:
            raise VaticanError("API zakończyło strony przed totalResults")
        time.sleep(PAGE_GAP_SECONDS)
    else:
        raise VaticanError("API przekroczyło limit stron wyników")
    if total_results is not None and len(all_products) != total_results:
        raise VaticanError(f"API zwróciło {len(all_products)} z {total_results} ofert")
    keys = [item["key"] for item in all_products]
    if len(keys) != len(set(keys)):
        raise VaticanError("API powtórzyło ofertę na kilku stronach")
    return sorted(all_products, key=lambda item: item["key"])


def default_state():
    return {"snapshots": {}, "visitors": None, "selection": None,
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
    snapshots = loaded.get("snapshots")
    if isinstance(snapshots, dict):
        state["snapshots"] = {str(day): value for day, value in snapshots.items()
                              if isinstance(value, list)}
    try:
        state["visitors"] = int(loaded["visitors"]) if loaded.get("visitors") is not None else None
    except (TypeError, ValueError):
        log("⚠ Stan ma niepoprawną liczbę osób — zapiszę nowy punkt odniesienia.")
    if isinstance(loaded.get("selection"), dict):
        state["selection"] = loaded["selection"]
    if isinstance(loaded.get("failure"), dict):
        try:
            state["failure"]["count"] = max(0, int(loaded["failure"].get("count") or 0))
        except (TypeError, ValueError):
            log("⚠ Stan awarii ma niewłaściwy format — zeruję licznik błędów.")
        state["failure"]["alerted"] = bool(loaded["failure"].get("alerted"))
    return state


def save_state(state):
    directory = os.path.dirname(STATE_PATH) or "."
    tmp_path = None
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
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def ntfy_post(topic, title, message, click=None, priority="default", tags="ticket"):
    if not topic:
        log("Brak ntfy_topic — tryb bez powiadomień.")
        return True
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags,
               "Content-Type": "text/plain; charset=utf-8"}
    if click:
        headers["Click"] = click
    request = urllib.request.Request("https://ntfy.sh/" + urllib.parse.quote(topic, safe=""),
                                     data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20):
            return True
    except urllib.error.HTTPError as exc:
        log(f"⚠ ntfy HTTP {exc.code}; alert zostanie ponowiony.")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log(f"⚠ ntfy niedostępne ({exc}); alert zostanie ponowiony.")
    return False


TRACKED_FIELDS = ("availability", "message", "price", "participants", "visitor_types")
FIELD_LABELS = {"availability": "status", "message": "komunikat", "price": "cena od",
                "participants": "liczba uczestników", "visitor_types": "typy odwiedzających"}


def diff_snapshots(previous, current, day):
    old = {item["key"]: item for item in previous}
    new = {item["key"]: item for item in current}
    changes = []
    for key in sorted(new.keys() - old.keys()):
        changes.append({"kind": "added", "day": day, "after": new[key]})
    for key in sorted(old.keys() - new.keys()):
        changes.append({"kind": "removed", "day": day, "before": old[key]})
    for key in sorted(old.keys() & new.keys()):
        fields = [field for field in TRACKED_FIELDS if old[key].get(field) != new[key].get(field)]
        if fields:
            changes.append({"kind": "changed", "day": day, "before": old[key],
                            "after": new[key], "fields": fields})
    return changes


def short(value, limit=72):
    if isinstance(value, list):
        value = ", ".join(value)
    value = clean_text(value) or "—"
    return value if len(value) <= limit else value[:limit - 1] + "…"


def format_change(change):
    day = change["day"].strftime("%d.%m")
    if change["kind"] == "added":
        item = change["after"]
        return f"➕ {day} {short(item['name'], 58)} [{item['availability']}]"
    if change["kind"] == "removed":
        item = change["before"]
        return f"➖ {day} {short(item['name'], 58)} [{item['availability']}]"
    before, after = change["before"], change["after"]
    details = [f"{FIELD_LABELS[field]}: {short(before.get(field), 30)} → {short(after.get(field), 30)}"
               for field in change["fields"]]
    return f"↔ {day} {short(after['name'], 52)} — " + "; ".join(details)


def describe_changes(changes, visitors):
    shown = changes[:MAX_CHANGES_IN_NOTIFICATION]
    lines = [format_change(change) for change in shown]
    if len(changes) > len(shown):
        lines.append(f"… i {len(changes) - len(shown)} dalszych zmian (pełna lista w Dzienniku)")
    return (f"Zmiany w oficjalnej ofercie dla {visitors} "
            f"{'osoby' if visitors == 1 else 'osób'}:\n" + "\n".join(lines))


def changes_include_availability(changes):
    for change in changes:
        after = change.get("after") or {}
        if after.get("availability") in AVAILABLE_STATES:
            if change["kind"] == "added" or "availability" in change.get("fields", []):
                return True
    return False


def retry_delay(interval, failure_count, retry_after=None):
    exponential = min(MAX_BACKOFF_SECONDS, 30 * (2 ** min(failure_count, 7)))
    return min(MAX_BACKOFF_SECONDS, max(interval, exponential, retry_after or 0))


def record_failure(state, cfg, error):
    failure = state["failure"]
    failure["count"] += 1
    log(f"⚠ API Watykanu: {error}. Kolejny błąd z rzędu: {failure['count']}.")
    if failure["count"] >= FAILURE_ALERT_AFTER and not failure["alerted"]:
        sent = ntfy_post(cfg["topic"], "⚠ Watykan Watch: problem z API",
                         "Nie udało się sprawdzić oficjalnej oferty. "
                         "Poprzedni stan został zachowany; monitor ponowi próbę.",
                         priority="default", tags="warning")
        if sent:
            failure["alerted"] = True
    save_state(state)
    return retry_delay(cfg["interval"], failure["count"], getattr(error, "retry_after", None))


def run_once(announce_startup=False, today=None):
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
    if state["visitors"] not in (None, cfg["visitors"]) or (
            state["snapshots"] and state["selection"] != selection_key(cfg)):
        log("Zmieniła się liczba osób lub wybór biletów/statusów — zapisuję nowy punkt odniesienia bez fałszywego alertu.")
        state["snapshots"] = {}
    observed = {}
    try:
        for index, day in enumerate(days):
            observed[day.isoformat()] = [product for product in fetch_snapshot(day, cfg["visitors"])
                                         if selected_product(product, cfg)]
            if index + 1 < len(days):
                time.sleep(DATE_GAP_SECONDS)
    except VaticanError as exc:
        return 2, record_failure(state, cfg, exc)

    previous_failure = state["failure"]
    if previous_failure["alerted"]:
        if not ntfy_post(cfg["topic"], "✅ Watykan Watch: API znów działa",
                         "Oficjalne API znów odpowiada. Monitor kontynuuje obserwację zmian.",
                         priority="default", tags="white_check_mark"):
            return 2, cfg["interval"]
        log("API odzyskało działanie.")
    state["failure"] = {"count": 0, "alerted": False}

    if announce_startup:
        ntfy_post(cfg["topic"], "🎟️ Watykan Watch uruchomiony",
                  f"Monitoruję {selection_description(cfg)} dla {cfg['visitors']} "
                  f"{'osoby' if cfg['visitors'] == 1 else 'osób'}: "
                  f"{cfg['start_date']:%d.%m.%Y}–{cfg['end_date']:%d.%m.%Y}. "
                  f"Sprawdzanie co {cfg['interval']} s.", priority="default", tags="ticket")

    changes = []
    baseline_days = []
    for day in days:
        key = day.isoformat()
        if key not in state["snapshots"]:
            baseline_days.append(day)
            continue
        changes.extend(change for change in diff_snapshots(state["snapshots"][key], observed[key], day)
                       if selected_change(change, cfg))

    if changes:
        for change in changes:
            log(format_change(change))
        priority = "high" if changes_include_availability(changes) else "default"
        earliest = min(change["day"] for change in changes)
        if not ntfy_post(cfg["topic"], f"🎟️ Watykan: {len(changes)} zmian",
                         describe_changes(changes, cfg["visitors"]),
                         click=result_url(earliest, cfg["visitors"]),
                         priority=priority, tags="ticket"):
            return 2, cfg["interval"]

    state["snapshots"] = {day.isoformat(): observed[day.isoformat()] for day in days}
    state["visitors"] = cfg["visitors"]
    state["selection"] = selection_key(cfg)
    save_state(state)
    if baseline_days:
        log("Punkt odniesienia: " + ", ".join(
            f"{day:%d.%m} — {len(observed[day.isoformat()])} ofert" for day in baseline_days))
    elif not changes:
        log("Bez zmian: " + ", ".join(
            f"{day:%d.%m} — {len(observed[day.isoformat()])} ofert" for day in days))
    return 0, cfg["interval"]


def main():
    try:
        cfg = load_config()
    except ValueError as exc:
        log(f"✗ Błędna konfiguracja: {exc}")
        return 1
    log(f"Start Watykan Watch {VERSION}: {cfg['start_date']:%d.%m.%Y}–{cfg['end_date']:%d.%m.%Y}, "
        f"{selection_description(cfg)} dla {cfg['visitors']} osób, co {cfg['interval']} s (Europe/Rome).")
    first = True
    while True:
        code, requested_delay = run_once(announce_startup=first)
        if code != 2:
            first = False
        if not active_dates(cfg):
            return 0
        delay = requested_delay if requested_delay is not None else cfg["interval"]
        delay += random.uniform(0, min(5.0, delay * 0.1))
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
