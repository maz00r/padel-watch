#!/usr/bin/env python3
"""Zbieracz tokenów kont dodatkowych — jedna przeglądarka naraz, po kolei.

Konto główne ma własną, stale otwartą przeglądarkę i nic tu nie zmieniamy. Konta
dodatkowe dostają profile na dysku, a ten proces odwiedza je po kolei na DRUGIM ekranie
(`:2`, CDP 9223), czyta token i zabija przeglądarkę.

## Kiedy uruchamiamy konta dodatkowe

To jest sedno całego harmonogramu i najłatwiejsza rzecz do pomylenia. Strona Decathlon GO
odnawia JWT **dopiero po jego wygaśnięciu** — udokumentowane w `read_token.next_sleep`
i `check_padel.BROWSER_RENEW_GRACE`. Wejście na stronę z żywym tokenem oddaje ten sam
token, więc jest czystą stratą czasu i procesora.

Konta dodatkowe służą wyłącznie do codziennego rzutu o godzinie skonfigurowanej
w `burst`. Zbieracz zaczyna preflight 30 minut przed startem, a końcowy obchód
układa tak, żeby JWT obejmowały całe polowanie. W ostatnich 90 sekundach i podczas
zrywu nie uruchamia Chromium. Konto główne ma osobny, stale działający mechanizm
i przez całą dobę obsługuje zwykły monitoring.

W aktywnym oknie odwiedzamy tylko konto z wygasłym tokenem. Strona Decathlon GO
odnawia JWT dopiero po jego wygaśnięciu; wcześniejsza wizyta oddałaby ten sam token.

## Czego zbieracz NIE robi

Nie wpisuje loginu ani hasła. Pierwsze zalogowanie każdego konta jest ręczne, przez panel
— sesja zostaje w profilu i przeżywa restarty, tak jak na koncie głównym.
"""

import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - obraz dodatku ma współczesnego Pythona
    ZoneInfo = None

# Ile sekund przed publikacją przestajemy uruchamiać przeglądarkę. Start Chromium plus
# załadowanie SPA to ~15-25 s; próba rozpoczęta później i tak by nie zdążyła, a zabrałaby
# procesor monitorowi dokładnie wtedy, gdy jest mu najbardziej potrzebny.
STOP_PRZED_PUBLIKACJA = 90
# Dziewięć profili otwieranych kolejno potrzebuje zwykle około 3–4 minut. Zaczynamy
# wcześniej, żeby wykryć wylogowane profile i zostawić użytkownikowi czas na reakcję.
PRZYGOTOWANIE_PRZED_PUBLIKACJA = 30 * 60
# JWT Decathlon GO żyje zwykle około 15 minut. Pierwszy obchód o 10:30 jest tylko
# kontrolny: gdybyśmy odnawiali ponownie natychmiast po wygaśnięciu około 10:45,
# kolejna fala tokenów wygasałaby właśnie podczas publikacji o 11:00. Dlatego po
# preflight czekamy z końcowym obchodem do chwili, w której nowy JWT obejmie całe
# polowanie oraz ten zapas.
OCZEKIWANA_WAZNOSC_JWT = 15 * 60
MARGINES_PO_POLOWANIU = 30
# Najkrótszy odstęp między dwiema wizytami u TEGO SAMEGO konta. Chroni przed pętlą
# dobijania się do konta, które jest wylogowane i nigdy nie odda tokenu.
COOLDOWN = 120
# Ile czasu dajemy jednej wizycie, zanim uznamy ją za nieudaną.
LIMIT_WIZYTY = 60
# Przy ręcznym logowaniu dajemy czas na hasło i kod z maila. Przeglądarka pozostaje
# otwarta wyłącznie przez ten czas albo do chwili pojawienia się ważnego JWT.
LIMIT_LOGOWANIA = 10 * 60
STATE_DIR = os.environ.get("STATE_DIR") or "/data"
STATUS_PATH = os.environ.get("HARVEST_STATUS_PATH") or os.path.join(STATE_DIR, "token-harvest.json")
LOGIN_REQUEST_PATH = os.environ.get("LOGIN_REQUEST_PATH") or os.path.join(
    STATE_DIR, "token-login-request.json")
EXTRA_DISPLAY = os.environ.get("EXTRA_DISPLAY") or ":2"
EXTRA_CDP_PORT = int(os.environ.get("EXTRA_CDP_PORT") or 9223)
START_URL = os.environ.get("START_URL") or "https://go.decathlon.pl"
PETLA_SLEEP = 10
_auto_login_stany = {}


def wczytaj_status(sciezka):
    """Stan zbiórki: kiedy ostatnio odwiedziliśmy konto i do kiedy żyje jego token."""
    try:
        with open(sciezka, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def nastepne_konto(status, konta, teraz, publikacja=None,
                   cooldown=COOLDOWN, stop_przed=STOP_PRZED_PUBLIKACJA,
                   przygotowanie=PRZYGOTOWANIE_PRZED_PUBLIKACJA):
    """Które konto odwiedzić TERAZ. None, gdy nie ma czego robić.

    Kolejność pilności:
      1. konta, o których nic nie wiemy (brak wpisu w statusie) — trzeba sprawdzić,
         czy w ogóle są zalogowane,
      2. konta z WYGASŁYM tokenem, od najdawniej wygasłego.

    Konta z żywym tokenem pomijamy świadomie: strona i tak oddałaby ten sam token.
    """
    if not okno_kont_dodatkowych(teraz, publikacja, przygotowanie):
        return None
    quiet = cisza_przed_publikacja(
        teraz, publikacja, stop_przed + LIMIT_WIZYTY)
    # Twarda cisza: Chromium nie startuje ani tuż przed publikacją, ani w trakcie
    # zrywu. Poprzednio wyjątek dla wygasłej sesji powodował dokładnie odwrotny efekt:
    # dziewięć tokenów odnawianych od 10:30 wpadało w trzeci cykl około 11:00.
    if quiet:
        return None
    poczatek_przygotowania = publikacja - przygotowanie
    kandydaci = []
    for konto in konta:
        if konto.get("main"):
            continue                     # konto główne ma własną, stałą przeglądarkę
        wpis = status.get(konto["id"]) or {}
        if teraz - (wpis.get("odwiedzone") or 0) < cooldown:
            continue
        exp = wpis.get("exp")
        if exp is None:
            kandydaci.append((0, float("-inf"), konto))   # nic nie wiemy — najpilniejsze
        elif exp <= teraz:
            # Profil sprawdzony już w dzisiejszym preflight dostał token około 30 min
            # przed publikacją. Po jego pierwszym wygaśnięciu NIE odnawiamy od razu:
            # czekamy na końcowe uzbrojenie, aby kolejny JWT nie wygasł o 11:00.
            odwiedzone = wpis.get("odwiedzone") or 0
            sprawdzony_dzis = (odwiedzone >= poczatek_przygotowania
                               and not wpis.get("blad"))
            uzbrojenie_od = start_finalnego_uzbrojenia(
                publikacja, wpis.get("ttl") or OCZEKIWANA_WAZNOSC_JWT)
            if sprawdzony_dzis and teraz < uzbrojenie_od:
                continue
            kandydaci.append((1, exp, konto))             # wygasł, im dawniej tym pilniej
    if not kandydaci:
        return None
    kandydaci.sort(key=lambda k: (k[0], k[1]))
    return kandydaci[0][2]


def cisza_przed_publikacja(teraz, publikacja, stop_przed=STOP_PRZED_PUBLIKACJA):
    try:
        after = max(1, min(int(os.environ.get("BURST_SECONDS") or 75), 120))
    except ValueError:
        after = 75
    return publikacja is not None and -after < publikacja - teraz <= stop_przed


def _czas_okna(nazwa, domyslnie, maksimum):
    try:
        return max(1, min(int(float(os.environ.get(nazwa) or domyslnie)), maksimum))
    except (TypeError, ValueError):
        return domyslnie


def wymagany_exp(publikacja, burst_seconds=None, sprint_seconds=None,
                 margines=MARGINES_PO_POLOWANIU):
    """Najwcześniejszy bezpieczny `exp`: koniec dłuższego okna plus zapas."""
    burst = (_czas_okna("BURST_SECONDS", 75, 120)
             if burst_seconds is None else max(1, min(int(burst_seconds), 120)))
    sprint = (_czas_okna("SPRINT_SECONDS", 50, 60)
              if sprint_seconds is None else max(1, min(int(sprint_seconds), 60)))
    return publikacja + max(burst, sprint) + margines


def start_finalnego_uzbrojenia(publikacja, ttl=OCZEKIWANA_WAZNOSC_JWT):
    """Od tej chwili odnowiony JWT będzie ważny przez całe polowanie."""
    try:
        ttl = int(float(ttl))
    except (TypeError, ValueError):
        ttl = OCZEKIWANA_WAZNOSC_JWT
    # Uszkodzony status nie może przesunąć obchodu poza publikację ani o wiele godzin.
    ttl = max(5 * 60, min(ttl, 30 * 60))
    return wymagany_exp(publikacja) - ttl


def okno_kont_dodatkowych(teraz, publikacja,
                          przygotowanie=PRZYGOTOWANIE_PRZED_PUBLIKACJA):
    """Pętla rozpatruje profile tylko wokół rzutu; osobny bezpiecznik blokuje zryw."""
    if publikacja is None:
        return False
    try:
        after = max(1, min(int(os.environ.get("BURST_SECONDS") or 75), 120))
    except ValueError:
        after = 75
    return publikacja - przygotowanie <= teraz < publikacja + after


def zapisz_status(sciezka, status, zapis=None):
    if zapis is None:
        from check_padel import zapisz_json_atomowo as zapis
    return zapis(sciezka, status)


def odnotuj(status, kid, exp=None, blad="", user_id="", teraz=None):
    """Wynik jednej wizyty. Zawsze zapisuje `odwiedzone` — także po niepowodzeniu,
    inaczej cooldown by nie działał i zbieracz dobijałby się do wylogowanego konta."""
    wpis = dict(status.get(kid) or {})
    wpis.pop("aktywne", None)
    odwiedzone = int(teraz if teraz is not None else time.time())
    wpis["odwiedzone"] = odwiedzone
    if exp:
        wpis["exp"] = int(exp)
        wpis["ttl"] = max(0, int(exp) - odwiedzone)
        wpis["blad"] = ""
    if blad:
        wpis["blad"] = blad
    if user_id:
        wpis["user_id"] = user_id
    status[kid] = wpis
    return status


def tozsamosc_sie_zgadza(status, kid, user_id):
    """Czy token należy do TEGO konta.

    Najgroźniejszy cichy błąd wielokontowości: pomyłka w mapowaniu profil → konto daje
    dziesięć tokenów JEDNEGO konta. Wygląda jak działający system, a przynosi jeden kort
    dziennie zamiast kilku — i nic nie krzyczy. Pierwszy odczyt zapamiętuje `user_id`,
    każdy kolejny musi się z nim zgadzać.
    """
    if not user_id:
        return True                      # nie udało się sprawdzić — nie blokujemy zbiórki
    znany = (status.get(kid) or {}).get("user_id")
    return not znany or znany == user_id


def ile_zywych(status, konta, na_kiedy):
    """Ile kont będzie miało ważny token o danej chwili. Do Dziennika i alarmów —
    bez tej liczby nie da się odróżnić „wielokontowość nie pomaga" od „strzelały trzy
    konta z dziesięciu, bo reszta miała martwe tokeny"."""
    ile = 0
    for konto in konta:
        if konto.get("main"):
            ile += 1                     # konto główne odnawia się samo, w swojej pętli
            continue
        exp = (status.get(konto["id"]) or {}).get("exp")
        if exp and exp > na_kiedy:
            ile += 1
    return ile


def wczytaj_json(sciezka):
    try:
        with open(sciezka, encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def profil_konta(konto, state_dir=None):
    """Trwały profil Chromium jednego konta dodatkowego."""
    return os.path.join(state_dir or STATE_DIR, f"chrome-profile-{konto['id']}")


def komenda_chromium(konto, cdp_port=EXTRA_CDP_PORT, start_url=START_URL, state_dir=None):
    return [
        "chromium-browser",
        "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-gpu", "--disable-gpu-compositing",
        "--enable-logging=stderr", "--log-level=3",
        f"--user-data-dir={profil_konta(konto, state_dir)}",
        "--disk-cache-size=1", "--media-cache-size=1",
        f"--remote-debugging-port={cdp_port}", "--remote-allow-origins=*",
        "--window-position=0,0", "--window-size=1280,900",
        "--no-first-run", "--no-default-browser-check",
        start_url,
    ]


def uruchom_chromium(konto, display=EXTRA_DISPLAY, cdp_port=EXTRA_CDP_PORT,
                     start_url=START_URL, state_dir=None):
    profil = profil_konta(konto, state_dir)
    os.makedirs(profil, exist_ok=True)
    env = dict(os.environ)
    env["DISPLAY"] = display
    return subprocess.Popen(
        komenda_chromium(konto, cdp_port, start_url, state_dir),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def zatrzymaj_chromium(proc):
    """Kończy całe drzewo Chromium, nie tylko proces rodzica."""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _user_id(konto, jwt):
    """Sprawdza właściciela tokenu bez ratunku plikiem innego/starego konta."""
    import check_padel
    cfg = {
        "token": jwt,
        "browser_mode": False,
        "token_file": konto.get("token_file"),
    }
    user_id, _token = check_padel.decathlon_my_user_id(cfg, jwt)
    return user_id


def czekaj_na_token(konto, status, proc, reczne=False, limit=None,
                    odczyt=None, zapis=None, identyfikator=None,
                    teraz=None, sen=None, cdp_url=None, start_url=START_URL):
    """Czeka na ważny JWT otwartego profilu i zapisuje go do pliku konta.

    W trybie ręcznym tylko obserwuje stronę. To ważne: cykliczna nawigacja podczas
    wpisywania hasła albo kodu z maila wyrywałaby użytkownikowi formularz spod palców.
    """
    import read_token

    odczyt = odczyt or read_token.read_jwt_once
    zapis = zapis or read_token.write_token_file
    identyfikator = identyfikator or _user_id
    teraz = teraz or time.time
    sen = sen or time.sleep
    limit = limit if limit is not None else (LIMIT_LOGOWANIA if reczne else LIMIT_WIZYTY)
    cdp_url = cdp_url or f"http://127.0.0.1:{EXTRA_CDP_PORT}"
    deadline = teraz() + limit
    ostatni_blad = "brak tokenu"
    while teraz() < deadline:
        if proc.poll() is not None:
            ostatni_blad = f"Chromium zakończył się (kod {proc.poll()})"
            break
        try:
            jwt, exp, blad = odczyt(
                cdp_url=cdp_url,
                start_url=start_url,
                navigate=not reczne,
            )
        except Exception as e:  # noqa: BLE001 - jedna wizyta nie może ubić zbieracza
            jwt, exp, blad = None, 0, f"błąd odczytu: {e!r}"
        if jwt and exp and exp > teraz():
            user_id = ""
            try:
                user_id = identyfikator(konto, jwt) or ""
            except Exception as e:  # noqa: BLE001 - JWT nadal może być prawidłowy
                log(f"! {konto['id']}: nie sprawdziłem tożsamości ({e!r})")
            if not tozsamosc_sie_zgadza(status, konto["id"], user_id):
                znany = (status.get(konto["id"]) or {}).get("user_id")
                ostatni_blad = f"profil zwrócił inne konto ({user_id}, oczekiwano {znany})"
                odnotuj(status, konto["id"], blad=ostatni_blad, teraz=teraz())
                return False, ostatni_blad
            zapisany = zapis(jwt, exp, path=konto["token_file"])
            if zapisany is False:
                ostatni_blad = f"nie zapisałem {konto['token_file']}"
                odnotuj(status, konto["id"], blad=ostatni_blad, teraz=teraz())
                return False, ostatni_blad
            odnotuj(status, konto["id"], exp=exp, user_id=user_id, teraz=teraz())
            return True, ""
        ostatni_blad = blad or "token jest pusty albo wygasł"
        sen(min(3, max(0, deadline - teraz())))
    odnotuj(status, konto["id"], blad=ostatni_blad, teraz=teraz())
    return False, ostatni_blad


def log(*args):
    print("[zbieracz]", *args, flush=True)


def odwiedz_konto(konto, status, reczne=False, limit=None,
                  uruchom=None, zatrzymaj=None, **czekaj_kw):
    """Uruchamia dokładnie jeden profil, zbiera token i zawsze sprząta Chromium."""
    uruchom = uruchom or uruchom_chromium
    zatrzymaj = zatrzymaj or zatrzymaj_chromium
    wpis = dict(status.get(konto["id"]) or {})
    wpis["aktywne"] = True
    status[konto["id"]] = wpis
    proc = None
    import read_token
    poprzedni_stan = read_token.stan_cichego_logowania()
    read_token.przywroc_stan_cichego_logowania(
        _auto_login_stany.get(konto["id"], (0, 0.0)))
    try:
        log(f"otwieram konto {konto['id']}{' do logowania' if reczne else ''}")
        proc = uruchom(konto)
        return czekaj_na_token(konto, status, proc, reczne=reczne, limit=limit, **czekaj_kw)
    except Exception as e:  # noqa: BLE001 - kolejne konta nadal muszą dostać swoją kolej
        blad = f"nie uruchomiłem Chromium: {e!r}"
        odnotuj(status, konto["id"], blad=blad)
        return False, blad
    finally:
        _auto_login_stany[konto["id"]] = read_token.stan_cichego_logowania()
        read_token.przywroc_stan_cichego_logowania(poprzedni_stan)
        zatrzymaj(proc)


def _zapisz_json(sciezka, doc):
    from check_padel import zapisz_json_atomowo
    zapisz_json_atomowo(sciezka, doc)


def publikacja_dzis(check_padel, teraz=None):
    nazwa = os.environ.get("TIMEZONE") or "Europe/Warsaw"
    try:
        tz = ZoneInfo(nazwa) if ZoneInfo else timezone.utc
    except Exception:  # noqa: BLE001 - błędna strefa nie może ubić zbieracza
        tz = timezone.utc
    now = datetime.fromtimestamp(teraz if teraz is not None else time.time(), tz)
    start = check_padel.burst_start_today(now, tz)
    return start.timestamp() if start else None


def _konto_z_zadania(konta, doc):
    # `active` zostaje na dysku tylko wtedy, gdy poprzedni proces padł w trakcie wizyty.
    # Nowy proces bezpiecznie otwiera ten sam trwały profil jeszcze raz.
    if doc.get("state") not in ("pending", "active"):
        return None
    kid = str(doc.get("id") or "")
    return next((k for k in konta if k["id"] == kid and not k.get("main")), None)


def main():
    import check_padel

    status = wczytaj_status(STATUS_PATH)
    log(f"start: ekran {EXTRA_DISPLAY}, CDP {EXTRA_CDP_PORT}, stan {STATUS_PATH}")
    while True:
        try:
            cfg = check_padel.load_config(quiet=True)
            konta = check_padel.konta_z_konfiguracji(cfg)
            dodatkowe = [k for k in konta if not k.get("main")]
            if not dodatkowe:
                time.sleep(60)
                continue

            zadanie = wczytaj_json(LOGIN_REQUEST_PATH)
            konto = _konto_z_zadania(konta, zadanie)
            if zadanie.get("state") in ("pending", "active") and konto is None:
                zadanie.update(state="done", ok=False, error="nieznane konto", finished=int(time.time()))
                _zapisz_json(LOGIN_REQUEST_PATH, zadanie)
                time.sleep(PETLA_SLEEP)
                continue
            if konto:
                teraz = time.time()
                publikacja = publikacja_dzis(check_padel, teraz)
                # Nie zaczynaj dziesięciominutowego logowania tak późno, żeby mogło
                # pozostać otwarte w krytycznym oknie polowania.
                if cisza_przed_publikacja(
                        teraz, publikacja, STOP_PRZED_PUBLIKACJA + LIMIT_LOGOWANIA):
                    time.sleep(PETLA_SLEEP)
                    continue
                zadanie.update(state="active", started=int(time.time()), error="")
                _zapisz_json(LOGIN_REQUEST_PATH, zadanie)
                ok, blad = odwiedz_konto(konto, status, reczne=True)
                zapisz_status(STATUS_PATH, status)
                zadanie.update(state="done", ok=ok, error=blad, finished=int(time.time()))
                _zapisz_json(LOGIN_REQUEST_PATH, zadanie)
                log(f"{konto['id']}: {'token zapisany' if ok else blad}")
                continue

            teraz = time.time()
            konto = nastepne_konto(status, konta, teraz,
                                    publikacja=publikacja_dzis(check_padel, teraz))
            if konto:
                ok, blad = odwiedz_konto(konto, status)
                zapisz_status(STATUS_PATH, status)
                log(f"{konto['id']}: {'token odnowiony' if ok else blad}")
            else:
                time.sleep(PETLA_SLEEP)
        except Exception as e:  # noqa: BLE001 - proces ma przeżyć także wadliwą konfigurację
            log(f"! błąd pętli: {e!r}")
            time.sleep(PETLA_SLEEP)


if __name__ == "__main__":
    main()
