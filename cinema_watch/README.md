# Kino (Cinema City) — dodatek Home Assistant

Pilnuje repertuaru wybranego filmu w Cinema City i wysyła **push przez ntfy.sh**,
gdy pojawią się **nowe terminy** — czy to nowy dzień w repertuarze, czy dołożony
seans w dniu, który już znasz.

Dodatek jest lekki: czysty Python bez zależności, bez przeglądarki i bez logowania
(repertuar Cinema City jest publiczny). To osobny dodatek niż **Padel** — możesz
mieć włączone oba albo tylko jeden.

## Instalacja

1. W HA: **Ustawienia → Dodatki → Sklep z dodatkami**. Jeśli nie masz jeszcze tego
   repozytorium: **⋮ → Repozytoria** → dodaj `https://github.com/maz00r/padel-watch`.
2. Odśwież sklep → **Kino (Cinema City)** → **Zainstaluj**.
3. **Konfiguracja** → wklej `film_url` i temat ntfy → **Zapisz**.
4. **Info** → **Uruchom**. Postęp zobaczysz w zakładce **Dziennik**.

## Skąd wziąć `film_url`

1. Wejdź na [cinema-city.pl](https://www.cinema-city.pl), otwórz stronę filmu.
2. Wybierz **miasto** (albo konkretne kino) w widoku kupowania biletów.
3. Skopiuj adres z paska przeglądarki — musi zawierać `in-cinema=...`.

Przykład:

```
https://www.cinema-city.pl/filmy/odyseja/7460s2r#/buy-tickets-by-film?in-cinema=warszawa&for-movie=7460s2r
```

Dodatek sam wyciągnie z niego identyfikator filmu (`7460s2r`) i miejsce (`warszawa`).
Miejscem może być **miasto** (wszystkie kina w mieście) albo **id pojedynczego kina**
(liczba, np. `1074`).

> Zły identyfikator filmu **nie zwraca błędu** — Cinema City odpowiada pustą listą.
> Dlatego przy zerowym repertuarze dodatek wypisuje ostrzeżenie `⚠ Brak jakichkolwiek
> seansów` zamiast milczeć. Jeśli je widzisz, sprawdź link.

## Opcje

| Opcja | Znaczenie | Przykład |
|-------|-----------|----------|
| `ntfy_topic` | temat ntfy (ten sam, co subskrybujesz w apce) | `moj-temat-kino` |
| `film_url` | link do filmu z wybranym miastem/kinem (patrz wyżej) | `https://www.cinema-city.pl/filmy/odyseja/7460s2r#/...` |
| `check_interval` | co ile sekund sprawdzać (300–86400) | `3600` |
| `cinemas` | tylko wybrane kina; nazwy po przecinku, fragment wystarczy. Puste = wszystkie | `Arkadia, Mokotów` |
| `days_ahead` | jak daleko w przyszłość patrzeć (dni) | `365` |
| `timezone` | strefa czasowa dat w logach i powiadomieniach | `Europe/Warsaw` |
| `clear_state` | `all` = zapomnij zapamiętane seanse i zacznij od nowa | `` |

Repertuar kin zmienia się zwykle **raz dziennie**, więc `check_interval: 3600`
(co godzinę) jest z zapasem. Częściej nie ma sensu — to tylko ruch na serwerze kina.

## Powiadomienia

Przy pierwszym uruchomieniu: **„🎬 Monitoruję: odyseja"** z informacją, ile seansów
jest teraz w repertuarze. To punkt odniesienia — o istniejących seansach nie dostaniesz
alertu, tylko o tym, co dojdzie później.

Potem, gdy pojawią się nowe terminy:

```
🎬 odyseja: nowe terminy
🆕 piątek 07.08 — 57 seansów, 10:00–21:45
➕ wt 04.08: 21:15 Sadyba, 21:45 Sadyba
```

- **🆕** = dzień, którego wcześniej w repertuarze nie było (zbiorczo: ile seansów i w jakich godzinach),
- **➕** = seanse dołożone w dniu, który już znaliśmy (wypisane pojedynczo, do 6 sztuk).

Kliknięcie powiadomienia otwiera stronę filmu.

## Zachowanie w razie problemów

- **Cinema City nieosiągalne** → wpis w Dzienniku, stan **nietknięty**, ponowna próba
  w kolejnym biegu. Żaden termin nie zostanie przez to pominięty.
- **Nieudana wysyłka ntfy** → stan **nie jest zapisywany**, więc w kolejnym biegu te
  same seanse zostaną wykryte ponownie i powiadomienie pójdzie jeszcze raz. Alert nie ginie.
- **Minione dni** znikają ze stanu automatycznie (API ich nie zwraca).

## Rozwój

Silnik: [check_cinema.py](check_cinema.py) — jeden plik, tylko stdlib.
Testy: `python3 -m unittest -v test_check_cinema` (uruchamiane też w CI przy każdym PR).
