#!/usr/bin/env python3
"""Zbieracz tokenów kont dodatkowych — jedna przeglądarka naraz, po kolei.

Konto główne ma własną, stale otwartą przeglądarkę i nic tu nie zmieniamy. Konta
dodatkowe dostają profile na dysku, a ten proces odwiedza je po kolei na DRUGIM ekranie
(`:2`, CDP 9223), czyta token i zabija przeglądarkę.

## Dlaczego odwiedzamy tylko konta z WYGASŁYM tokenem

To jest sedno całego harmonogramu i najłatwiejsza rzecz do pomylenia. Strona Decathlon GO
odnawia JWT **dopiero po jego wygaśnięciu** — udokumentowane w `read_token.next_sleep`
i `check_padel.BROWSER_RENEW_GRACE`. Wejście na stronę z żywym tokenem oddaje ten sam
token, więc jest czystą stratą czasu i procesora.

Zbieracz czeka więc, aż token konkretnego konta umrze, i dopiero wtedy je odwiedza —
dokładnie tak, jak `read_token.py` robi to od miesiąca dla konta głównego.

Rachunek: token żyje 15 minut, więc dziesięć kont to jedno odnowienie co 90 sekund.
Przy wizycie trwającej ~20 s zbieracz jest zajęty 22% czasu. Reszta to czekanie.

## Czego zbieracz NIE robi

Nie wpisuje loginu ani hasła. Pierwsze zalogowanie każdego konta jest ręczne, przez panel
— sesja zostaje w profilu i przeżywa restarty, tak jak na koncie głównym.
"""

import json
import os
import time

# Ile sekund przed publikacją przestajemy uruchamiać przeglądarkę. Start Chromium plus
# załadowanie SPA to ~15-25 s; próba rozpoczęta później i tak by nie zdążyła, a zabrałaby
# procesor monitorowi dokładnie wtedy, gdy jest mu najbardziej potrzebny.
STOP_PRZED_PUBLIKACJA = 90
# Najkrótszy odstęp między dwiema wizytami u TEGO SAMEGO konta. Chroni przed pętlą
# dobijania się do konta, które jest wylogowane i nigdy nie odda tokenu.
COOLDOWN = 120
# Ile czasu dajemy jednej wizycie, zanim uznamy ją za nieudaną.
LIMIT_WIZYTY = 60


def wczytaj_status(sciezka):
    """Stan zbiórki: kiedy ostatnio odwiedziliśmy konto i do kiedy żyje jego token."""
    try:
        with open(sciezka, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def nastepne_konto(status, konta, teraz, publikacja=None,
                   cooldown=COOLDOWN, stop_przed=STOP_PRZED_PUBLIKACJA):
    """Które konto odwiedzić TERAZ. None, gdy nie ma czego robić.

    Kolejność pilności:
      1. konta, o których nic nie wiemy (brak wpisu w statusie) — trzeba sprawdzić,
         czy w ogóle są zalogowane,
      2. konta z WYGASŁYM tokenem, od najdawniej wygasłego.

    Konta z żywym tokenem pomijamy świadomie: strona i tak oddałaby ten sam token.
    """
    if publikacja is not None and 0 <= publikacja - teraz <= stop_przed:
        return None                      # cisza przed publikacją, procesor dla monitora
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
            kandydaci.append((1, exp, konto))             # wygasł, im dawniej tym pilniej
    if not kandydaci:
        return None
    kandydaci.sort(key=lambda k: (k[0], k[1]))
    return kandydaci[0][2]


def zapisz_status(sciezka, status, zapis=None):
    if zapis is None:
        from check_padel import zapisz_json_atomowo as zapis
    return zapis(sciezka, status)


def odnotuj(status, kid, exp=None, blad="", user_id="", teraz=None):
    """Wynik jednej wizyty. Zawsze zapisuje `odwiedzone` — także po niepowodzeniu,
    inaczej cooldown by nie działał i zbieracz dobijałby się do wylogowanego konta."""
    wpis = dict(status.get(kid) or {})
    wpis["odwiedzone"] = int(teraz if teraz is not None else time.time())
    if exp:
        wpis["exp"] = int(exp)
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
