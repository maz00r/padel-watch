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
import collections
import concurrent.futures
import http.client
import io
import json
import os
import re
import socket
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
# DZIENNIK POLOWAŃ — osobny plik, żeby najważniejsze liczby z sekundy publikacji
# nie ginęły w tysiącach linii Dziennika dodatku. Jeden wpis na dobę, widoczny
# w panelu (zakładka „Polowania"), plus push, gdy coś wymaga uwagi.
HUNTS_PATH = os.path.join(os.environ.get("STATE_DIR") or HERE, "hunts.json")
HUNTS_KEEP = 60   # ~2 miesiące historii; starsze i tak nikomu nie służą
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


def plural(n, one, few, many):
    """Polski liczebnik: 1 / 2-4 / reszta, z wyjątkiem nastek (12-14 idą jak 'reszta')."""
    last, teens = n % 10, n % 100
    if n == 1:
        return one
    if 2 <= last <= 4 and not 12 <= teens <= 14:
        return few
    return many


def fmt_when(dt, short=False):
    days = PL_DAYS_SHORT if short else PL_DAYS
    return f"{days[dt.weekday()]} {dt:%d.%m %H:%M}"


_LOG_TZ = None


def _log_tz():
    """Strefa czasowa znaczników w logach (TIMEZONE / Europe/Warsaw; fallback UTC)."""
    global _LOG_TZ
    if _LOG_TZ is None:
        # Strefa także z `config.json` — inaczej znaczniki czasu w logu chodziłyby
        # w innej strefie niż filtrowanie terminów. `quiet=True`, bo ta funkcja bywa
        # wywołana z wnętrza logowania i nie może wołać o pomoc do samej siebie.
        name = opcja("TIMEZONE", load_config(quiet=True), "timezone", "Europe/Warsaw")
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


# POZIOMY LOGOWANIA. Dzień pracy dodatku to ~2000 linii, z czego ~95% to powtarzalne
# „= Kort: N dostępnych" i „Brak nowych wolnych terminów" co dwie sekundy. Sygnał —
# publikacja, rejestracje, alarmy — tonie w tym szumie.
#
# Podział jest prosty i wynika z tego, co naprawdę chce się zobaczyć:
#   debug — rutynowe odpytywanie (każde pobranie, każdy pusty cykl, odczyt tokenu),
#   info  — zdarzenia: publikacja, rejestracje, dziennik polowań, zryw, powiadomienia,
#   warn  — coś poszło nie tak, ale polujemy dalej (409, ponowienie, rozjazd okna),
#   error — polowanie przerwane albo dane utracone.
POZIOMY = {"debug": 10, "info": 20, "warn": 30, "error": 40}

# Dodatek od początku znakuje wagę komunikatu pierwszym znakiem: „!" i „⚠" to kłopot,
# „✗" to nieudany strzał albo martwy token. Czytamy tę konwencję zamiast dopisywać
# level= w czterdziestu miejscach — i nowe linie same trafią na właściwą półkę.
WAGA_ZNAKU = {"✗": "error", "!": "warn", "⚠": "warn"}


def poziom_z_tresci(args):
    for a in args:
        s = str(a).strip()
        if s:
            return WAGA_ZNAKU.get(s[0], "info")
    return "info"


def _prog_logu():
    """Najniższy wypisywany poziom. Zła wartość NIE może uciszyć dodatku."""
    nazwa = (os.environ.get("LOG_LEVEL") or "info").strip().lower()
    return POZIOMY.get(nazwa, POZIOMY["info"])


def log(*args, level=None):
    waga = POZIOMY.get(level or poziom_z_tresci(args), POZIOMY["info"])
    if waga < _prog_logu():
        return
    now = datetime.now(_log_tz())
    ts = (now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if _LOG_MILLIS
          else now.strftime("%Y-%m-%d %H:%M:%S"))
    print(f"[{ts}]", *args, flush=True)


# --------------------------------------------------------------------------- IO

_config_warned = False


def listings_z_konfiguracji(cfg):
    """Lista kortów jako pojedynczy napis, niezależnie od źródła.

    `config.json` trzyma listę, ENV — napis rozdzielony przecinkami. Bez tej funkcji
    rozgrzewka i sprint czytały wyłącznie ENV i przy konfiguracji plikowej po cichu
    nie miały czego obserwować.
    """
    z_env = os.environ.get("LISTINGS")
    if z_env:
        return z_env
    z_pliku = (cfg or {}).get("listings") or []
    return ",".join(z_pliku) if isinstance(z_pliku, (list, tuple)) else str(z_pliku)


def opcja(env, cfg, klucz, domyslna=""):
    """Wartość opcji: ENV wygrywa nad `config.json`, potem wartość domyślna.

    Wydzielone, bo trzy opcje rozstrzygały się RÓŻNIE zależnie od tego, która funkcja
    je czytała, i były to ciche błędy — nic nie krzyczało, po prostu dwie części
    aplikacji działały na innych ustawieniach:

    - `TIMEZONE`  — `run_once` uwzględniał `config.json`, `main` i `_log_tz` nie.
      Skutek: znaczniki czasu i okno zrywu w innej strefie niż filtrowanie terminów.
    - `NTFY_TOPIC` — `run_once` uwzględniał, kontrola tokenu przed zrywem nie.
      Skutek: brak ostrzeżenia o martwym tokenie u kogoś, kto ustawił temat w pliku.
    - `LISTINGS`  — `run_once` uwzględniał, rozgrzewka i sprint nie.
      Skutek: sprint i zdalny strzał po cichu NIE BIORĄ UDZIAŁU w polowaniu.

    W dodatku HA `run.sh` eksportuje wszystko do ENV, więc te trzy były zamaskowane.
    Wychodziły dopiero przy uruchomieniu z samym `config.json`.
    """
    return os.environ.get(env) or (cfg or {}).get(klucz) or domyslna


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
    except (ValueError, OSError) as e:
        # USZKODZONY plik nie może zatrzymać polowania. Wcześniej leciał tu wyjątek —
        # a odkąd strefę czasową logu też bierzemy z konfiguracji, wywracałby KAŻDĄ
        # linię logu, czyli cały dodatek, przez jeden zabłąkany przecinek w JSON-ie.
        if not quiet and not _config_warned:
            log(f"! {CONFIG_PATH} jest uszkodzony ({e}) — używam wartości z ENV")
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


# ------------------------------------------------------------ dziennik polowań

def load_hunts():
    """Historia polowań, najnowsze pierwsze. Uszkodzony plik NIE może wywrócić biegu."""
    try:
        with open(HUNTS_PATH, encoding="utf-8") as f:
            dane = json.load(f)
        return dane if isinstance(dane, list) else []
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        log("! hunts.json uszkodzony — zaczynam historię od nowa")
        return []


def save_hunts(wpisy):
    """Zapis dziennika. Błąd zapisu jest głośny, ale nie przerywa polowania —
    historia jest cenna, a rezerwacja cenniejsza."""
    zapisz_json_atomowo(HUNTS_PATH, wpisy[:HUNTS_KEEP])


# KONTROLA SESJI PRZED POLOWANIEM. 27.08 wszystkie pięć strzałów padło w 0 ms
# z powodem „token" — sesja przeglądarki nie żyła, a dowiedzieliśmy się o tym PO
# publikacji, tracąc cztery wolne terminy (15, 17, 19, 20), w tym 20:00, o które
# walczymy od tygodni. Push o martwej sesji ma przychodzić ZANIM okno się otworzy,
# żeby był czas się zalogować.
PREFLIGHT_MIN_BEFORE = 30
_preflight_done_on = None    # data zamkniętej kontroli — jedna na dobę wystarczy
_preflight_problem_od = None  # odkąd kontrola widzi martwą sesję (karencja na ciche logowanie)
_preflight_alarm = False      # czy poszedł już push „zaloguj się"


def zryw_z_otoczenia():
    """Zryw odczytany z ENV — JEDNO miejsce, które to robi.

    Ta sama parę linii stała wcześniej w trzech miejscach (`burst_start_today`,
    `hunt_window`, `main`). Trzy kopie tego samego ograniczenia znaczą, że przy zmianie
    limitu trzeba pamiętać o wszystkich — a zapomniana kopia nie krzyczy, tylko cicho
    liczy inaczej niż pozostałe.

    Zwraca None, gdy zryw jest wyłączony ALBO zapisany błędnie: zły zryw nie może
    wywrócić polowania, kontroli tokenu ani dziennika.
    """
    surowy = (os.environ.get("BURST") or "").strip()
    if not surowy:
        return None
    try:
        burst = parse_burst_env(surowy)
        burst["seconds"] = max(1, min(int(os.environ.get("BURST_SECONDS") or 15),
                                      BURST_MAX_SECONDS))
        return burst
    except Exception:  # noqa: BLE001 - zły zryw nie może wywrócić niczego
        return None


def burst_start_today(now_local, tz):
    """Początek dzisiejszego okna zrywu albo None, gdy zryw wyłączony/błędny."""
    burst = zryw_z_otoczenia()
    if not burst:
        return None
    hour, minute, second = burst["at"]
    dzis = now_local.replace(hour=hour, minute=minute, second=second, microsecond=0)
    # DAY_NAMES, a nie strftime("%a") — ten drugi zależy od locale kontenera
    # i przy innym ustawieniu języka cicho przestałby pasować do dni zrywu.
    return dzis if DAY_NAMES[now_local.weekday()] in burst["days"] else None


def preflight_token(now_local, tz, topic, book_url):
    """Na X minut przed zrywem sprawdza, czy sesja żyje. Raz na dobę.

    Sprawdzenie jest DWUSTOPNIOWE, bo dwa różne uszkodzenia wyglądają tak samo
    dopiero przy strzale:
      1) lokalnie — czy w ogóle mamy token i czy nie wygasł (to złapało 27.08),
      2) na żywo — czy serwer go jeszcze akceptuje (sesja mogła zostać unieważniona,
         a token nadal ma ważny `exp`).

    To NIE jest powrót do rozgrzewki uwierzytelnionej z 0.9.0, która miała przyspieszać
    strzał i została obalona. Tu chodzi o jedno zapytanie na dobę, pół godziny przed
    oknem, wyłącznie po to, żeby zdążyć zareagować.
    """
    global _preflight_done_on, _preflight_problem_od, _preflight_alarm
    start = burst_start_today(now_local, tz)
    if start is None:
        return None
    try:
        ile_wczesniej = max(0, min(int(os.environ.get("TOKEN_CHECK_BEFORE")
                                       or PREFLIGHT_MIN_BEFORE), 240))
    except ValueError:
        ile_wczesniej = PREFLIGHT_MIN_BEFORE
    if not ile_wczesniej:
        return None
    moment = start - timedelta(minutes=ile_wczesniej)
    # Okno minutowe, a nie punkt: pętla budzi się co kilka sekund i nie trafi w sekundę.
    # Znaczniki są WAŻNE TYLKO DZIŚ. Bez tego nierozwiązany problem z wczoraj trzymałby
    # `czuwamy` w prawdzie i dziś rano odpytywalibyśmy API w KAŻDEJ iteracji aż do zrywu.
    if _preflight_problem_od is not None and _preflight_problem_od.date() != now_local.date():
        _preflight_problem_od, _preflight_alarm = None, False
    w_oknie = moment <= now_local < moment + timedelta(seconds=90)
    # Gdy sesja nie żyje, sprawdzamy DALEJ — aż do startu zrywu. Wcześniej kontrola
    # zamykała się po jednym spojrzeniu, więc sesja odzyskana przez ciche logowanie
    # minutę później nie miała jak zdjąć alarmu: użytkownik biegł do komputera na darmo.
    czuwamy = _preflight_problem_od is not None and now_local < start
    if not (w_oknie or czuwamy):
        return None
    if _preflight_done_on == now_local.date():
        return None

    cfg = load_config(quiet=True)
    if not boolish(os.environ.get("AUTO_REGISTER") or cfg.get("auto_register")):
        return None   # bez auto-rejestracji nie ma czego chronić

    reg_cfg = build_reg_cfg(cfg, load_state_doc())
    token, blad = ensure_decathlon_token(dict(reg_cfg))
    if not blad and token:
        try:
            decathlon_rpc("users.getMe", token, {})
        except urllib.error.HTTPError as e:
            blad = (f"serwer odrzucił token (HTTP {e.code})"
                    if e.code in (401, 403) else "")
            try:
                e.close()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 - awaria sieci to nie martwa sesja
            blad = ""
    if not blad:
        _preflight_done_on = now_local.date()
        if _preflight_alarm and topic:
            # Alarm już poszedł, a sesja wróciła — najczęściej sama, cichym logowaniem.
            # Bez tej wiadomości użytkownik jedzie do komputera po nic.
            ntfy_post(topic, "✅ Sesja wróciła — polowanie ma czym strzelać",
                      f"Token znów działa. Nic nie musisz robić przed {start:%H:%M}.",
                      click=book_url, priority="default", tags="white_check_mark")
        log(f"🔑 Sesja Decathlon sprawdzona — polowanie o {start:%H:%M:%S} ma czym strzelać."
            + (" Alarm odwołany." if _preflight_alarm else ""))
        _preflight_problem_od, _preflight_alarm = None, False
        return True

    if _preflight_problem_od is None:
        # KARENCJA: ciche logowanie żyje w drugim procesie i po nieudanym odczycie
        # ponawia dopiero po 45 s. Push wysłany teraz trafiłby do użytkownika, zanim
        # aplikacja w ogóle spróbowała się naprawić — i najczęściej byłby fałszywy.
        _preflight_problem_od = now_local
        log(f"⚠ Sesja nie odpowiada ({blad}) — daję cichemu logowaniu "
            f"{AUTH_ALERT_GRACE}s, zanim zaalarmuję. Do zrywu {start:%H:%M:%S}.")
        return False

    if not _preflight_alarm and (now_local - _preflight_problem_od).total_seconds() >= AUTH_ALERT_GRACE:
        _preflight_alarm = True
        log(f"⚠ SESJA NIE ŻYJE ({blad}). Polowanie o {start:%H:%M:%S} nie uda się bez "
            f"zalogowania w panelu Padel.")
        if topic:
            ntfy_post(topic, "⚠️ Zaloguj się — polowanie za %d min" % ile_wczesniej,
                      f"{blad}\n\nOtwórz panel Padel i zaloguj się na go.decathlon.pl.\n"
                      f"Bez tego rejestracja o {start:%H:%M} nie wyśle ani jednego żądania.",
                      click=book_url, priority="urgent", tags="rotating_light")
    return False


def hunt_window(now_local, tz):
    """(czy publikacja trafiła w zryw, opis okna). (None, '') gdy zryw wyłączony.

    To jest sedno alertu: 23.08 publikacja przyszła o 11:00:15, a zryw startował
    o 11:00:30 — spóźniliśmy się na własne przyjęcie i pięć terminów zniknęło,
    zanim w ogóle spojrzeliśmy. Bez tego sprawdzenia takie przesunięcie wygląda
    po prostu jak seria gorszych dni.
    """
    burst = zryw_z_otoczenia()
    if not burst:
        return None, ""
    granice = burst_bounds(burst, tz, now_local)
    if not granice:
        return None, ""
    opis = f"{granice[0]:%H:%M:%S}–{granice[1]:%H:%M:%S}"
    return granice[0] <= now_local < granice[1], opis


def zapisz_json_atomowo(sciezka, dane):
    """Zapis przez plik tymczasowy i `os.replace` — czytelnik nigdy nie widzi połówki.

    `read_token.py` robił tak od początku dla tokenu, ale stan i dziennik zapisywały się
    w miejscu. Ubicie dodatku w trakcie zapisu (restart HA, zatrzymanie kontenera, zanik
    zasilania) zostawiało ucięty `state.json`, a to znaczy:

    - punkt odniesienia zerowany → KAŻDY termin wygląda na nowy → lawina powiadomień,
    - `registered_ids` przepadają → możemy strzelić w termin, który już mamy.

    `os.replace` jest atomowe w obrębie jednego systemu plików, a `/data` w dodatku HA
    jest jednym wolumenem — więc plik tymczasowy trzymamy obok celu, nie w /tmp.
    """
    tmp = f"{sciezka}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dane, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, sciezka)
        return True
    except OSError as e:
        log(f"! Nie zapisałem {sciezka}: {e!r}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def write_state_doc(doc):
    zapisz_json_atomowo(STATE_PATH, doc)


def save_state(free_ids, registered_ids=None, decathlon_jwt=None, pending_ids=None,
               auth_alert_sent=None, decathlon_rt=None, auth_error_since="",
               startup_push_at=""):
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
    if startup_push_at:
        doc["startup_push_at"] = startup_push_at   # dławik powiadomienia startowego
    if auth_error_since:
        # Odkąd token nie działa. Bez tego karencja liczyłaby się od nowa przy KAŻDEJ
        # iteracji i powiadomienie nie przyszłoby nigdy.
        doc["auth_error_since"] = auth_error_since
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


def http_get_json(url, timeout=60):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Accept": "application/json"}
    )
    with open_url(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def fetch_listing_light(listing_id):
    """Lekki ping (~1 KB): kort + datesStats.availableListingDates."""
    return http_get_json(LISTING_URL.format(id=listing_id))


def fetch_listing(listing_id, timeout=60):
    """Ciężki payload (~257 KB): kort + wszystkie terminy w included[]."""
    return http_get_json(LISTING_DATES_URL.format(id=listing_id), timeout=timeout)


# ------------------------------------------------------------------ core logic

def parse_slots(doc, listing_id, now_utc, only_free=True):
    """Terminy z dokumentu API. `only_free=False` zachowuje także ZAJĘTE.

    Zajęte są potrzebne, żeby dało się policzyć CAŁY grafik dnia. Bez nich log widział
    wyłącznie to, co jeszcze wolne, i nie sposób było odpowiedzieć na pytanie „czy ktoś
    zdążył przed nami" — brakujące godziny wyglądały tak samo jak nigdy niewystawione.
    """
    out = []
    pominiete = 0
    for item in doc.get("included", []):
        if item.get("type") != "listing-date":
            continue
        a = item.get("attributes", {})
        if a.get("cancelled"):
            continue
        try:
            limit = a.get("participantsLimit")
            if limit is None:  # bez limitu miejsc — pomijamy (nie da się ocenić)
                continue
            limit = int(limit)
            count = int(a.get("participantsCount") or 0)
            start = parse_dt(a.get("date"))
            reg_end = parse_dt(a.get("registrationEndDate"))
        except (TypeError, ValueError):
            # JEDEN uszkodzony rekord nie może zabić polowania. Wcześniej zła data albo
            # liczba w postaci napisu leciała wyjątkiem przez `free_slots` aż na wierzch:
            # lokalnie kosztowało to iterację, ale w Lambdzie NIE JEST ŁAPANE — całe
            # wywołanie padało i publikacja przechodziła bokiem.
            pominiete += 1
            continue
        if only_free and count >= limit:
            continue
        if start is None or start <= now_utc:
            continue
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
    if pominiete:
        # Głośno, bo to znaczy, że API zmieniło kształt danych i część grafiku jest
        # dla nas niewidzialna — a niewidzialny termin wygląda dokładnie jak
        # nieistniejący. Cisza zamieniłaby awarię integracji w „dziś nic nie było".
        log(f"! Pominąłem {pominiete} "
            f"{plural(pominiete, 'termin', 'terminy', 'terminów')} o nieczytelnych "
            f"danych (data albo liczba miejsc w nieznanym formacie) — sprawdź, "
            f"czy API Decathlona się nie zmieniło.")
    return out


def free_slots(doc, listing_id, now_utc):
    """Wolne terminy (przyszłe, niezarezerwowane)."""
    return parse_slots(doc, listing_id, now_utc, only_free=True)


def day_grid(doc, listing_id, now_utc, day_local, tz, held_ids=()):
    """(wolne, wszystkie, [godziny już zajęte]) w grafiku danego dnia lokalnego.

    W momencie publikacji różnica między tymi liczbami to terminy zajęte, ZANIM
    zdążyliśmy zobaczyć grafik — jedyny sposób, żeby to w ogóle zmierzyć. Bez tego
    „nie było takiej godziny" i „ktoś ją zabrał przed nami" wyglądają identycznie.

    Same liczby jednak nie wystarczą. Żeby odpowiedzieć na pytanie „czy 20:00 w ogóle
    było do wzięcia", trzeba wiedzieć, KTÓRE godziny są zajęte. Godzina, której nigdy
    nie zobaczyliśmy jako wolnej, to zupełnie inny problem niż przegrany wyścig —
    tamtej nie da się wygrać żadną prędkością ani żadnym regionem.
    """
    wszystkie = [s for s in parse_slots(doc, listing_id, now_utc, only_free=False)
                 if s["start_utc"].astimezone(tz).date() == day_local]
    # NASZE rezerwacje też są „zajęte" — ale nie przez konkurencję. Bez tego wyjątku
    # termin zdobyty wczoraj wracał następnego dnia jako „zniknął przed nami"
    # (26.08: 01.09 18:00 było nasze od 25.08, a raport pokazywał je jako stracone).
    zajete = sorted(f"{s['start_utc'].astimezone(tz):%H:%M}"
                    for s in wszystkie
                    if s["count"] >= s["limit"] and s["id"] not in set(held_ids))
    return len(wszystkie) - sum(1 for s in wszystkie if s["count"] >= s["limit"]), \
        len(wszystkie), zajete


# Ile dni naprzód sięga publikacja. Odwołanie dotyczy dnia BLISKIEGO (dziś, jutro),
# publikacja — horyzontu +7. Bez tego rozróżnienia dziennik bierze pierwsze nowe
# terminy dnia za publikację: 28.08 o 08:57 ktoś zwolnił termin na TEN SAM dzień,
# a dodatek ogłosił „publikacja o 08:57 poza zrywem" i wysłał fałszywy alarm.
# Prawdziwa publikacja przyszła tego dnia o 11:00:36.
PUBLIKACJA_MIN_DNI = 6


# Ile czasu po pierwszym wykryciu jeszcze wierzymy, że „zajęte" znaczy „ktoś nas ubiegł".
# Publikacja sypie partiami przez ~sekundę, a kolejne przebiegi monitora dzieli kilka
# sekund — dwie minuty to zapas z naddatkiem, a jednocześnie nic, co można pomylić
# ze zwykłym ruchem w ciągu dnia.
OKNO_PUBLIKACJI_S = 120


def okno_publikacji(wpis, now_local):
    """Czy wciąż jesteśmy w chwili publikacji? Bez znacznika — zakładamy, że tak."""
    zaczeto = wpis.get("first_seen_iso")
    if not zaczeto:
        return True
    try:
        return (now_local - datetime.fromisoformat(zaczeto)).total_seconds() <= OKNO_PUBLIKACJI_S
    except ValueError:
        return True


def record_hunt(now_local, tz, new_slots, wyniki, shots, grid, zdalnie, topic,
                wykryto=None):
    """Dopisuje polowanie do dziennika i alarmuje, gdy coś wymaga uwagi.

    JEDEN wpis na dobę — publikacja przychodzi partiami, więc kolejne wykrycia tego
    samego dnia dokładają się do istniejącego wpisu zamiast tworzyć nowy.

    Po to jest ten plik: najważniejsze liczby z sekundy publikacji giną w tysiącach
    linii Dziennika, a użytkownik nie ma ich codziennie przeglądać. Wpis odkłada się
    sam, widać go w panelu, a push przychodzi TYLKO wtedy, gdy jest o czym mówić.
    """
    dzis = now_local.date().isoformat()
    wpisy = load_hunts()
    wpis = wpisy[0] if wpisy and wpisy[0].get("date") == dzis else None
    # Publikacja czy odwołanie? Publikacja dotyczy horyzontu +7, odwołanie — dnia
    # bliskiego. Mylenie ich dawało fałszywy alarm „publikacja poza zrywem" (28.08).
    horyzont = grid[4] if grid and len(grid) > 4 else None
    to_publikacja = bool(horyzont) and (horyzont - now_local.date()).days >= PUBLIKACJA_MIN_DNI
    if wpis is None:
        wpis = {"date": dzis, "alerted": False, "remote": bool(zdalnie),
                "registered": [], "failed": [], "shots": [], "free": 0, "total": 0,
                "first_seen": None, "first_seen_iso": None, "burst": "", "aligned": None}
        wpisy.insert(0, wpis)
    if to_publikacja and not wpis.get("first_seen"):
        # Godzinę publikacji i ocenę okna zapisujemy TYLKO przy prawdziwej publikacji.
        # `wykryto` to chwila, w której zobaczyła ją IRLANDIA — bez tego zapisywalibyśmy
        # moment POWROTU wyniku. Odkąd wywołanie zostaje do końca okna (0.20.0), różnica
        # sięga kilkudziesięciu sekund: 04.09 wpis mówił 11:00:48 przy publikacji
        # o 11:00:29, a ja na tej podstawie doradzałem przesunięcie okna.
        chwila = wykryto.astimezone(tz) if wykryto else now_local
        w_oknie, okno = hunt_window(chwila, tz)
        wpis["first_seen"] = f"{chwila:%H:%M:%S}"
        wpis["first_seen_iso"] = chwila.isoformat()
        wpis["burst"], wpis["aligned"] = okno, w_oknie

    if grid:
        wpis["target_day"], wpis["free"], wpis["total"] = grid[:3]
        # Zajęte godziny zbieramy TYLKO przez chwilę po pierwszym wykryciu.
        # Publikacja przychodzi partiami przez ~sekundę, więc jedna migawka nie
        # pokazuje całego grafiku — ale godzina zarezerwowana przez kogoś sześć godzin
        # PÓŹNIEJ to zwykły ruch, a nie „zniknęła przed naszym pierwszym spojrzeniem".
        # 28.08 sumowanie przez całą dobę dało 8 godzin „nigdy nie widzianych" przy
        # 8 wolnych z 11 — liczby, które nie mogą być jednocześnie prawdziwe.
        if okno_publikacji(wpis, now_local):
            wpis["taken"] = sorted(set(wpis.get("taken") or [])
                                   | set(grid[3] if len(grid) > 3 else []))
    wpis["remote"] = wpis["remote"] or bool(zdalnie)
    for s in sorted(new_slots, key=lambda x: x["start_utc"]):
        kiedy = fmt_when(s["start_utc"].astimezone(tz), short=True)
        ok, msg = wyniki.get(s["id"], (None, ""))
        if ok:
            wpis["registered"].append(kiedy)
        elif ok is False:
            wpis["failed"].append({"when": kiedy, "why": skroc_powod(msg)})
    wpis["shots"].extend(shots or [])

    # Przegrana ZDALNEGO strzału nie trafiała tu wcale. `failed` powstaje z `new_slots`,
    # czyli z terminów, które strona lokalna NADAL widzi jako wolne — a termin przegrany
    # w Irlandii jest już zajęty, gdy dokument wraca do domu. 02.09 dało to wpis, który
    # twierdził „nigdy nie pokazane jako wolne: 17:00", choć strzelaliśmy w 17:00 DWA razy.
    wygrane_strzalem = {s["when"] for s in wpis["shots"] if s.get("ok")}
    znane = ({f["when"] for f in wpis["failed"]} | set(wpis["registered"])
             | wygrane_strzalem)
    for s in wpis["shots"]:
        if s.get("ok") or s["when"] in znane:
            continue
        wpis["failed"].append({"when": s["when"], "why": s.get("why") or "zajęty (409)"})
        znane.add(s["when"])

    # SEDNO diagnostyki: godzina zajęta, w którą NIE strzelaliśmy i której NIE zdobyliśmy,
    # to godzina, której nigdy nie zobaczyliśmy jako wolnej. Takiej nie da się wygrać
    # żadną prędkością — i to zupełnie inny problem niż przegrany wyścig (`failed`).
    # Oddany strzał jest dowodem, że godzinę widzieliśmy — nawet jeśli zniknęła zaraz potem.
    przegrane = {f["when"].split()[-1] for f in wpis["failed"]}
    nasze = {w.split()[-1] for w in wpis["registered"]}
    strzelane = {s["when"].split()[-1] for s in wpis["shots"]}
    wpis["never_seen"] = sorted(set(wpis.get("taken") or [])
                                - przegrane - nasze - strzelane)

    powod = hunt_alert_reason(wpis)
    if powod and not wpis["alerted"]:
        wpis["alerted"] = True
        log(f"⚠ {powod}")
        # Push leci tu bez kolejki celowo: alert o rozjeździe z oknem może powstać
        # WYŁĄCZNIE poza zrywem (w zrywie z definicji jesteśmy zsynchronizowani),
        # więc nie ma czego odkładać.
        notify_hunt(topic, wpis, powod)
    save_hunts(wpisy)
    return wpis


def skroc_powod(msg):
    """Powód porażki w jednym słowie — dziennik ma być czytelny, nie kompletny."""
    # KOLEJNOŚĆ MA ZNACZENIE: własny dublet też niesie „409", więc musi być sprawdzony
    # PRZED ogólnym warunkiem. Inaczej dzień, w którym termin jest nasz, trafiałby do
    # dziennika jako przegrany wyścig — i odpalał alarm „żadna rezerwacja się nie udała".
    if WLASNA_REZERWACJA in msg:
        return POWOD_JUZ_NASZE
    if "No available seats" in msg or "409" in msg:
        return "zajęty (409)"
    for marker in AUTH_FAILURE_MARKERS:
        if marker in msg:
            return "token"
    return (msg or "nieznany")[:60]


def hunt_alert_reason(wpis):
    """Czy ten dzień wymaga uwagi? Pusty napis = wszystko w normie.

    Alarmujemy oszczędnie. Push, który przychodzi codziennie, przestaje być
    czytany — a wtedy nie ma go po co wysyłać.
    """
    if wpis.get("aligned") is False and wpis.get("first_seen"):
        return (f"Publikacja o {wpis['first_seen']} wypadła POZA zrywem "
                f"({wpis['burst']}) — przesuń okna w konfiguracji dodatku.")
    # „Miejsce już nasze" NIE jest porażką — to odbita kopia strzału redundantnego albo
    # ponowienie na termin, który już mamy. Alarm ma krzyczeć o przegranych wyścigach.
    realne = [f for f in wpis["failed"] if f.get("why") != POWOD_JUZ_NASZE]
    if not wpis["registered"] and realne:
        return (f"Żadna rezerwacja się nie udała ({len(realne)} prób) — "
                f"sprawdź Dziennik.")
    return ""


def notify_hunt(topic, wpis, powod):
    """Push o polowaniu wymagającym uwagi — raz na dobę, nie częściej."""
    if not topic:
        log("! Brak NTFY_TOPIC — pomijam alert o polowaniu (tryb testowy).")
        return None
    zdobyte = ", ".join(wpis["registered"]) or "nic"
    return ntfy_post(
        topic,
        "⚠️ Polowanie wymaga uwagi",
        f"{powod}\n\nZarezerwowano: {zdobyte}\n"
        f"Grafik: {wpis.get('free', 0)} wolnych z {wpis.get('total', 0)}",
        priority="high",
        tags="warning",
    )


def log_day_grids(new_slots, docs_by_lid, now_utc, tz, held_ids=()):
    """Loguje, ile terminów danego dnia jest wolnych, a ile liczy CAŁY grafik.

    Zwraca statystykę dla dnia NAJDALSZEGO w przyszłość — bo to on jest świeżo
    opublikowanym horyzontem. Nowe terminy potrafią przyjść z dwóch dni naraz:
    publikacja dotyczy dnia +7, a odwołania dotyczą dni bliższych. 26.08 właśnie
    tak było (01.09 z odwołań, 02.09 z publikacji) i raport opisał zły dzień.

    Tylko dla dnia publikacji ma sens zdanie „zanim zobaczyliśmy grafik" — w dniach
    wcześniejszych zajęte godziny to zwykły ruch z ostatniej doby, nie wyścig.
    """
    dni = {(s["listing_id"], s["start_utc"].astimezone(tz).date()) for s in new_slots}
    posortowane = sorted(dni, key=lambda k: (k[1], k[0]))
    horyzont = posortowane[-1] if posortowane else None
    wynik = None
    for lid, dzien in posortowane:
        doc = docs_by_lid.get(lid)
        if doc is None:
            continue
        wolne, wszystkie, zajete_godziny = day_grid(doc, lid, now_utc, dzien, tz, held_ids)
        etykieta = f"{PL_DAYS_SHORT[dzien.weekday()]} {dzien:%d.%m}"
        publikacja = (lid, dzien) == horyzont
        if publikacja:
            wynik = (etykieta, wolne, wszystkie, zajete_godziny, dzien)
        ile = len(zajete_godziny)
        log(f"📋 Grafik na {etykieta}: "
            f"{wolne} {plural(wolne, 'wolny', 'wolne', 'wolnych')} z {wszystkie}"
            + (f" — {ile} {plural(ile, 'zajęty', 'zajęte', 'zajętych')} "
               f"({', '.join(zajete_godziny)})"
               + (", zanim zobaczyliśmy grafik" if publikacja else " przez innych")
               if ile else ""))
    return wynik


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


def plan_sleep(default_s, windows, burst, tz, elapsed=0.0, now=None, sprint=None):
    """Ile spać przed kolejnym sprawdzeniem.

    Cztery zasady:
      1. w trwającym zrywie obowiązuje jego własny, gęsty takt,
      2. tuż przed zrywem śpimy DOKŁADNIE do jego początku, żeby go nie przespać,
      3. TO SAMO dotyczy sprintu — bez tego dawało się go przespać W CAŁOŚCI: przy
         domyślnej konfiguracji (sprint 11:00:05–11:00:45, zryw dopiero 11:00:45) pętla
         budziła się o 10:59:50, liczyła sen do startu zrywu i spała 55 s. Irlandia nie
         była wywoływana ani razu, a w logu nie było śladu, bo z punktu widzenia pętli
         wszystko poszło zgodnie z planem,
      4. od reszty odejmujemy czas pracy — inaczej ustawione 2 s dają realnie ~2,4 s,
         bo do każdego cyklu doklejał się czas zapytań.
    """
    now = now or datetime.now(tz)
    elapsed = max(0.0, elapsed)
    bounds = burst_bounds(burst, tz, now)
    if bounds and bounds[0] <= now < bounds[1]:
        return max(MIN_SLEEP_SECONDS, burst["interval"] - elapsed)
    target = current_interval(default_s, windows, tz, now.astimezone(timezone.utc)) - elapsed
    # Do startu okna liczymy od TERAZ — czas pracy już upłynął, więc drugi raz go nie
    # odejmujemy; inaczej budzilibyśmy się ułamek sekundy za wcześnie i pierwsze
    # sprawdzenie wypadałoby obok celu.
    for granice in (bounds, burst_bounds(sprint, tz, now) if sprint else None):
        if granice and now < granice[0]:
            target = min(target, (granice[0] - now).total_seconds())
    return max(MIN_SLEEP_SECONDS, target)


def fmt_price(price, listing_default):
    """Cena do wyświetlenia. Nierozpoznany kształt opisujemy wprost, nie jako „za darmo"."""
    p = price if price is not None else listing_default
    kwota = price_amount({"price": p}, None)
    if kwota == CENA_NIEZNANA:
        return "cena nieznana"
    if kwota == 0:
        return "za darmo"
    waluta = p.get("currency", "") if isinstance(p, dict) else ""
    return f"{kwota / 100:.2f} {waluta}".strip()


def boolish(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on", "tak")


# Kwota, której NIE UMIEMY odczytać. Nie zero — bo zero znaczy „za darmo" i przy
# nierozpoznanym kształcie ceny puszczałoby płatny termin jako darmowy.
CENA_NIEZNANA = -1


def price_amount(slot, listing_default):
    """Kwota w groszach, 0 dla darmowego, `CENA_NIEZNANA` gdy kształt jest nieznany.

    `free_only` jest JEDYNĄ rzeczą stojącą między użytkownikiem a płaceniem za kort,
    a poprzednia wersja (`p.get("amount") or 0`) zawodziła OTWARCIE: cena
    `{"value": 3900}` albo `{"gross": 3900}` dawała 0, czyli „za darmo", i termin
    zostałby zarezerwowany. Cena jako napis (`{"amount": "3900"}`) wywracała z kolei
    porównanie `> 0` wyjątkiem TypeError.

    Zasada: czego nie rozumiemy, uznajemy za PŁATNE. Przegapiony darmowy termin kosztuje
    jedno polowanie; nieoczekiwana płatna rezerwacja kosztuje pieniądze i trzeba ją
    ręcznie odkręcić.
    """
    p = slot.get("price")
    if p is None:
        p = listing_default
    if p is None:
        return 0                       # brak ceny w API = kort darmowy
    if not isinstance(p, dict):
        return CENA_NIEZNANA           # napis, liczba, cokolwiek innego
    if "amount" not in p:
        return CENA_NIEZNANA           # inny schemat niż znany nam
    kwota = p["amount"]
    if kwota is None:
        return 0
    if isinstance(kwota, bool):        # True/False to nie jest kwota
        return CENA_NIEZNANA
    try:
        return int(float(kwota))       # akceptujemy też „3900" i 39.0
    except (TypeError, ValueError):
        return CENA_NIEZNANA


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


def resolve_decathlon_token(cfg, state_doc):
    """Token z trzech źródeł: przeglądarka (plik), opcje dodatku, zapamiętany stan.

    Wygrywa ten o NAJDALSZYM exp — nie kolejność. Dzięki temu ręcznie wklejony świeży
    token działa nawet, gdy plik z przeglądarki trzyma stary (np. sesja padła), i odwrotnie.
    """
    return newer_decathlon_token(
        newer_decathlon_token(
            token_from_file(),
            os.environ.get("DECATHLON_TOKEN") or cfg.get("decathlon_token") or "",
        ),
        (state_doc or {}).get("decathlon_jwt") or "",
    )


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


def check_decathlon_credentials(cfg, topic=None, book_url=None):   # noqa: ARG001
    # `topic`/`book_url` zostają w sygnaturze dla zgodności wywołań, ale ta funkcja
    # świadomie NIE wysyła już powiadomień. Robiła to obok `run_once`, który zaraz
    # potem wysyłał drugie z tego samego powodu — przy martwym tokenie dostawało się
    # więc dwa identyczne pushe. Jeden punkt powiadamiania jest w `run_once`.
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
# Ile czekamy z powiadomieniem o problemie z tokenem. Czytnik ponawia odczyt po 45 s,
# a ciche logowanie (kliknięcie „ZALOGUJ SIĘ" i odbicie przez OAuth) trwa do ~20 s.
# Krótsza karencja znaczy push wysłany, zanim aplikacja spróbowała się naprawić.
AUTH_ALERT_GRACE = 120


def _starszy_niz(iso, sekund):
    """True, gdy znacznik ISO jest starszy niż `sekund`. Zepsuty znacznik = tak."""
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(iso)).total_seconds() >= sekund
    except (TypeError, ValueError):
        return True


TOKEN_WAIT_ATTEMPTS = 8
TOKEN_WAIT_DELAY = 3


def wait_for_fresher_token(token, attempts=TOKEN_WAIT_ATTEMPTS, delay=TOKEN_WAIT_DELAY):
    """Czeka, aż przeglądarka zapisze token INNY niż podany. Zwraca '' gdy się nie doczekał.

    Bez pliku tokenu nie ma na co czekać — wracamy NATYCHMIAST. Inaczej czekalibyśmy
    pełne ~24 s na przeglądarkę, której w danym środowisku w ogóle nie ma (np. w Lambdzie
    w Irlandii), i to dokładnie w sekundzie publikacji.
    """
    if not TOKEN_FILE:
        return ""
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
    kwota = price_amount(slot, listing_price)
    if kwota != 0 and cfg.get("free_only", True):
        # `!= 0`, nie `> 0`: obejmuje także `CENA_NIEZNANA`. Nierozpoznana cena to
        # powód, żeby NIE rezerwować, a nie żeby zgadywać.
        if kwota == CENA_NIEZNANA:
            return False, (f"nie rozpoznaję ceny terminu ({slot.get('price') or listing_price!r}) "
                           f"— nie rezerwuję, żeby nie zapłacić przez pomyłkę")
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


def reservations_ics(reservations, calname="Padel", alarm_minutes=60, method="PUBLISH"):
    """Kalendarz iCalendar z rezerwacjami — telefon dodaje go jednym dotknięciem.

    Anulowane rezerwacje NIE są pomijane: dostają `STATUS:CANCELLED` i `SEQUENCE:1`.
    Kalendarz dopasowuje wydarzenie po `UID` i przyjmuje zmianę tylko wtedy, gdy
    `SEQUENCE` jest wyższy niż zapamiętany — bez tego plik zostałby zignorowany,
    a odwołany termin wisiałby w kalendarzu w nieskończoność.

    `method="CANCEL"` daje plik czysto odwołujący (iTIP) — to najbardziej
    jednoznaczny sygnał dla kalendarza, że wydarzenie ma zniknąć.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//padel-watch//Decathlon GO//PL",
        "CALSCALE:GREGORIAN", f"METHOD:{method}",
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
            # Numer wersji wydarzenia: odwołanie musi być WYŻSZE niż pierwotny wpis,
            # inaczej kalendarz uzna plik za nieaktualny i nic nie zmieni.
            f"SEQUENCE:{1 if res.get('cancelled') or method == 'CANCEL' else 0}",
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


# Odpowiedź serwera, gdy miejsce jest JUŻ NASZE. Przy strzale redundantnym dostaje ją
# każda kopia poza zwycięską — to nie jest przegrany wyścig, tylko oczekiwany koniec
# drugiego losowania. Rywal zajmujący termin daje inny komunikat („No available seats").
# Mylenie tych dwóch zamieniałoby każdy hedgowany dzień w fałszywy alarm.
WLASNA_REZERWACJA = "Booking is already exists"
# Etykieta w dzienniku. Wydzielona, bo `hunt_alert_reason` musi umieć odróżnić ją od
# prawdziwej porażki — inaczej alarm „żadna rezerwacja się nie udała" odpalałby w dniu,
# w którym termin trzymamy w kieszeni.
POWOD_JUZ_NASZE = "miejsce już nasze"

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

# STRZAŁ REDUNDANTNY: ile równoległych zapisów w NAJCENNIEJSZY termin.
#
# Czas przetwarzania zapisu przez serwer skacze losowo: 61, 62, 63, 71, 115, 150, 157,
# 178, 188, 236, 251, 730 ms — bez związku z czymkolwiek, co robimy (31.08 samotny
# strzał na danych sprzed 1 ms trwał 730 ms). Skoro to loteria, jedno losowanie można
# zamienić na minimum z kilku: dwa równoległe zapisy w ten sam termin, liczy się ten,
# który wróci pierwszy. Ten sam termin trafiony dwukrotnie dawał już 62 i 251 ms
# (30.08, 12:00) oraz 700 i 68 ms (25.08, 20:00) — rozrzut jest ogromny.
#
# Dotyczy WYŁĄCZNIE czołowego celu. Rozciąganie tego na całą salwę mnożyłoby ryzyko
# podwójnej rezerwacji bez żadnych danych, że pomaga — to osobny eksperyment.
HEDGE_MAX = 3
_salvo_pool = None


def salvo_pool(size):
    """Pula TRWAŁYCH wątków — każdy trzyma własne, ciepłe połączenie (threading.local).

    Świeży wątek oznaczałby świeże połączenie i ~160 ms na uzgodnienie TLS, czyli
    dokładnie to, co salwa ma wyeliminować. Dlatego pula żyje przez cały proces.
    """
    global _salvo_pool
    if _salvo_pool is None:
        # Miejsce także na kopie czołowego strzału. Gdyby pula była mniejsza,
        # kopie czekałyby w KOLEJCE PULI zamiast lecieć równolegle — czyli dokładnie
        # odwrotnie, niż wymaga eksperyment.
        _salvo_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=SALVO_MAX + HEDGE_MAX - 1, thread_name_prefix="salwa")
    return _salvo_pool


def warm_connections(pool, size, url):
    """Rozgrzewa `size` połączeń w RÓŻNYCH wątkach podanej puli (bariera je rozdziela).

    Bez bariery szybkie zadania wykonałyby się na jednym wątku i rozgrzałoby się
    jedno połączenie zamiast wszystkich. Dotyczy tak samo salwy, jak i sprintu:
    kilka równoczesnych uzgodnień TLS na zimno potrafi zająć sekundy, a sprint
    ma do dyspozycji tylko kilka sekund wokół publikacji.

    UWAGA HISTORYCZNA: w 0.9.0 rozgrzewka wysyłała też uwierzytelniony POST
    (`users.getMe`), żeby sprawdzić hipotezę „brama waliduje JWT przy pierwszym
    użyciu i dlatego pierwsza rejestracja kosztuje ~300 ms". 10.08 hipoteza UPADŁA:
    ten POST kosztował 48 ms, a pierwsza rejestracja i tak 232 ms. Nie wracaj do tego
    pomysłu — koszt nie siedzi ani w połączeniu, ani w uwierzytelnieniu.
    """
    size = max(1, size)
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

    list(pool.map(rozgrzej, range(size)))


def fmt_shot(res):
    """Opis czasu strzału: kiedy ruszył względem salwy i jak długo trwał.

    Odstęp startu jest tu po to, żeby dało się przypisać winę za „schodek" czasów.
    13.08 z Irlandii cztery strzały naraz zajęły 185/217/326/382 ms, a jeden samotny
    121 ms. Jeśli starty są bliskie zeru, kolejkuje serwer; jeśli się rozjeżdżają,
    problem jest u nas (pula wątków, DNS, TLS) — bez tej liczby to zgadywanka.
    """
    start = res.get("start_ms")
    opis = f"{res['ms']} ms" if start is None else f"start +{start} ms, {res['ms']} ms"
    # Wiek informacji, na podstawie której strzelamy. 24.08 strzał w 20:00 trwał 74 ms
    # — podłoga tego, co osiągalne — i wrócił 409. Skoro sam zapis był błyskawiczny,
    # to znaczy, że miejsce zniknęło ZANIM zapytaliśmy. Bez tej liczby nie da się
    # odróżnić „byliśmy wolni" od „patrzyliśmy na nieaktualny grafik".
    wiek = res.get("seen_ms")
    return opis if wiek is None else f"{opis}, dane sprzed {wiek} ms"


# ODSTĘP MIĘDZY STRZAŁAMI SALWY (ms). Zmierzone 14.08 z Irlandii: cztery strzały
# ruszyły z `start +0 ms` co do jednego, a wróciły po 21 / 37 / 117 / 282 ms. Skoro
# startują razem, ten „schodek" powstaje po stronie Decathlona — najpewniej serwer
# serializuje zapisy per KONTO, więc nasze własne strzały stoją w kolejce jeden za
# drugim. Miejsce w tej kolejce było LOSOWE: 15:00 wymienione jako ostatnie weszło
# w 37 ms, a 17:00 jako trzecie czekało 282 ms.
#
# Odstęp sprawia, że kolejność w kolejce jest NASZA, a nie przypadkowa: najbardziej
# pożądany termin wchodzi pierwszy. Wystarczy kilka ms — w regionie rozrzut sieci
# jest poniżej milisekundy, więc nie trzeba czekać na odpowiedź, tylko zagwarantować
# kolejność dotarcia. Przy 4 strzałach ostatni rusza 24 ms później, czyli o rząd
# wielkości mniej niż obserwowany rozrzut.
#
# UWAGA: to wynika z HIPOTEZY o kolejce per konto. Gdyby okazała się fałszywa, kosztem
# jest te kilkanaście ms na dalszych strzałach. `auto_register_stagger: 0` przywraca
# strzelanie wszystkim naraz.
SALVO_STAGGER_MS = 8


def fire_salvo(targets, listing_price_by_id, cfg, speculative, size, ranks=None):
    """Wysyła próby rejestracji równolegle, w kolejności preferencji.

    Każdy wątek dostaje własną kopię cfg — inaczej odświeżenie tokenu w jednym
    wątku nadpisywałoby stan pozostałych.

    `targets` są już posortowane wg `auto_register_order`, więc indeks 0 to termin
    najbardziej pożądany — i to on dostaje przewagę, gdy odstęp jest włączony.

    `ranks` pozwala rozdzielić POZYCJĘ W KOLEJCE od pozycji na liście: kopie tego samego
    terminu (strzał redundantny) mają tę samą rangę, więc startują razem, a nie
    schodkowo. Bez tego odstęp rozsuwałby losowania, które mają być równoczesne.
    """
    fired = targets[:size]
    try:
        odstep = max(0, min(int(cfg.get("stagger", SALVO_STAGGER_MS)), 100)) / 1000.0
    except (TypeError, ValueError):
        odstep = SALVO_STAGGER_MS / 1000.0
    salwa_start = time.monotonic()
    zobaczone = cfg.get("seen_at")

    def strzal(numer_i_slot):
        numer, slot = numer_i_slot
        # Czekamy PRZED pomiarem, żeby `ms` był czystym czasem żądania, a odstęp
        # widać było w `start_ms`. Dzięki temu log sam pokazuje, czy odstęp zadziałał.
        if odstep and numer:
            time.sleep(numer * odstep)
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
                # Ile czasu minęło od chwili, gdy zobaczyliśmy ten termin jako wolny,
                # do chwili, gdy ruszył zapis. To jest opóźnienie, które przegrywa
                # wieczorne godziny — nie czas samego żądania.
                "seen_ms": (None if zobaczone is None
                            else int((started - zobaczone) * 1000)),
                # Odstęp od startu salwy. To NIE jest ozdobnik: rozstrzyga, czy strzały
                # ruszyły razem (wtedy „schodek" czasów robi serwer), czy rozjechały się
                # u nas (wtedy wina jest po naszej stronie — pula, DNS, TLS).
                # 13.08 z Irlandii: 185/217/326/382 ms przy czterech naraz, a 121 ms
                # przy jednym samotnym. Bez odstępów nie da się tego przypisać.
                "start_ms": int((started - salwa_start) * 1000),
                "tx": local.get("transaction_id") or "",
                "token": local.get("token") or ""}

    pool = salvo_pool(size)
    pary = list(zip(ranks, fired)) if ranks else list(enumerate(fired))
    return list(pool.map(strzal, pary))


# --------------------------------------------------------- pomiar opóźnienia

# Ocena rundy do serwera. Zmierzone z łącza domowego po WiFi: ~80 ms do Irlandii,
# przy ~10 ms na samą bramę lokalną. Kabel zamiast WiFi potrafi urwać kilkanaście ms,
# a przy DWÓCH rundach w ścieżce rezerwacji liczy się to podwójnie.
RTT_DOBRE = 45
RTT_TYPOWE = 75


def measure_rtt(host, port=443, samples=5, timeout=3):
    """(mediana, min, max) czasu jednej rundy do serwera w ms. None, gdy nie doszło.

    Czysty TCP — bez TLS i bez dotykania API, więc pomiar niczego nie obciąża.
    """
    czasy = []
    for _ in range(samples):
        sock = socket.socket()
        sock.settimeout(timeout)
        started = time.monotonic()
        try:
            sock.connect((host, port))
            czasy.append((time.monotonic() - started) * 1000)
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass
    if not czasy:
        return None
    czasy.sort()
    return czasy[len(czasy) // 2], czasy[0], czasy[-1]


def log_rtt(host):
    """Wypisuje opóźnienie do serwera i tłumaczy, ile z niego bierze rezerwacja."""
    wynik = measure_rtt(host)
    if not wynik:
        log(f"📡 Nie zmierzyłem opóźnienia do {host} — brak połączenia?")
        return None
    mediana, naj, gorszy = wynik
    log(f"📡 Runda do {host}: {mediana:.0f} ms (min {naj:.0f}, max {gorszy:.0f}). "
        f"Rezerwacja potrzebuje DWÓCH takich rund (~{2 * mediana:.0f} ms) — "
        f"tego nie da się skrócić kodem.")
    if mediana >= RTT_TYPOWE:
        log(f"   Opóźnienie wysokie. Jeśli serwer stoi na WiFi, kabel potrafi urwać "
            f"kilkanaście ms — a liczy się to podwójnie.")
    elif mediana < RTT_DOBRE:
        log("   Opóźnienie bardzo dobre — z tej strony nie ma już czego poprawiać.")
    return mediana


# ------------------------------------------------------------------ sprint

# Między odpytaniami stoimy bezczynnie — przy takcie 0,2 s to średnio ~100 ms straty.
# Zmierzone: 3 wątki pobierające BEZ PRZERW dają świeży obraz co ~20 ms (34 zapytania/s).
# Dlatego przez kilka sekund wokół sekundy publikacji przechodzimy na tryb ciągły.
SPRINT_MAX_THREADS = 4
_sprint_pool = None


def sprint_pool():
    """Pula OSOBNA od salwy — inaczej wątki zajęte pobieraniem blokowałyby strzały.

    Miejsc jest WIĘCEJ niż maksimum wątków sprintu: zwycięzca wraca natychmiast,
    a maruderzy dokańczają swoje pobranie jeszcze przez chwilę. Bez zapasu jeden
    wolniejszy strzał zabierałby miejsce kolejnej rundzie i sprint cichcem
    działałby węższy, niż prosił użytkownik.
    """
    global _sprint_pool
    if _sprint_pool is None:
        _sprint_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=SPRINT_MAX_THREADS * 2, thread_name_prefix="sprint")
    return _sprint_pool


def run_sprint(deadline, threads, listing_url, baseline_ids, tz, filters=None):
    """Pobiera bez przerw, aż pojawi się termin spoza `baseline_ids`.

    Zwraca (listing_id, dokument, chwila_pobrania) — dane SĄ JUŻ POBRANE, więc
    rejestracja nie płaci drugi raz za rundę do serwera (~92 ms). Trzeci element to
    monotoniczny znacznik chwili, w której serwer oddał grafik: bez niego nie da się
    powiedzieć, jak stara była informacja, na podstawie której strzelaliśmy.
    None, gdy okno minęło bez zmian.

    Punkt odniesienia bierzemy z zapisanego stanu, a nie z pierwszego pobrania:
    inaczej publikacja, która trafi w pierwsze ~90 ms sprintu, wpadłaby do punktu
    odniesienia i sprint nigdy by się nie odpalił.
    """
    threads = max(1, min(int(threads), SPRINT_MAX_THREADS))
    try:
        lid = resolve_current_id(listing_id_from_url(listing_url))
    except Exception as e:  # noqa: BLE001 - sprint nie może wywrócić polowania
        log(f"! Sprint: nie rozwiązałem adresu kortu ({e!r}) — pomijam")
        return None
    # Filtry można podać z zewnątrz — w Lambdzie nie ma config.json, a i tak muszą
    # być IDENTYCZNE z tymi, których używa monitor (inaczej sprint uzna za „nowy"
    # termin, którego monitor w ogóle nie śledzi).
    if filters is None:
        filters = resolve_filters(load_config(quiet=True))
    znalezione, lock, stop = {}, threading.Lock(), threading.Event()

    def obserwuj(_):
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                doc = fetch_listing(lid)
                # Znacznik stawiamy TU, a nie po filtrowaniu: to moment, w którym
                # serwer oddał nam grafik. Wszystko dalej to już nasze opóźnienie.
                zobaczone = time.monotonic()
                swiezy = {s["id"] for s in free_slots(doc, lid, datetime.now(timezone.utc))
                          if passes_filter(s, filters, tz)}
            except Exception:  # noqa: BLE001 - pojedyncza wpadka: próbujemy dalej
                # Krótka przerwa: bez niej trwała awaria endpointu zamieniłaby sprint
                # w pętlę dobijającą się do serwera tak szybko, jak pozwoli sieć.
                stop.wait(0.05)
                continue
            if swiezy - baseline_ids:
                with lock:
                    if not znalezione:
                        znalezione["hit"] = (lid, doc, zobaczone)
                        stop.set()
                return

    pool = sprint_pool()
    for i in range(threads):
        pool.submit(obserwuj, i)
    # Czekamy na PIERWSZE trafienie, nie na wszystkie wątki: maruder w trakcie
    # pobierania kosztowałby do ~90 ms, czyli tyle, ile sprint ma zaoszczędzić.
    stop.wait(timeout=max(0.0, deadline - time.monotonic()))
    stop.set()   # okno minęło albo mamy trafienie — pozostałe wątki kończą same
    return znalezione.get("hit")


# ---------------------------------------------------- zdalny strzał (eu-west-1)

# Serwer Decathlona stoi w AWS eu-west-1 (sporteo-01-*.eu-west-1.elb.amazonaws.com).
# Zmierzone 11.08 z Lambdy w tym samym regionie: runda 0,5 ms zamiast ~42 ms z domu,
# a ciężkie pobranie 39 ms zamiast ~80 ms. Ścieżka „termin się pojawia → nasze żądanie
# dociera" spada ze ~107 ms do ~32 ms. Dlatego w sekundzie publikacji sprint i salwę
# wykonuje funkcja w Irlandii, a cała reszta (token, panel, kalendarz, stan,
# powiadomienia) zostaje w Home Assistancie.
#
# Token leci w TREŚCI żądania i nigdzie w AWS nie jest zapisywany.
REMOTE_TIMEOUT_MARGIN = 6      # ile sekund ponad okno sprintu dajemy na odpowiedź


def call_remote(url, secret, payload, timeout):
    """Odpala zdalny sprint+salwę. Zwraca (wynik, błąd) — dokładnie jedno jest None.

    Odpowiedź jest spakowana gzipem: dokument kortu ma ~260 KB, a spakowany ~25 KB.
    Leci już PO rejestracji, więc nie jest na ścieżce krytycznej, ale nie ma powodu
    ciągnąć ćwierć megabajta przez łącze domowe.
    """
    dane = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=dane,
        headers={
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": UA,
            # Function URL zostaje bez autoryzacji IAM, więc wpuszczamy tylko żądania
            # z tym sekretem. Świadomy wybór: klucze AWS w konfiguracji dodatku byłyby
            # groźniejsze w razie wycieku kopii zapasowej niż sekret do jednej funkcji.
            "X-Padel-Secret": secret or "",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            surowe = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                surowe = gzip.decompress(surowe)
        return json.loads(surowe.decode("utf-8")), None
    except urllib.error.HTTPError as e:
        szczegol = ""
        try:
            szczegol = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                e.close()
            except Exception:  # noqa: BLE001
                pass
        return None, f"HTTP {e.code}: {szczegol or e.reason}"
    except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
        return None, f"brak odpowiedzi: {e!r}"
    except (ValueError, OSError) as e:
        return None, f"zła odpowiedź: {e!r}"


def remote_payload(listing_url, baseline_ids, tz_name, seconds, threads, reg_cfg):
    """Treść żądania do Irlandii. Filtry idą SUROWYM napisem z env, żeby zdalna
    strona sparsowała je tą samą funkcją — inaczej ryzykujemy dwie różne interpretacje."""
    return {
        "token": reg_cfg.get("token") or "",
        "listing_url": listing_url,
        "filters": os.environ.get("FILTERS", ""),
        "timezone": tz_name,
        "baseline_ids": sorted(baseline_ids),
        "sprint_seconds": seconds,
        "sprint_threads": threads,
        "salvo": reg_cfg.get("salvo") or 0,
        "stagger": reg_cfg.get("stagger", SALVO_STAGGER_MS),
        # Bez tego wyłącznik strzału czołowego działałby tylko lokalnie, a w sekundzie
        # publikacji strzela WŁAŚNIE Irlandia — czyli nie działałby wcale.
        "lead": boolish(reg_cfg.get("lead")),
        # Strzał redundantny musi działać po TEJ stronie, która naprawdę strzela
        # w sekundzie publikacji — czyli w Irlandii.
        "hedge": reg_cfg.get("hedge") or 1,
        "max_per_run": reg_cfg.get("max_per_run") or 1,
        "order": reg_cfg.get("order") or "earliest",
        "name": reg_cfg.get("name") or "",
        "age": reg_cfg.get("age"),
        "free_only": bool(reg_cfg.get("free_only", True)),
        "speculative": bool(reg_cfg.get("speculative")),
        "enabled": bool(reg_cfg.get("enabled")),
    }


def adopt_remote(wynik):
    """Przenosi wynik z Irlandii do struktur, których używa `run_once`.

    Zwraca (prefetched, remote) albo (None, None), gdy nic nie znaleziono.
    Linie dziennika ze zdalnej strony przepisujemy do NASZEGO Dziennika — inaczej
    trzeba by ich szukać w CloudWatch, a to jedyny ślad po sekundzie publikacji.
    """
    for linia in wynik.get("log") or []:
        # Zdalna linia ma już swój znacznik czasu, a `log()` dokłada własny —
        # bez tego w Dzienniku byłyby dwa obok siebie.
        log(f"   ☁ {re.sub(r'^\[[^]]+\]\s*', '', linia)}")
    czasy = wynik.get("timings") or {}
    if czasy:
        # Licznik partii pokazujemy TAKŻE przy zerze. Jego brak jest jedynym sygnałem,
        # że Lambda chodzi na kodzie sprzed 0.20.0 — a ukrywanie zera odbierało
        # możliwość odróżnienia „stary kod" od „nowy kod, nic nie znalazł".
        partie = czasy.get("batches")
        log(f"☁ Irlandia: sprint {czasy.get('sprint_ms', '?')} ms, "
            f"całość {czasy.get('total_ms', '?')} ms"
            + (f", {partie} {plural(partie, 'partia', 'partie', 'partii')}"
               if partie is not None else " (stara wersja funkcji — wgraj paczkę)"))
    if not wynik.get("doc"):
        return None, None
    # Wiek pierwszego wykrycia -> chwila wykrycia w NASZYM zegarze.
    wiek = czasy.get("first_hit_ago_ms")
    remote = {
        "wykryto": (datetime.now(timezone.utc) - timedelta(milliseconds=wiek)
                    if isinstance(wiek, (int, float)) else None),
        # klucze przychodzą jako listy (JSON nie ma krotek) — normalizujemy do (ok, msg)
        "results": {k: (bool(v[0]), v[1]) for k, v in (wynik.get("results") or {}).items()},
        "registered": set(wynik.get("registered") or []),
        "transactions": wynik.get("transactions") or {},
        "shots": wynik.get("shots") or [],
    }
    return (wynik["listing_id"], wynik["doc"]), remote


def resolve_filters(cfg):
    """Okna czasowe z FILTERS (env) albo z config.json.

    Wspólne dla monitora i sprintu — gdyby liczyły filtry osobno, sprint mógłby uznać
    za „nowy" termin, którego monitor w ogóle nie śledzi (i odwrotnie).
    """
    filters_env = os.environ.get("FILTERS")
    if filters_env:
        try:
            return parse_filters_env(filters_env)
        except Exception as e:  # noqa: BLE001 - błędny env nie może wywrócić procesu
            log(f"! Błędny FILTERS '{filters_env}': {e} — używam filtrów z config.json")
    return cfg.get("filters", [])


def oddaj_salwe(fired, listing_price_by_id, cfg, speculative, salvo):
    """Wybiera kształt salwy, ogłasza go w logu i strzela. Zwraca `(wyniki, ile_kopii)`.

    Trzy kształty, w kolejności historycznej:

    - **redundantny** (`hedge`, domyślny) — najcenniejszy termin dostaje kilka
      równoległych zapisów o TEJ SAMEJ randze, więc ruszają razem. Czas przetwarzania
      zapisu przez serwer to loteria (61–730 ms bez związku z czymkolwiek, co robimy),
      więc bierzemy minimum z kilku losowań zamiast jednego.
    - **zwykły** — wszystkie cele naraz.
    - **czołowy** (`lead`) — najcenniejszy termin sam i pierwszy. HIPOTEZA OBALONA
      31.08: czołowy strzał w 19:00 poszedł SAM, na danych sprzed 1 ms, i trwał 730 ms —
      przy zerowej konkurencji z naszej strony. Kolejka nie jest nasza, tylko serwera.
      Co gorsza, kosztowało to drugi strzał: 17:00 ruszyło na danych sprzed 732 ms.
      Mediany po dwóch dniach: samotny 251 ms, z salwy 157 ms. Domyślnie wyłączone;
      zostaje wyłącznie jako wyłącznik na wypadek powtórzenia pomiaru.
    """
    opis = ", ".join(fmt_when(s["start_utc"].astimezone(_log_tz()), short=True) for s in fired)
    pierwszy = fmt_when(fired[0]["start_utc"].astimezone(_log_tz()), short=True)
    czolowy = boolish(cfg.get("lead")) and len(fired) > 1
    try:
        kopie = max(1, min(int(cfg.get("hedge") or 1), HEDGE_MAX))
    except (TypeError, ValueError):
        kopie = 1

    if czolowy:
        log(f"⇉ Strzał czołowy: {pierwszy} sam, potem salwa w resztę ({opis})")
        wyniki = fire_salvo(fired[:1], listing_price_by_id, cfg, speculative, 1)
        # Któryś wątek mógł odnowić token po 401 — druga fala nie ma czekać na to samo.
        for res in wyniki:
            cfg["token"] = newer_decathlon_token(cfg.get("token") or "", res.get("token") or "")
        wyniki += fire_salvo(fired[1:], listing_price_by_id, cfg, speculative, salvo - 1)
        ile_kopii = collections.Counter(s["id"] for s in fired)
    elif kopie > 1:
        log(f"⇉ Salwa: {len(fired)} prób równolegle, w tym {pierwszy} "
            f"×{kopie} równolegle ({opis})")
        strzaly = [fired[0]] * kopie + fired[1:]
        rangi = [0] * kopie + list(range(1, len(fired)))
        wyniki = fire_salvo(strzaly, listing_price_by_id, cfg, speculative,
                            len(strzaly), ranks=rangi)
        ile_kopii = collections.Counter([fired[0]["id"]] * kopie
                                        + [s["id"] for s in fired[1:]])
    else:
        log(f"⇉ Salwa: {len(fired)} prób równolegle ({opis})" if len(fired) > 1
            else f"⇉ Strzał z rozgrzanego wątku ({opis})")
        wyniki = fire_salvo(fired, listing_price_by_id, cfg, speculative, salvo)
        ile_kopii = collections.Counter(s["id"] for s in fired)
    return wyniki, ile_kopii


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
    # Czasy strzałów wędrują przez cfg (jak transaction_id), żeby trafiły do dziennika
    # polowań. Bez nich wpis mówiłby CO się udało, ale nie JAK szybko — a to właśnie
    # ta liczba rozstrzygała każdą dotychczasową zagadkę.
    # DOKŁADAMY, nie kasujemy. Zerowanie zmuszało każdego wołającego, żeby pamiętał
    # o sklejeniu list po sobie — robiły to trzy miejsca (druga fala lokalnie, pętla
    # partii w Lambdzie, scalanie wyniku zdalnego). Zapomniana kopia nie krzyczy, tylko
    # cicho gubi strzały z Dziennika, czyli jedyny ślad po sekundzie publikacji.
    # Świeżą listę daje `build_reg_cfg`, wołane raz na bieg.
    cfg.setdefault("shots", [])
    done = 0
    skipped = []

    # SALWA: najbardziej pożądane terminy lecą NARAZ. Po kolei strzał w czwarty termin
    # szedł ~350 ms po pierwszym — tyle wystarczy, żeby stracić wieczorną godzinę.
    try:
        salvo = max(0, min(int(cfg.get("salvo") or 0), SALVO_MAX))
    except (TypeError, ValueError):
        salvo = 0
    queue = list(todo)
    # Także POJEDYNCZY termin idzie przez pulę salwy. Jej wątki są rozgrzane tuż przed
    # sprintem, a wątek główny nie — i to na nim 9.08 poszedł samotny strzał w 19:00:
    # 319 ms wobec 57–84 ms strzałów z puli w tej samej sekundzie. Termin przepadł.
    if salvo > 1 and queue:
        fired = queue[:salvo]
        queue = queue[salvo:]
        wins, auth_error = [], None
        wyniki, ile_kopii = oddaj_salwe(fired, listing_price_by_id, cfg, speculative, salvo)
        # Któryś wątek mógł odnowić token po HTTP 401 (pracował na kopii cfg).
        # Przejmujemy najświeższy, żeby próby sekwencyjne po salwie nie czekały
        # jeszcze raz na to samo odnowienie.
        for res in wyniki:
            cfg["token"] = newer_decathlon_token(cfg.get("token") or "", res.get("token") or "")
        for res in wyniki:
            slot, msg, ms = res["slot"], res["msg"], res["ms"]
            when = fmt_when(slot["start_utc"].astimezone(_log_tz()), short=True)
            cfg["shots"].append({"when": when, "ok": bool(res["ok"]), "ms": ms,
                                 "start_ms": res.get("start_ms", 0),
                                 "seen_ms": res.get("seen_ms"), "salwa": True,
                                 "hedge": ile_kopii.get(slot["id"], 1) > 1,
                                 # Powód wędruje ze strzałem, bo tylko on dociera do
                                 # dziennika, gdy termin zdążył zniknąć z grafiku.
                                 "why": "" if res["ok"] else skroc_powod(msg)})
            # Przy strzale redundantnym dwie odpowiedzi dotyczą tego samego terminu:
            # jedna kopia dostaje 409, druga wygrywa. Sukces MUSI być lepki, inaczej
            # wolniejsza porażka nadpisałaby zwycięstwo i powiadomienie skłamałoby,
            # że termin przepadł.
            #
            # Podwójnej rezerwacji NIE bronimy — limit miejsc w terminie wynosi 1, więc
            # gdy jedna kopia zapisze się skutecznie, druga z definicji dostaje 409.
            if not (results.get(slot["id"]) or (False,))[0]:
                results[slot["id"]] = (res["ok"], msg)
            if res["ok"]:
                wins.append(res)
            elif WLASNA_REZERWACJA in msg:
                # Oczekiwany koniec drugiego losowania — bez „!", bo to nie jest kłopot.
                log(f"= Kopia strzału w {when} odbita: miejsce już nasze [{fmt_shot(res)}]")
            elif any(m in msg for m in AUTH_FAILURE_MARKERS):
                auth_error = auth_error or msg
                log(f"! Salwa: {when} — {msg} [{fmt_shot(res)}]")
            else:
                log(f"! Auto-rejestracja nieudana dla {when}: {msg} [{fmt_shot(res)}]")

        # Zwycięzcy w kolejności preferencji; nadmiar ponad limit oddajemy od razu,
        # żeby nie blokować terminu innym grającym dłużej niż ułamek sekundy.
        for res in wins:
            slot, msg, ms = res["slot"], res["msg"], res["ms"]
            when = fmt_when(slot["start_utc"].astimezone(_log_tz()), short=True)
            if done < limit:
                done += 1
                if speculative:
                    log(f"~ Auto-rejestracja (test, bez rezerwacji): {when} — {msg} [{fmt_shot(res)}]")
                else:
                    registered.add(slot["id"])
                    log(f"✓ Auto-rejestracja: {when} — {msg} [{fmt_shot(res)}]")
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
        zobaczone = cfg.get("seen_at")
        wiek = None if zobaczone is None else int((attempt_started - zobaczone) * 1000)
        cfg["shots"].append({"when": when, "ok": bool(ok), "ms": took_ms,
                             "start_ms": None, "seen_ms": wiek, "salwa": False,
                             "why": "" if ok else skroc_powod(msg)})
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
        if WLASNA_REZERWACJA in msg:
            log(f"= Strzał w {when} odbity: miejsce już nasze [{took_ms} ms]")
        else:
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


# ODŁOŻONY PUSH: ntfy.sh to zapytanie do CAŁKIEM INNEGO serwera, a w zrywie wysyłamy
# je w najgorętszej sekundzie dnia. Zmierzone w logach z 9. i 10.08: między wykryciem
# partii a wznowieniem sprintu mijało 643–819 ms, w większości właśnie na tym pushu —
# i przez ten czas NIE PATRZYLIŚMY na grafik, choć publikacja wciąż trwała.
# Dlatego w zrywie powiadomienia lądują w kolejce i idą po zamknięciu okna.
# Poza zrywem nic się nie zmienia: push leci natychmiast, jak dotąd.
_pending_notifications = []
NOTIFY_MAX_ATTEMPTS = 3


def queue_notification(wpis):
    """Odkłada paczkę powiadomień na po zrywie (patrz `flush_notifications`)."""
    _pending_notifications.append(wpis)


def flush_notifications():
    """Wysyła odłożone powiadomienia. Zwraca liczbę wysłanych paczek.

    Nieudane wracają do kolejki i są ponawiane, ale nie w nieskończoność — po
    NOTIFY_MAX_ATTEMPTS porzucamy je GŁOŚNO. Cicho porzucone powiadomienie o wolnym
    terminie jest gorsze niż żadne, bo wygląda jak brak terminów.
    """
    if not _pending_notifications:
        return 0
    czekaly = list(_pending_notifications)
    _pending_notifications.clear()
    wyslane = 0
    for topic, slots, tz, listing_price, url, wyniki, proba in czekaly:
        failed = notify_new(topic, slots, tz, listing_price, url, wyniki)
        if not failed:
            wyslane += 1
            continue
        if proba + 1 < NOTIFY_MAX_ATTEMPTS:
            _pending_notifications.append(
                (topic, slots, tz, listing_price, url, wyniki, proba + 1))
        else:
            log(f"! Porzucam {len(failed)} odłożonych powiadomień po "
                f"{NOTIFY_MAX_ATTEMPTS} próbach — sprawdź NTFY_TOPIC.")
    if wyslane:
        log(f"📨 Wysłano {wyslane} {plural(wyslane, 'odłożone powiadomienie', 
                                            'odłożone powiadomienia', 'odłożonych powiadomień')} "
            f"(zryw ich nie blokował).")
    return wyslane


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


# Temat z przykładu w konfiguracji. ntfy.sh nie ma haseł: KAŻDY, kto zna nazwę tematu,
# czyta wszystkie wiadomości i może wysyłać własne. Zostawiona wartość domyślna znaczy
# więc, że powiadomienia o Twoich rezerwacjach czyta każdy, kto też jej nie zmienił —
# i że Ty czytasz jego.
TEMATY_PRZYKLADOWE = ("your-ntfy-topic-here", "padel", "test", "changeme")


def ostrzez_o_temacie(topic):
    """Jednorazowe ostrzeżenie o temacie, który nie chroni niczego."""
    czysty = (topic or "").strip().lower()
    if not czysty:
        return
    if czysty in TEMATY_PRZYKLADOWE:
        log(f"! Temat ntfy '{topic}' to wartość przykładowa. ntfy.sh nie ma haseł — "
            f"kto zna nazwę tematu, czyta Twoje powiadomienia i może wysyłać własne. "
            f"Ustaw długą, losową nazwę w opcji ntfy_topic.")
    elif len(czysty) < 12:
        log(f"! Temat ntfy '{topic}' jest krótki — na ntfy.sh nazwa tematu jest jedynym "
            f"zabezpieczeniem. Zalecane co najmniej 16 losowych znaków.")


# Jak rzadko wolno przypominać, że monitor wstał. Aktualizacja dodatku ma dać jeden
# push; pętla restartów — jeden na godzinę, a nie jeden na minutę.
STARTUP_PUSH_MIN_ODSTEP = 3600


def _wolno_powiadomic_o_starcie(state_doc):
    ostatni = (state_doc or {}).get("startup_push_at")
    if not ostatni:
        return True
    return _starszy_niz(ostatni, STARTUP_PUSH_MIN_ODSTEP)


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

def build_reg_cfg(cfg, state_doc):
    """Ustawienia auto-rejestracji z opcji dodatku i stanu.

    Wspólne dla lokalnego biegu i dla żądania do Irlandii — gdyby liczyły się osobno,
    zdalna strona mogłaby np. dostać inny limit niż lokalna i zarezerwować za dużo.
    """
    return {
        "enabled": boolish(os.environ.get("AUTO_REGISTER") or cfg.get("auto_register")),
        "speculative": boolish(os.environ.get("AUTO_REGISTER_DRY_RUN") or cfg.get("auto_register_dry_run")),
        "token": resolve_decathlon_token(cfg, state_doc),
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
        "hedge": os.environ.get("AUTO_REGISTER_HEDGE") or cfg.get("auto_register_hedge") or 1,
        # DOMYŚLNIE WYŁĄCZONY — hipoteza obalona 31.08, patrz komentarz przy strzale
        # czołowym w `auto_register_new_slots`.
        "lead": os.environ.get("AUTO_REGISTER_LEAD") or cfg.get("auto_register_lead", False),
        "stagger": os.environ.get("AUTO_REGISTER_STAGGER")
                   or cfg.get("auto_register_stagger", SALVO_STAGGER_MS),
    }


# Ile najwyżej czekamy na jedno pobranie obserwatora. Patrz komentarz w `patrz()`.
OBSERWATOR_TIMEOUT = 5


def obserwuj_podczas_zapisu(lid, filters, tz, znane_ids):
    """Pobiera grafik BEZ PRZERW, dopóki trwa nasz własny zapis. Zwraca (stop, wynik).

    SEDNO (03.09): czekanie na odpowiedź serwera trwało 1303 ms i przez cały ten czas
    nikt nie patrzył na grafik. W tym oknie liczba dostępnych terminów skoczyła z 4 na
    10 — sześć terminów pojawiło się, gdy byliśmy zajęci własnym strzałem. Gdy
    spojrzeliśmy ponownie, żaden z nich nie pasował już do filtra: wieczorne godziny
    zdążyły zniknąć. Przez 39 kolejnych sekund i 80 pobrań nie wróciła ani jedna.

    Publikacja przychodzi partiami, a zapis trwa dłużej niż odstęp między nimi — więc
    strzelanie i patrzenie MUSZĄ dziać się jednocześnie.

    `wynik["nowe"]` to sloty spoza `znane_ids`, które przeszły przez filtr.
    """
    stop = threading.Event()
    wynik = {"nowe": {}, "doc": None, "pobran": 0}

    def patrz():
        while not stop.is_set():
            try:
                # KRÓTKI timeout, nie domyślne 60 s. Obserwator pyta co ~100 ms, więc
                # odpowiedź po dziesięciu sekundach jest dla niego bezwartościowa —
                # a zawieszone pobranie trzyma wątek puli sprintu jeszcze długo po
                # `stop.set()`. Przy 60 s jeden taki wątek blokowałby slot przez CAŁE
                # okno zrywu, i to dokładnie w sekundzie publikacji.
                doc = fetch_listing(lid, timeout=OBSERWATOR_TIMEOUT)
            except Exception:  # noqa: BLE001 - obserwacja nie może wywrócić polowania
                stop.wait(0.05)
                continue
            wynik["pobran"] += 1
            wynik["doc"] = doc          # zawsze najświeższy — grafik dnia liczymy z niego
            for s in free_slots(doc, lid, datetime.now(timezone.utc)):
                if s["id"] not in znane_ids and passes_filter(s, filters, tz):
                    wynik["nowe"].setdefault(s["id"], s)

    sprint_pool().submit(patrz)
    return stop, wynik


class Grafik:
    """Wynik jednego obchodu kortów — wszystko, czego potrzebuje reszta biegu.

    Wydzielone z `run_once`, która miała 256 linii i 69 punktów rozgałęzienia. Pięć
    błędów rodzaju „etykieta twierdziła więcej, niż mówiły dane" (0.14.1, 0.16.1,
    0.18.0, 0.19.1, 0.20.2) wyszło dopiero z produkcji — bo w tak długiej funkcji nie
    widać, że dwie listy powstają z różnych zbiorów.

    `blad=True` znaczy: pobranie się nie udało i stanu NIE WOLNO nadpisać. Cisza po
    błędzie sieci jest bezpieczna, nadpisany stan już nie — następny bieg uznałby
    wszystkie terminy za nowe i wysłał lawinę powiadomień.
    """

    __slots__ = ("current", "book_url", "book_url_by_id", "listing_price_by_id",
                 "lid_by_id", "meta_by_lid", "docs_by_lid", "seen_at", "blad")

    def __init__(self):
        self.current = {}                # id terminu -> termin
        self.book_url = None             # kanoniczny link, z PIERWSZEGO kortu
        self.book_url_by_id = {}
        self.listing_price_by_id = {}
        self.lid_by_id = {}              # który kort wydał dany termin
        self.meta_by_lid = {}            # kort -> (link, cena)
        self.docs_by_lid = {}            # do policzenia CAŁEGO grafiku dnia
        self.seen_at = None              # NAJSTARSZA obserwacja: punkt zerowy wieku danych
        self.blad = False


def zbierz_terminy(listings, filters, tz, now_utc, skip_light=False, prefetched=None):
    """Obchodzi korty i zwraca `Grafik`.

    Wyłącznie zbieranie: nie rejestruje, nie powiadamia, nie zapisuje stanu. Dzięki
    temu da się to przetestować bez udawania połowy aplikacji.
    """
    g = Grafik()
    for url in listings:
        # Podążaj za przekierowaniem -> aktualne ID kortu (do monitoringu i linku).
        lid = resolve_current_id(listing_id_from_url(url))
        canon_url = LISTING_PAGE_URL.format(id=lid)
        if g.book_url is None:
            g.book_url = canon_url
        # Krok 1: lekki ping (~1 KB) — licznik dostępności bez ciężkiego payloadu.
        # W ZRYWIE go pomijamy: oszczędza transfer, ale kosztuje całą rundę do serwera
        # (~110 ms), a pełne dane i tak są potrzebne po identyfikatory terminów —
        # i niosą te same atrybuty kortu, więc nic przez to nie tracimy.
        doc = None
        fetch_started = time.monotonic()
        seen_at = None
        if prefetched and prefetched[0] == lid:
            # Dane przyniósł sprint — pobieranie ich ponownie kosztowałoby całą
            # rundę do serwera (~92 ms) dokładnie w chwili, gdy liczy się najbardziej.
            doc = prefetched[1]
            attrs = (doc.get("data", {}).get("attributes", {}) or {})
            fetch_ms = 0
            # Sprint (albo Irlandia) niesie własny znacznik. Gdyby go zabrakło —
            # starsze wyniki, inna wersja po drugiej stronie — bierzemy chwilę
            # przejęcia danych; zaniża wiek, ale nigdy go nie zmyśla.
            seen_at = (prefetched[2] if len(prefetched) > 2 and prefetched[2] is not None
                       else time.monotonic())
        else:
            try:
                if skip_light:
                    doc = fetch_listing(lid)
                    attrs = (doc.get("data", {}).get("attributes", {}) or {})
                else:
                    attrs = (fetch_listing_light(lid).get("data", {}).get("attributes", {}) or {})
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                log(f"! Błąd pobierania kortu {lid}: {e} — nie zmieniam stanu, kończę.")
                g.blad = True
                return g
            seen_at = time.monotonic()
            fetch_ms = int((seen_at - fetch_started) * 1000)
        listing_price = attrs.get("price")
        title = attrs.get("title", lid)
        avail = (attrs.get("datesStats") or {}).get("availableListingDates") or 0
        if avail <= 0 and doc is None:
            # Brak jakichkolwiek wolnych terminów -> nie ma czego filtrować ani pobierać.
            log(f"= {title}: 0 dostępnych (lekki ping ~1 KB), pomijam pełne pobranie",
                level="info" if skip_light else "debug")
            continue
        # Krok 2: coś jest wolne -> dopiero teraz ciężki payload (~21 KB gzip) i filtr.
        if doc is None:
            try:
                doc = fetch_listing(lid)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                log(f"! Błąd pobierania terminów kortu {lid}: {e} — nie zmieniam stanu, kończę.")
                g.blad = True
                return g
            seen_at = time.monotonic()
        g.docs_by_lid[lid] = doc
        # Wiek danych liczymy od NAJSTARSZEJ obserwacji w tym biegu: jeśli któryś kort
        # pobraliśmy wcześniej, to jego terminy są odpowiednio starsze.
        if seen_at is not None:
            g.seen_at = seen_at if g.seen_at is None else min(g.seen_at, seen_at)
        slots = [s for s in free_slots(doc, lid, now_utc) if passes_filter(s, filters, tz)]
        # W zrywie dokładamy czas pobrania — pozwala oddzielić opóźnienie sieci
        # od opóźnienia wykrycia przy analizie logu po polowaniu.
        log(f"= {title}: {avail} dostępnych, {len(slots)} pasujących do filtra"
            + (f" (pobranie {fetch_ms} ms)" if skip_light else ""),
            level="info" if skip_light else "debug")
        g.meta_by_lid[lid] = (canon_url, listing_price)
        for s in slots:
            g.current[s["id"]] = s
            g.book_url_by_id[s["id"]] = canon_url
            g.listing_price_by_id[s["id"]] = listing_price
            g.lid_by_id[s["id"]] = lid
        for s in sorted(slots, key=lambda x: x["start_utc"]):
            log(f"   - {fmt_when(s['start_utc'].astimezone(tz), short=True)}  {s['name']}  {s['count']}/{s['limit']}",
                level="info" if skip_light else "debug")
    return g


def rejestruj_obserwujac(lid, sloty, ceny, cfg, registered, filters, tz, znane,
                         obserwuj=True):
    """Rejestruje `sloty`, patrząc RÓWNOLEGLE na to, co pojawia się w trakcie zapisu.

    Zwraca `(wyniki, registered, druga_fala, swiezy_dokument)`.

    Używane po OBU stronach — lokalnie i w Irlandii. Wcześniej obserwacja w trakcie
    zapisu istniała tylko lokalnie, a to Irlandia strzela w sekundzie publikacji.
    Jej okno ślepoty było krótsze (zapis ~100–200 ms zamiast ~1300 ms), ale publikacja
    sypie partiami co ~450 ms — więc i tam mieściła się cała partia.
    """
    obserwator = (obserwuj_podczas_zapisu(lid, filters, tz, znane)
                  if obserwuj and lid else None)
    # `auto_register_new_slots` liczy swój limit od zera przy KAŻDYM wywołaniu, a wołamy
    # je tu dwa razy. Bez odjęcia zdobyczy `auto_register_max: 2` dałoby cztery
    # rezerwacje — każda to zajęty kort i pieniądze.
    limit_calkowity = cfg.get("max_per_run", 1)
    try:
        limit_calkowity = max(0, int(limit_calkowity))
    except (TypeError, ValueError):
        limit_calkowity = 1
    przed = len(registered)
    try:
        wyniki, registered = auto_register_new_slots(sloty, ceny, cfg, registered)
    finally:
        if obserwator:
            obserwator[0].set()

    druga = obserwator[1]["nowe"] if obserwator else {}
    if not druga:
        return wyniki, registered, {}, None

    log(f"⇉ Druga fala: {len(druga)} "
        f"{plural(len(druga), 'termin', 'terminy', 'terminów')} pojawiło się "
        f"w trakcie zapisu ({obserwator[1]['pobran']} pobrań) — strzelam od razu")
    doc = obserwator[1]["doc"]
    cena = ((doc.get("data", {}).get("attributes", {}) or {}).get("price")
            if doc else None)
    ceny_fala = dict(ceny)
    ceny_fala.update({sid: cena for sid in druga})
    # Limit obejmuje OBIE fale. Zero znaczy: dalej tylko patrzymy.
    cfg["max_per_run"] = max(0, limit_calkowity - (len(registered) - przed))
    if cfg["max_per_run"] == 0:
        cfg["max_per_run"] = limit_calkowity
        log(f"= Limit {limit_calkowity} wykorzystany w pierwszej fali — "
            f"drugiej nie rezerwuję")
        return wyniki, registered, druga, doc
    # Strzelamy PRZED księgowaniem: dziennik, stan i powiadomienia kosztowały 03.09
    # kolejne ~470 ms, a to więcej, niż trwa cudzy zapis.
    cfg["seen_at"] = time.monotonic()
    try:
        wyniki_fala, registered = auto_register_new_slots(
            list(druga.values()), ceny_fala, cfg, registered)
    finally:
        # Przywracamy wartość wołającego. Pętla partii w Lambdzie i tak ustawia swoją
        # przed każdą partią, ale zostawianie po sobie okrojonego limitu w cudzej
        # konfiguracji to dokładnie ten rodzaj niespodzianki, który wraca po miesiącu.
        cfg["max_per_run"] = limit_calkowity
    wyniki.update(wyniki_fala)
    return wyniki, registered, druga, doc


def zarejestruj_z_obserwacja(grafik, kandydaci, reg_cfg, registered_ids, filters, tz,
                             skip_light):
    """Strona lokalna: mapuje `Grafik` na wspólny rdzeń i wnosi drugą falę z powrotem.

    Obserwujemy kort, który wydał terminy do rejestracji — nie ostatni z brzegu.
    Ciągłe pobieranie tylko w zrywie: poza nim publikacja nie sypie partiami.
    """
    reg_cfg["seen_at"] = grafik.seen_at
    lid_obs = next((grafik.lid_by_id[i] for i in kandydaci if i in grafik.lid_by_id), None)
    wyniki, registered_ids, druga, doc = rejestruj_obserwujac(
        lid_obs, [grafik.current[i] for i in kandydaci], grafik.listing_price_by_id,
        reg_cfg, registered_ids, filters, tz, set(grafik.current),
        obserwuj=bool(skip_light))
    if druga:
        url_obs, cena_obs = grafik.meta_by_lid.get(lid_obs, (grafik.book_url, None))
        for sid, s in druga.items():
            grafik.current[sid] = s
            grafik.book_url_by_id[sid] = url_obs
            grafik.listing_price_by_id[sid] = cena_obs
            grafik.lid_by_id[sid] = lid_obs
        if doc is not None:
            # Grafik dnia liczony z dokumentu sprzed publikacji dał 03.09 wpis
            # „4 wolne z 4", choć chwilę później kort miał ich jedenaście.
            grafik.docs_by_lid[lid_obs] = doc
    return wyniki, registered_ids, druga


def run_once(announce_startup=False, skip_light=False, prefetched=None, defer_push=False,
             remote=None):
    """Zwraca 0 przy powodzeniu, 2 przy błędzie sieci (stan nietknięty).

    `defer_push=True` (tylko w zrywie/sprincie) odkłada powiadomienia do kolejki
    zamiast wysyłać je od razu — patrz `flush_notifications`.

    `remote` (wynik z Irlandii) niesie rejestracje, które JUŻ SIĘ ODBYŁY. Ich ID
    dokładamy do zapisanych, zanim policzymy kandydatów — inaczej lokalna strona
    próbowałaby zarezerwować drugi raz to, co zdalna właśnie zajęła.
    """
    cfg = load_config()
    state_doc = load_state_doc()
    topic = opcja("NTFY_TOPIC", cfg, "ntfy_topic")
    reg_cfg = build_reg_cfg(cfg, state_doc)
    tzname = opcja("TIMEZONE", cfg, "timezone", "Europe/Warsaw")
    tz = ZoneInfo(tzname) if ZoneInfo else timezone.utc
    filters = resolve_filters(cfg)
    now_utc = datetime.now(timezone.utc)

    listings_env = os.environ.get("LISTINGS")
    if listings_env:
        listings = [u.strip() for u in re.split(r"[,\s]+", listings_env) if u.strip()]
    else:
        listings = cfg.get("listings", [])
    grafik = zbierz_terminy(listings, filters, tz, now_utc, skip_light, prefetched)
    if grafik.blad:
        return 2  # błąd sieci: stan zostaje nietknięty
    # Nazwy lokalne, żeby reszta funkcji czytała się jak dotąd. `grafik` jest jedynym
    # źródłem prawdy o tym, co widzieliśmy w tym biegu.
    current = grafik.current
    book_url = grafik.book_url
    book_url_by_id = grafik.book_url_by_id
    listing_price_by_id = grafik.listing_price_by_id
    lid_by_id = grafik.lid_by_id
    meta_by_lid = grafik.meta_by_lid
    docs_by_lid = grafik.docs_by_lid
    seen_at_all = grafik.seen_at

    current_ids = set(current.keys())
    prev = None if state_doc is None else set(state_doc.get("free_ids", []))

    # Powiadomienie startowe: przy uruchomieniu aplikacji oraz przy pierwszym biegu bez
    # zapisanego stanu — ale NIE CZĘŚCIEJ niż raz na godzinę. Proces wstaje przy każdym
    # restarcie Home Assistanta, aktualizacji dodatku, zadziałaniu watchdoga i po każdym
    # crashu. Dodatek w pętli restartów zasypywał telefon „✅ Monitor uruchomiony",
    # a push, który przychodzi bez powodu, uczy ignorowania wszystkich pozostałych.
    if (announce_startup or prev is None) and _wolno_powiadomic_o_starcie(state_doc):
        notify_startup(topic, len(current_ids), tz, book_url)
        start_push_iso = datetime.now(timezone.utc).isoformat()
    else:
        start_push_iso = (state_doc or {}).get("startup_push_at", "")

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
    if remote:
        registered_ids = registered_ids | remote["registered"]
        registration_results.update(remote["results"])

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
        wyniki_lokalne, registered_ids, druga_fala = zarejestruj_z_obserwacja(
            grafik, candidate_ids, reg_cfg, registered_ids, filters, tz, skip_light)
        # update, nie podstawienie: wyniki z Irlandii muszą przetrwać, bo to one
        # trafiają do powiadomienia jako „co udało się zarezerwować".
        registration_results.update(wyniki_lokalne)
        current_ids |= set(druga_fala)
        new_ids |= set(druga_fala)

    # Alert o tokenie: raz na incydent (kasowany, gdy token znów działa).
    auth_error = reg_cfg.get("auth_error")
    auth_alert_sent = bool(state_doc.get("auth_alert_sent"))
    auth_verified = bool(candidate_ids) or reg_cfg.get("auth_checked")
    auth_since = state_doc.get("auth_error_since")
    if auth_error:
        # KARENCJA: czytnik tokenu ma najpierw spróbować CICHEGO LOGOWANIA — po nieudanym
        # odczycie ponawia po 45 s, a samo odbicie przez OAuth trwa do ~20 s. Push wysłany
        # natychmiast trafiał więc do użytkownika, zanim aplikacja w ogóle spróbowała się
        # naprawić, i bardzo często okazywał się fałszywym alarmem.
        if not auth_since:
            auth_since = datetime.now(timezone.utc).isoformat()
            log(f"~ Problem z tokenem ({auth_error}) — daję cichemu logowaniu "
                f"{AUTH_ALERT_GRACE}s, zanim powiadomię.")
        elif not auth_alert_sent and _starszy_niz(auth_since, AUTH_ALERT_GRACE):
            notify_auth_problem(topic, auth_error, book_url)
            auth_alert_sent = True
    else:
        auth_since = None
        if auth_alert_sent and auth_verified and reg_cfg.get("enabled"):
            log("✓ Token Decathlon znów działa — kasuję alert.")
            auth_alert_sent = False

    if new_ids:
        log(f"NOWE wolne terminy: {len(new_ids)}")
        new_slots = sorted((current[i] for i in new_ids), key=lambda x: x["start_utc"])
        grid = log_day_grids(new_slots, docs_by_lid, now_utc, tz, registered_ids)
        # Dziennik polowań: jeden wpis na dobę z tym, co naprawdę się liczy.
        # Nie może wywrócić biegu — rezerwacje są już zrobione, historia to dodatek.
        try:
            # Strzały mogły paść po OBU stronach: zdalnie w Irlandii i lokalnie
            # z zapasu. Dziennik ma pokazać jedno i drugie.
            strzaly = list((remote or {}).get("shots") or []) + list(reg_cfg.get("shots") or [])
            record_hunt(datetime.now(tz), tz, new_slots, registration_results,
                        strzaly, grid, bool(remote), topic,
                        wykryto=(remote or {}).get("wykryto"))
        except Exception as e:  # noqa: BLE001
            log(f"! Nie zapisałem polowania do dziennika: {e!r}")
        # grupuj powiadomienia per listing (book_url)
        by_url = {}
        for s in new_slots:
            by_url.setdefault(book_url_by_id[s["id"]], []).append(s)
        for url, slots in by_url.items():
            cena = listing_price_by_id[slots[0]["id"]]
            if defer_push:
                # Slot trafia do stanu MIMO nieudanej jeszcze wysyłki: gdyby go pominąć,
                # następna iteracja uznałaby go za nowy i odłożyła DRUGĄ paczkę.
                # Ponawianie jest tu zadaniem kolejki, nie stanu.
                queue_notification((topic, slots, tz, cena, url, registration_results, 0))
            else:
                failed_ids |= notify_new(topic, slots, tz, cena, url, registration_results)
        if defer_push:
            log(f"📨 Powiadomienia odłożone na po zrywie "
                f"({len(by_url)} {plural(len(by_url), 'paczka', 'paczki', 'paczek')}) "
                f"— sprint wraca do patrzenia od razu.")
        if failed_ids:
            log(f"! Nie wysłano {len(failed_ids)} powiadomień — ponowię w następnej iteracji.")
    else:
        log("Brak nowych wolnych terminów.", level="debug")

    # Sloty z nieudaną wysyłką NIE trafiają do stanu -> następna iteracja
    # potraktuje je znów jako nowe i ponowi powiadomienie.
    save_state(
        current_ids - failed_ids,
        registered_ids,
        reg_cfg.get("token"),
        pending_ids=reg_cfg.get("pending_ids") or [],
        auth_alert_sent=auth_alert_sent,
        auth_error_since=auth_since or "",
        startup_push_at=start_push_iso,
        decathlon_rt=reg_cfg.get("refresh_token"),
    )
    return 0


class Nastawy:
    """Ustawienia polowania odczytane RAZ, przy starcie procesu.

    Wcześniej `main` parsowała to wszystko w miejscu — 60 linii przed pętlą, przez które
    trzeba było przejść, żeby zobaczyć, co właściwie robi polowanie. Osobno: każda z tych
    opcji ma inny sposób zawodzenia (zły format, przekroczony zakres, brak zależnej
    opcji), a wymieszane z pętlą główną te ścieżki błędu były nieczytelne.
    """

    __slots__ = ("tz", "tzname", "windows", "burst", "salvo_size", "hedge_size",
                 "warm_size", "warm_url", "first_listing", "remote_url", "remote_secret",
                 "sprint", "sprint_threads",
                 # Temat ntfy potrzebny kontroli sesji przed zrywem. Wcześniej `main`
                 # sięgała po `cfg_startowy` — zmienną, która przy wydzieleniu tej
                 # funkcji przeniosła się do jej wnętrza. Efekt: NameError co 30 s
                 # i kontrola sesji martwa przez cały dzień.
                 "topic")


def oglos_tryb_pracy():
    """Mówi WPROST, co dodatek będzie robił. Raz, przy starcie.

    Dwie opcje domyślne cicho wyłączają polowanie: `auto_register: false` (w ogóle nie
    rezerwuje) i `auto_register_dry_run: true` (sprawdza wszystko i nie rezerwuje nic).
    Druga jest groźniejsza, bo wygląda identycznie jak działające polowanie — jedyny
    ślad to linia „~ Auto-rejestracja (test, bez rezerwacji)", która pojawia się raz na
    dobę, w chwili publikacji, wśród tysiąca innych. Można stracić tydzień, zanim się
    zauważy, że nic nigdy nie zostało zarezerwowane.
    """
    cfg = load_config(quiet=True)
    wlaczona = boolish(os.environ.get("AUTO_REGISTER") or cfg.get("auto_register"))
    proba = boolish(os.environ.get("AUTO_REGISTER_DRY_RUN") or cfg.get("auto_register_dry_run"))
    if not wlaczona:
        log("= Auto-rejestracja WYŁĄCZONA — dostaniesz powiadomienie o wolnym terminie, "
            "ale rezerwacja jest po Twojej stronie (auto_register).")
        return
    if proba:
        log("! TRYB PRÓBNY: auto_register_dry_run=true — sprawdzam i loguję wszystko, "
            "ale NIE REZERWUJĘ NICZEGO. Ustaw auto_register_dry_run: false, żeby "
            "dodatek naprawdę zajmował korty.")
        return
    imie = opcja("AUTO_REGISTER_NAME", cfg, "auto_register_name")
    ile = opcja("AUTO_REGISTER_MAX", cfg, "auto_register_max", 1)
    kolejnosc = opcja("AUTO_REGISTER_ORDER", cfg, "auto_register_order", "earliest")
    # Cudzysłowy w apostrofach: polski znak zamykający zapisany jako ASCII " kończyłby
    # literał. Ten sam błąd zjadł już raz `read_token.py`.
    log(f'✓ Auto-rejestracja WŁĄCZONA: do {ile} '
        f'{plural(int(ile) if str(ile).isdigit() else 1, "terminu", "terminów", "terminów")} '
        f'na przebieg, kolejność „{kolejnosc}”, uczestnik „{imie or "BRAK IMIENIA"}”.')
    if not imie:
        log("! Brak auto_register_name — serwer odrzuci KAŻDĄ rezerwację. "
            "Wpisz imię i nazwisko uczestnika.")


def wczytaj_nastawy(interval):
    """Parsuje opcje z ENV/pliku i zwraca `Nastawy`. Błędna opcja WYŁĄCZA swoją funkcję,
    nigdy nie wywraca procesu — polowanie bez zrywu jest gorsze, ale wciąż polowaniem."""
    n = Nastawy()
    # Opcjonalne okna z inną częstotliwością (INTERVALS), w strefie TIMEZONE.
    cfg_startowy = load_config(quiet=True)
    tzname = opcja("TIMEZONE", cfg_startowy, "timezone", "Europe/Warsaw")
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
        try:
            hedge_size = max(1, min(int(os.environ.get("AUTO_REGISTER_HEDGE") or 1), HEDGE_MAX))
        except (TypeError, ValueError):
            hedge_size = 1
        # Ile gniazd musi być ciepłych: cała salwa plus dodatkowe kopie czołowego celu.
        warm_size = min(salvo_size + hedge_size - 1, SALVO_MAX + HEDGE_MAX - 1)
    except ValueError:
        salvo_size = 0
        # Bez tego `warm_size` w ogóle nie powstaje przy błędnej opcji. Dziś ratuje nas
        # tylko to, że `if salvo_size > 1` nie dopuszcza do użycia — czyli poprawność
        # zależy od strażnika stojącego sto linii dalej. Tak się nie pisze.
        hedge_size = warm_size = 1
    warm_url = ""
    first_listing = [u for u in re.split(r"[,\s]+", listings_z_konfiguracji(cfg_startowy))
                     if u.strip()]
    if first_listing:
        try:
            warm_url = LISTING_URL.format(id=listing_id_from_url(first_listing[0]))
        except Exception as e:  # noqa: BLE001
            # Literówka w adresie kortu wywracała CAŁY proces bez czytelnego powodu,
            # a w Home Assistancie znaczy to pętlę restartów bez wyjaśnienia. Każda inna
            # błędna opcja wyłącza tylko swoją funkcję — ta nie może być wyjątkiem.
            log(f"! Nie rozpoznaję adresu kortu '{first_listing[0]}' ({e}) — "
                f"sprint i rozgrzewka wyłączone. Popraw opcję listing_url.")
    if salvo_size > 1:
        log(f"⇉ Salwa włączona: do {salvo_size} prób rejestracji równolegle "
            f"(nadmiar ponad auto_register_max jest anulowany).")

    # Zdalny strzał z eu-west-1. Puste = wszystko dzieje się lokalnie, jak dotąd.
    remote_url = (os.environ.get("REMOTE_URL") or "").strip()
    remote_secret = (os.environ.get("REMOTE_SECRET") or "").strip()
    if remote_url and not remote_secret:
        log("! REMOTE_URL ustawione bez REMOTE_SECRET — adres funkcji byłby otwarty "
            "dla każdego, kto go pozna. Wyłączam zdalny strzał.")
        remote_url = ""
    if remote_url:
        log(f"☁ Zdalny strzał włączony: sprint i salwa lecą z {urllib.parse.urlsplit(remote_url).netloc}. "
            f"Gdy nie odpowie, poluję lokalnie.")

    # Sprint: wąskie okno pobierania bez przerw, wycelowane w samą sekundę publikacji.
    sprint = None
    sprint_env = os.environ.get("SPRINT", "")
    try:
        sprint_threads = max(1, min(int(os.environ.get("SPRINT_THREADS") or 3),
                                    SPRINT_MAX_THREADS))
    except ValueError:
        sprint_threads = 3
    if sprint_env.strip() and warm_url:
        try:
            sprint = parse_burst_env(sprint_env)   # ten sam format co burst
            # Ten sam sufit co w Lambdzie — zapas lokalny musi umieć pokryć to samo okno.
            sprint["seconds"] = max(1, min(int(os.environ.get("SPRINT_SECONDS") or 4), 60))
            hour, minute, second = sprint["at"]
            log(f"🏁 Sprint: {','.join(sprint['days'])} o {hour:02d}:{minute:02d}:{second:02d}, "
                f"przez {sprint['seconds']}s, {sprint_threads} wątków bez przerw "
                f"(świeży obraz co ~{max(1, round(117 / sprint_threads))} ms)")
        except Exception as e:  # noqa: BLE001 - błędna opcja nie może wywrócić monitora
            log(f"! Błędny SPRINT '{sprint_env}': {e} — sprint wyłączony")
            sprint = None


    n.tz, n.tzname, n.windows, n.burst = tz, tzname, windows, burst
    n.topic = opcja("NTFY_TOPIC", cfg_startowy, "ntfy_topic")
    n.salvo_size, n.hedge_size, n.warm_size = salvo_size, hedge_size, warm_size
    n.warm_url, n.first_listing = warm_url, first_listing
    n.remote_url, n.remote_secret = remote_url, remote_secret
    n.sprint, n.sprint_threads = sprint, sprint_threads
    return n


def wykonaj_sprint(n, granice, now_local, in_sprint):
    """Jedno okno sprintu: rozgrzewka, strzał z Irlandii, zapas lokalny.

    Zwraca `(prefetched, remote_result, in_sprint)`. Wydzielone z pętli głównej, w której
    zajmowało 62 linie i mieszało trzy odrębne sprawy: cykl życia okna, wywołanie zdalne
    i decyzję o zapasie lokalnym.
    """
    if not in_sprint:
        # Sprint to najbardziej czasowo-krytyczny moment całego polowania —
        # milisekundy w znacznikach są tu potrzebne nawet bez zrywu.
        set_log_precision(True)
        log(f"🏁 Sprint START — {n.sprint_threads} wątków bez przerw "
            f"do {granice[1]:%H:%M:%S}")
        in_sprint = True
        # Salwa strzela dopiero, gdy sprint coś znajdzie — a jej pula leży bezczynnie
        # od startu zrywu. UWAGA: „wystygnięte gniazda" jako wyjaśnienie wolnego
        # PIERWSZEGO strzału zostały obalone DWA RAZY: (1) pomiarem — połączenie
        # przeżywa 12 s bezczynności (66–70 ms), (2) produkcyjnie 9.08 — połączenia
        # odświeżono tu, 2,5 s przed strzałem, a pierwsza rejestracja i tak kosztowała
        # 319 ms wobec 57–84 ms następnych (8.08 identycznie: 294 ms wobec 73 ms).
        # Uwierzytelniona rozgrzewka (0.9.0) tego NIE naprawiła i została usunięta —
        # patrz `warm_connections`. Zostaje jako tanie ubezpieczenie na wypadek, gdyby
        # serwer jednak zamknął bezczynne gniazdo. Sprint swojej puli nie potrzebuje:
        # zaraz zacznie pobierać bez przerw.
        if n.salvo_size > 1:
            odswiezone = time.monotonic()
            warm_connections(salvo_pool(n.warm_size), n.warm_size, n.warm_url)
            log(f"⇉ Salwa odświeżona przed sprintem "
                f"[{int((time.monotonic() - odswiezone) * 1000)} ms]")

    szukanie = time.monotonic()
    zostalo = max(0.0, (granice[1] - now_local).total_seconds())
    deadline = szukanie + zostalo
    baseline = set((load_state_doc() or {}).get("free_ids") or [])
    prefetched = remote_result = None

    if n.remote_url:
        # ZDALNY STRZAŁ: sprint i salwa lecą z eu-west-1, obok serwera Decathlona.
        # Rejestracja odbywa się TAM, więc wynik trzeba przenieść do stanu tutaj.
        wynik, blad = call_remote(
            n.remote_url, n.remote_secret,
            remote_payload(n.first_listing[0], baseline, n.tzname, zostalo,
                           n.sprint_threads,
                           build_reg_cfg(load_config(quiet=True), load_state_doc())),
            timeout=zostalo + REMOTE_TIMEOUT_MARGIN)
        if blad:
            # ZAPAS: gdy Irlandia milczy, polujemy lokalnie. Gorzej, ale wciąż.
            # Jedyny groźny przypadek to timeout PO rejestracji — wtedy termin jest
            # zajęty, a my o tym nie wiemy, więc mówimy o tym głośno.
            log(f"! ☁ Zdalny sprint nieudany ({blad}) — poluję lokalnie.")
            if "brak odpowiedzi" in blad:
                log("! ☁ UWAGA: brak odpowiedzi NIE znaczy, że nic nie "
                    "zarezerwowano. Sprawdź panel Padel po polowaniu.")
        else:
            prefetched, remote_result = adopt_remote(wynik)

    # Zapas lokalny ma sens tylko przy SZYBKIEJ wpadce zdalnej (odmowa, zły adres, brak
    # sekretu) — te wracają w milisekundach i okno jeszcze trwa. Po timeoucie okna już
    # nie ma, a do tego zdalna strona mogła zarezerwować: dlatego wtedy nie strzelamy
    # powtórnie, tylko ostrzegamy (wyżej).
    if prefetched is None and remote_result is None and time.monotonic() < deadline:
        prefetched = run_sprint(deadline, n.sprint_threads, n.first_listing[0],
                                baseline, n.tz,
                                filters=resolve_filters(load_config(quiet=True)))
    if prefetched:
        log(f"🏁 Sprint: NOWE terminy wykryte po "
            f"{int((time.monotonic() - szukanie) * 1000)} ms — rejestruję z gotowych danych")
    return prefetched, remote_result, in_sprint


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

    ostrzez_o_temacie(opcja("NTFY_TOPIC", load_config(quiet=True), "ntfy_topic"))
    n = wczytaj_nastawy(interval)
    tz, windows, burst = n.tz, n.windows, n.burst
    salvo_size, warm_size, warm_url = n.salvo_size, n.warm_size, n.warm_url
    first_listing, tzname = n.first_listing, n.tzname
    remote_url, remote_secret = n.remote_url, n.remote_secret
    sprint, sprint_threads = n.sprint, n.sprint_threads

    oglos_tryb_pracy()
    log_rtt(urllib.parse.urlsplit(DECATHLON_API_URL).netloc)

    log(f"Tryb pętli: sprawdzam co {interval}s. Ctrl+C aby zakończyć.")
    first = True  # powiadomienie startowe na pierwszej UDANEJ iteracji procesu
    last_sleep = None
    in_burst = False
    burst_window = None   # granice trwającego zrywu — do uczciwego opisu w linii „koniec"
    in_sprint = False
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
                if warm_url and (salvo_size > 1 or sprint):
                    warmed = time.monotonic()
                    if salvo_size > 1:
                        warm_connections(salvo_pool(warm_size), warm_size, warm_url)
                    # Sprint ma WŁASNĄ pulę, więc jej połączenia trzeba rozgrzać osobno —
                    # inaczej pierwsze sekundy sprintu zjadłoby uzgadnianie TLS.
                    if sprint:
                        warm_connections(sprint_pool(), sprint_threads, warm_url)
                    log(f"⇉ Połączenia gotowe (salwa {salvo_size if salvo_size > 1 else 0}, "
                        f"sprint {sprint_threads if sprint else 0}) "
                        f"[{int((time.monotonic() - warmed) * 1000)} ms]")
                if remote_url:
                    # Zimny start Lambdy to ~165 ms na init plus ~75 ms na import
                    # silnika. Pukamy TERAZ, na starcie zrywu, żeby o 11:00:51
                    # funkcja była już ciepła — kontener żyje potem kilka minut.
                    zdalne = time.monotonic()
                    _, blad_warm = call_remote(remote_url, remote_secret, {"warm": True},
                                               timeout=REMOTE_TIMEOUT_MARGIN)
                    log(f"☁ Rozgrzewka Irlandii [{int((time.monotonic() - zdalne) * 1000)} ms]"
                        + (f" — NIEUDANA: {blad_warm}" if blad_warm else ""))
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
        # Kontrola sesji na pół godziny przed zrywem — 27.08 martwa sesja kosztowała
        # cztery wolne terminy, a dowiedzieliśmy się o tym dopiero po polowaniu.
        try:
            preflight_token(now_local, tz, n.topic,
                            LISTING_PAGE_URL.format(id=listing_id_from_url(first_listing[0]))
                            if first_listing else "")
        except Exception as e:  # noqa: BLE001 - kontrola nie może wywrócić pętli
            log(f"! Kontrola sesji nieudana: {e!r}")

        # SPRINT: przez kilka sekund wokół sekundy publikacji pobieramy BEZ PRZERW
        # kilkoma wątkami. Zwycięzca oddaje gotowe dane, więc rejestracja rusza od razu.
        prefetched = None
        remote_result = None
        sprint_bounds = burst_bounds(sprint, tz, now_local) if sprint else None
        if sprint_bounds and sprint_bounds[0] <= now_local < sprint_bounds[1] and warm_url:
            prefetched, remote_result, in_sprint = wykonaj_sprint(
                n, sprint_bounds, now_local, in_sprint)
        elif in_sprint:
            log("🏁 Sprint koniec")
            in_sprint = False
            set_log_precision(active)   # milisekundy zostają tylko, jeśli trwa zryw

        try:
            rc = run_once(announce_startup=first, skip_light=active,
                          prefetched=prefetched, defer_push=active or in_sprint,
                          remote=remote_result)
            if rc != 2:  # 2 = błąd sieci; ponów próbę startowego powiadomienia później
                first = False
        except Exception as e:  # noqa: BLE001 - pętla ma przetrwać każdy błąd
            log(f"! Nieoczekiwany błąd w iteracji: {e!r} — kontynuuję.")
        # Spłukanie kolejki dopiero POZA zrywem i sprintem. Stoi za try/except, więc
        # odłożone powiadomienia wyjdą nawet wtedy, gdy iteracja padła albo nie było
        # nowych terminów — inaczej wisiałyby w pamięci do końca procesu.
        if not (active or in_sprint):
            flush_notifications()
        # Czas pracy odejmujemy od uśpienia: bez tego ustawione 2s dawały realnie ~2,4s.
        elapsed = time.monotonic() - started
        sleep_s = plan_sleep(interval, windows, burst, tz, elapsed, sprint=sprint)
        if not active and windows:
            shown = current_interval(interval, windows, tz)
            if shown != last_sleep:
                log(f"⏱ aktualny interwał: {shown}s", level="debug")
                last_sleep = shown
        time.sleep(sleep_s)


if __name__ == "__main__":
    sys.exit(main())
