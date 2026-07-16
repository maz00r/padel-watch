# Changelog

## 1.11.0
- **Nowa opcja `test_token`**: test poświadczeń Decathlon GO **bez wolnego terminu**.
  Przy starcie app próbuje pobrać token i loguje wynik wraz z datą ważności
  (`✓ Test poświadczeń: token OK, ważny do ... (jeszcze ~118 min)`). Nic nie rezerwuje.
- Ten sam test wykonuje się automatycznie przy każdym starcie, gdy `auto_register`
  jest włączone — od razu wiesz, czy cookie jeszcze żyje.
- Nieudany test wysyła alert ntfy i jest rozpoznawany jak zwykły błąd auth.

## 1.10.0
- **`decathlon_token` jest teraz opcjonalny — wystarczy `decathlon_cookie`.** App sam
  pobiera JWT z `/api/auth/refresh` (to cookie uwierzytelnia refresh, nie token).
  Wcześniej bez wklejonego tokenu auto-rejestracja w ogóle nie ruszała ("brak tokenu"),
  więc refresh nigdy nie miał szansy zadziałać.
- **Proaktywne odświeżanie**: token jest odnawiany na podstawie `exp` (z zapasem 60 s),
  zanim wygaśnie — zamiast czekać na 401 i marnować żądanie. Fallback po 401 zostaje.
- `/api/auth/refresh` nie wysyła pustego nagłówka `Authorization`, gdy nie mamy tokenu.
- Czytelniejsze błędy: `brak tokenu Decathlon GO i brak decathlon_cookie` oraz
  `nie udało się pobrać tokenu cookiem: ...` (oba przerywają przebieg jak błąd auth).

## 1.9.0
- **Minimalny interwał w `intervals` obniżony z 10 s do 2 s** — pozwala na agresywne
  „snajpowanie" w wąskim oknie (np. `mon-fri:10:45-11:15=2`).
- Podbicie do minimum jest teraz **widoczne w logu** (`żądano 1s — używam 2s`), zamiast
  po cichu ignorować ustawienie.
- Interwał poniżej 5 s loguje ostrzeżenie z liczbą zapytań/h — poniżej 2 s realnie
  ryzykujesz blokadę po IP, dlatego zostaje twardy limit.

## 1.8.0
- **Alert ntfy, gdy token/cookie Decathlon przestanie działać** ("⚠️ Token Decathlon
  wygasł") — raz na incydent, kasowany gdy token znów działa. Wcześniej o awarii
  auto-rezerwacji dowiadywałeś się tylko z logów HA.
- **Ponawianie po naprawie tokenu:** termin, którego nie udało się zarezerwować przez
  błąd autoryzacji, jest zapamiętywany (`pending_ids`) i ponawiany w kolejnych
  przebiegach — także gdy nie ma już "nowych" terminów. Wcześniej taki termin był
  trwale pomijany, mimo że dalej był wolny.
- Zapamiętywane jest maksymalnie tyle terminów, ile i tak zapisałby `auto_register_max`,
  więc naprawa tokenu nie powoduje hurtowego nadrabiania zaległości.
- Monitorowanie i powiadomienia o wolnych terminach nigdy nie zależą od tokenu.

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
