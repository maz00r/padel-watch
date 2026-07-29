#!/usr/bin/env python3
"""
Monitor nowych seansów w Cinema City.

Pilnuje repertuaru wybranego filmu (link ze strony cinema-city.pl) i wysyła push
przez ntfy.sh, gdy pojawią się NOWE terminy — czy to nowy dzień w repertuarze,
czy dołożony seans w dniu, który już znamy.

Uruchomienie lokalnie:
    NTFY_TOPIC=twoj-temat FILM_URL='https://www.cinema-city.pl/filmy/...' python3 check_cinema.py

Tylko biblioteka standardowa — brak zależności.
"""

import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - w obrazie zawsze jest
    ZoneInfo = None

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(os.environ.get("STATE_DIR") or HERE, "cinema_state.json")

# Publiczne API repertuaru Cinema City (to samo, z którego korzysta strona).
API_BASE = "https://www.cinema-city.pl/pl/data-api-service/v1/quickbook/10103"
FILM_PAGE = "https://www.cinema-city.pl/filmy/{slug}/{film_id}"
UA = "cinema-watch/1.0 (+https://www.cinema-city.pl)"

PL_DAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
PL_DAYS_SHORT = ["pon", "wt", "śr", "czw", "pt", "sob", "niedz"]

_LOG_TZ = None


def _log_tz():
    global _LOG_TZ
    if _LOG_TZ is None:
        name = os.environ.get("TIMEZONE") or "Europe/Warsaw"
        try:
            _LOG_TZ = ZoneInfo(name) if ZoneInfo else timezone.utc
        except Exception:  # noqa: BLE001 - zła strefa nie może wywrócić logowania
            _LOG_TZ = timezone.utc
    return _LOG_TZ


def log(*args):
    ts = datetime.now(_log_tz()).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}]", *args, flush=True)


def plural(n, one, few, many):
    """Polski liczebnik: 1 / 2-4 / reszta, z wyjątkiem nastek (12-14 idą jak 'reszta')."""
    last, teens = n % 10, n % 100
    if n == 1:
        return one
    if 2 <= last <= 4 and not 12 <= teens <= 14:
        return few
    return many


def screenings(n):
    return f"{n} {plural(n, 'seans', 'seanse', 'seansów')}"


def day_count(n):
    return f"{n} {plural(n, 'dniu', 'dniach', 'dniach')}"


def fmt_day(date_str, short=False):
    """'2026-08-07' -> 'piątek 07.08' (albo 'pt 07.08')."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return date_str
    days = PL_DAYS_SHORT if short else PL_DAYS
    return f"{days[d.weekday()]} {d:%d.%m}"


# ------------------------------------------------------------------ konfiguracja


def parse_film_url(url):
    """Wyciąga z linku Cinema City (film_id, miejsce, slug).

    Obsługuje link, który użytkownik kopiuje z paska adresu, np.
    .../filmy/odyseja/7460s2r#/buy-tickets-by-film?in-cinema=warszawa&for-movie=7460s2r
    'miejsce' to grupa kin ('warszawa') albo id pojedynczego kina ('1074').
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("pusty film_url")
    # Fragment (#/...) niesie parametry wyboru kina — urlparse zostawia go w .fragment.
    parts = urllib.parse.urlparse(url)
    query = {}
    for chunk in (parts.query, parts.fragment):
        if "?" in chunk:
            chunk = chunk.split("?", 1)[1]
        query.update(urllib.parse.parse_qs(chunk))

    film_id = (query.get("for-movie") or [""])[0].strip()
    slug = ""
    path = [p for p in parts.path.split("/") if p]
    if "filmy" in path:
        i = path.index("filmy")
        slug = path[i + 1] if len(path) > i + 1 else ""
        if not film_id and len(path) > i + 2:
            film_id = path[i + 2]
    if not film_id:
        raise ValueError("nie znalazłem identyfikatora filmu w linku "
                         "(oczekuję .../filmy/<tytuł>/<id> albo ?for-movie=<id>)")
    place = (query.get("in-cinema") or [""])[0].strip()
    if not place:
        raise ValueError("nie znalazłem kina w linku (oczekuję ?in-cinema=<miasto lub id kina>) — "
                         "otwórz film na cinema-city.pl, wybierz miasto i skopiuj adres z paska")
    return film_id, place, slug


def load_config():
    """Konfiguracja z ENV (dodatek HA mapuje opcje na zmienne środowiskowe)."""
    film_id, place, slug = parse_film_url(os.environ.get("FILM_URL"))
    try:
        days_ahead = int(os.environ.get("DAYS_AHEAD") or 365)
    except ValueError:
        days_ahead = 365
    wanted = [c.strip().lower() for c in (os.environ.get("CINEMAS") or "").split(",") if c.strip()]
    return {
        "film_id": film_id,
        "place": place,
        "slug": slug,
        "is_group": not place.isdigit(),   # 'warszawa' = grupa kin, '1074' = jedno kino
        "days_ahead": max(1, min(days_ahead, 365)),
        "cinemas": wanted,
        "topic": os.environ.get("NTFY_TOPIC") or "",
        "film_page": FILM_PAGE.format(slug=slug or "film", film_id=film_id),
    }


# ---------------------------------------------------------------------- stan


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log("! Stan uszkodzony — traktuję jako pierwszy bieg.")
        return None
    return doc if isinstance(doc, dict) else None


def save_state(event_ids, dates):
    """Zapis atomowy — przerwany restart nie może zostawić połówki pliku."""
    doc = {"event_ids": sorted(event_ids), "dates": sorted(dates)}
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        log(f"! Nie zapisałem stanu {STATE_PATH}: {e!r}")


def apply_clear_state():
    if (os.environ.get("CLEAR_STATE") or "").strip().lower() != "all":
        return
    try:
        os.unlink(STATE_PATH)
        log("🧹 Wyczyszczono stan — najbliższy bieg zapisze nowy punkt odniesienia.")
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"! Nie udało się wyczyścić stanu: {e!r}")


# ----------------------------------------------------------------------- API


def http_get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "gzip"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def fetch_dates(cfg):
    """Dni, w których film gra (dla grupy) albo dni z repertuarem kina."""
    until = (datetime.now(_log_tz()) + timedelta(days=cfg["days_ahead"])).strftime("%Y-%m-%d")
    if cfg["is_group"]:
        url = f"{API_BASE}/dates/in-group/{cfg['place']}/with-film/{cfg['film_id']}/until/{until}?attr="
    else:
        url = f"{API_BASE}/dates/in-cinema/{cfg['place']}/until/{until}?attr="
    return (http_get_json(url).get("body") or {}).get("dates") or []


def fetch_events(cfg, date):
    """(seanse filmu w danym dniu, nazwy kin). Dla pojedynczego kina odsiewamy inne filmy."""
    if cfg["is_group"]:
        url = (f"{API_BASE}/cinema-events/in-group/{cfg['place']}"
               f"/with-film/{cfg['film_id']}/at-date/{date}?attr=")
    else:
        url = f"{API_BASE}/film-events/in-cinema/{cfg['place']}/at-date/{date}?attr="
    body = http_get_json(url).get("body") or {}
    names = {c.get("id"): c.get("displayName") or c.get("id")
             for c in (body.get("cinemas") or [])}
    events = [e for e in (body.get("events") or []) if e.get("filmId") == cfg["film_id"]]
    return events, names


def normalize_event(raw, cinema_names):
    when = str(raw.get("eventDateTime") or "")
    cinema = cinema_names.get(raw.get("cinemaId")) or str(raw.get("cinemaId") or "")
    # "Warszawa -  Arkadia" -> "Arkadia" (miasto wynika z wyboru użytkownika)
    short = re.sub(r"^\s*[^-]+-\s*", "", cinema).strip() or cinema
    return {
        "id": str(raw.get("id") or ""),
        "date": (raw.get("businessDay") or when[:10]),
        "time": when[11:16],
        "when": when,
        "cinema": short,
        "link": raw.get("bookingLink") or "",
    }


def collect_events(cfg):
    """(seanse, dni z repertuaru). Wyjątki sieciowe puszczamy w górę — bieg ma się nie liczyć."""
    dates = fetch_dates(cfg)
    events = []
    for date in dates:
        raw_events, names = fetch_events(cfg, date)
        for raw in raw_events:
            event = normalize_event(raw, names)
            if cfg["cinemas"] and not any(w in event["cinema"].lower() for w in cfg["cinemas"]):
                continue
            events.append(event)
    # Dni raportujemy z faktycznie znalezionych seansów, nie z listy z API: przy filtrze
    # kin (i w trybie pojedynczego kina, gdzie API zwraca cały repertuar) lista z API
    # zawiera dni bez interesującego nas filmu.
    return events, sorted({e["date"] for e in events})


# --------------------------------------------------------------- powiadomienia


def ntfy_post(topic, title, message, click=None, tags="clapper"):
    if not topic:
        log("! Brak NTFY_TOPIC — pomijam wysyłkę (tryb testowy).")
        return True
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "default",
        "Tags": tags,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{topic}",
                                 data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.HTTPError as e:
        log(f"! ntfy HTTP {e.code} dla tematu '{topic}' — sprawdź nazwę tematu.")
    except (urllib.error.URLError, TimeoutError) as e:
        log(f"! ntfy nieosiągalny ({e!r}) — ponowię w kolejnym biegu.")
    return False


def describe_new(new_events, new_dates, tz_now=None):
    """Treść powiadomienia: nowe dni zbiorczo, dołożone seanse pojedynczo."""
    lines = []
    by_date = {}
    for event in new_events:
        by_date.setdefault(event["date"], []).append(event)

    for date in sorted(d for d in by_date if d in new_dates):
        events = sorted(by_date[date], key=lambda e: e["when"])
        times = [e["time"] for e in events]
        lines.append(f"🆕 {fmt_day(date)} — {screenings(len(events))}, {times[0]}–{times[-1]}")

    extra = sorted((d for d in by_date if d not in new_dates))
    for date in extra:
        events = sorted(by_date[date], key=lambda e: e["when"])
        shown = ", ".join(f"{e['time']} {e['cinema']}" for e in events[:6])
        more = f" (+{len(events) - 6})" if len(events) > 6 else ""
        lines.append(f"➕ {fmt_day(date, short=True)}: {shown}{more}")
    return "\n".join(lines)


# ------------------------------------------------------------------ przebieg


def run_once(announce_startup=False):
    """0 = OK, 2 = błąd sieci (stan nietknięty, spróbujemy ponownie)."""
    try:
        cfg = load_config()
    except ValueError as e:
        log(f"✗ Błędna konfiguracja: {e}")
        return 1

    try:
        events, dates = collect_events(cfg)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError) as e:
        log(f"! Cinema City niedostępne ({e!r}) — stan bez zmian, ponawiam w kolejnym biegu.")
        return 2

    title = cfg["slug"].replace("-", " ").strip() or cfg["film_id"]
    if not events:
        # Puste API to NIE błąd HTTP — tak samo wygląda literówka w linku i film zdjęty
        # z ekranów. Bez tego ostrzeżenia cisza byłaby nie do odróżnienia od „nic nowego".
        log(f"⚠ Brak jakichkolwiek seansów dla filmu '{cfg['film_id']}' w '{cfg['place']}'. "
            f"Sprawdź film_url (zły identyfikator daje pustą odpowiedź, nie błąd) "
            f"albo film zszedł z afisza.")

    state = load_state()
    seen_ids = set((state or {}).get("event_ids") or [])
    seen_dates = set((state or {}).get("dates") or [])
    current_ids = {e["id"] for e in events}
    current_dates = set(dates)

    if state is None:
        latest = max((e["when"] for e in events), default="")
        log(f"Pierwszy bieg — zapisuję punkt odniesienia: {screenings(len(events))} "
            f"w {day_count(len(current_dates))}"
            + (f", ostatni {fmt_day(latest[:10])} {latest[11:16]}." if latest else "."))
        if announce_startup:
            ntfy_post(cfg["topic"], f"🎬 Monitoruję: {title}",
                      f"Teraz w repertuarze: {screenings(len(events))}, "
                      f"ostatni dzień {fmt_day(max(current_dates)) if current_dates else '—'}.\n"
                      f"Dam znać, gdy pojawią się nowe terminy.",
                      click=cfg["film_page"])
        save_state(current_ids, current_dates)
        return 0

    new_events = [e for e in events if e["id"] not in seen_ids]
    new_dates = current_dates - seen_dates
    if not new_events:
        log(f"Bez zmian: {screenings(len(events))} w {day_count(len(current_dates))}"
            + (f", ostatni {fmt_day(max(current_dates))}." if current_dates else "."))
        save_state(current_ids, current_dates)   # sprzątamy minione dni ze stanu
        return 0

    log(f"NOWE terminy: {screenings(len(new_events))}"
        + (f", w tym nowe dni: {', '.join(fmt_day(d, short=True) for d in sorted(new_dates))}"
           if new_dates else ""))
    body = describe_new(new_events, new_dates)
    for line in body.split("\n"):
        log(f"   {line}")

    sent = ntfy_post(cfg["topic"], f"🎬 {title}: nowe terminy", body, click=cfg["film_page"])
    if not sent:
        # Nie zapisujemy stanu — w kolejnym biegu te same seanse wykryjemy jako nowe
        # i ponowimy powiadomienie. Lepiej powtórzyć push niż zgubić go po cichu.
        return 2
    save_state(current_ids, current_dates)
    return 0


def main():
    try:
        interval = int(os.environ.get("CHECK_INTERVAL", "0"))
    except ValueError:
        interval = 0

    apply_clear_state()

    if interval <= 0:
        return run_once(announce_startup=True)

    log(f"Tryb pętli: sprawdzam co {interval}s. Ctrl+C aby zakończyć.")
    first = True
    while True:
        try:
            rc = run_once(announce_startup=first)
            if rc != 2:      # 2 = błąd sieci; powiadomienie startowe spróbuje jeszcze raz
                first = False
        except Exception as e:  # noqa: BLE001 - pętla ma przetrwać każdy błąd
            log(f"! Nieoczekiwany błąd w iteracji: {e!r} — kontynuuję.")
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
