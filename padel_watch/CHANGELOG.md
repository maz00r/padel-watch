# Changelog

## 1.7.0
- **Nowa opcja `auto_register_order`** (`earliest` | `latest`): kolejność prób zapisu.
  `latest` = zaczyna od najpóźniejszego wolnego terminu. Domyślnie `earliest`.
- **Nowa opcja `clear_state`** (`registered` | `all`): jednorazowe wyczyszczenie stanu.
  `registered` kasuje listę zapisanych terminów (śledzone terminy i token zostają),
  `all` czyści cały `state.json` razem z zapisanym tokenem. Działa **raz** — kolejne
  restarty z tą samą wartością nie czyszczą ponownie (znacznik `clear_state_applied`).

## 1.6.0
- **Bezpiecznik: `auto_register_max` (domyślnie 1)** — auto-rejestracja nigdy nie
  rezerwuje hurtem całego grafiku. Wcześniej pojawienie się np. 39 nowych wolnych
  terminów oznaczało próbę zapisu na wszystkie naraz.
- Zapis zaczyna od **najwcześniejszego** pasującego terminu; reszta czeka na kolejny
  przebieg (logowana zbiorczo).
- Twardy błąd autoryzacji (brak/odrzucony token) **przerywa przebieg** zamiast
  ponawiać żądania dla każdego slotu z osobna (koniec dobijania się do API i spamu
  w logach: 39 linii -> 1).
- Testy bezpieczników (limit, kolejność, przerwanie po auth, tryb speculative).

## 1.5.2
- Auto-rejestracja potrafi odświeżyć krótkotrwały JWT Decathlon GO przez
  `/api/auth/refresh`, jeśli podano cookie sesji w `decathlon_cookie`.
- Odświeżony JWT jest zapisywany w stanie dodatku, więc nie trzeba co kilkanaście
  minut ręcznie aktualizować `decathlon_token` w Home Assistant.
- Token wklejony z prefixem `Bearer`, `JWT:` albo cudzysłowami jest automatycznie
  czyszczony przed użyciem.

## 1.5.1
- Poprawka auto-rejestracji: request do `/api/v2/transactions.create` wysyła teraz
  payload w polu `input`, zgodnie z formatem RPC Decathlon GO.

## 1.5.0
- Opcjonalna automatyczna rejestracja na nowe terminy Decathlon GO (`auto_register`).
  Wymaga aktualnego JWT `go-sdk-jwt` i danych uczestnika; domyślnie obsługuje tylko
  darmowe terminy, a płatne pozostawia do ręcznego dokończenia płatności.
- Domyślny tryb testowy `auto_register_dry_run`, który waliduje zapis przez
  `/api/v2/transactions.create`, ale nie tworzy rezerwacji.
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
