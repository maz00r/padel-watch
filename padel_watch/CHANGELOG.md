# Changelog

## Unreleased
- Opcjonalna automatyczna rejestracja na nowe terminy Decathlon GO (`auto_register`).
  Wymaga aktualnego JWT `go-sdk-jwt` i danych uczestnika; domyślnie obsługuje tylko
  darmowe terminy, a płatne pozostawia do ręcznego dokończenia płatności.
- Stan przechowuje także `registered_ids`, żeby nie ponawiać udanej rejestracji
  na ten sam termin.

## 1.4.0
- **Nowa opcja `intervals`**: inna częstotliwość odświeżania w zadanych godzinach,
  np. `mon-fri:15:00-02:00=30` (co 30 s wieczorem, poza oknem wg `check_interval`).
  Format jak `filters` + `=SEKUNDY`; okna przez północ działają; minimum 10 s.
- Niezgubione alerty: gdy wysyłka ntfy zawiedzie, termin nie trafia do stanu
  i powiadomienie jest ponawiane w następnej iteracji.
- Porządki repozytorium: to teraz wyłącznie repo dodatku HA (usunięte pliki
  Docker/Portainer/GitHub Actions cron i zdublowana kopia silnika).
- Testy jednostkowe silnika + CI na GitHub Actions.

## 1.3.1
- Każda linia logu ma znacznik czasu `[RRRR-MM-DD GG:MM:SS]` w strefie z opcji
  `timezone` (domyślnie Europe/Warsaw; przy błędnej nazwie fallback do UTC).

## 1.3.0
- Link do kortu nie jest już „na sztywno": aplikacja podąża za przekierowaniem
  strony `/l/{id}` (Decathlon czasem przenosi kort pod nowe ID) i używa AKTUALNEGO
  ID zarówno do monitoringu, jak i do linku w powiadomieniu. Dzięki temu app sam
  nadąża za zmianą adresu (wynik cache'owany ~6 h).

## 1.2.2
- Wysyłka ntfy jest teraz NIEBLOKUJĄCA: błąd (np. HTTP 404) nie wywraca iteracji
  ani nie blokuje zapisu stanu (koniec pętli powtarzających się powiadomień).
- Log pokazuje status i treść błędu z ntfy — łatwiej zdiagnozować zły temat.
- Sanityzacja tematu (przycięcie spacji; gdy wklejono pełny URL, brany jest sam temat).

## 1.2.1
- Poprawka: jawna instalacja `python3` w obrazie (HA buduje na bazie alpine+s6 bez
  pythona) — naprawia `python3: not found` i puste opcje przy starcie.
## 1.2.0
- Pierwsza wersja jako dodatek Home Assistant.
- Konfiguracja z UI: temat ntfy, interwał, godziny (FILTERS), link kortu, strefa czasowa.
- Powiadomienie przy każdym starcie dodatku; link do rezerwacji w każdym powiadomieniu.
- Okna godzin przez północ (np. 15:00–02:00). Stan trwały w /data.
