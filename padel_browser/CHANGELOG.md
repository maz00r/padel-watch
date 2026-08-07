# Changelog

## 0.6.0 — salwa: równoległe strzały o wieczorne godziny
- **Nowa opcja `auto_register_salvo`** (domyślnie 4): najbardziej pożądane terminy
  dostają próbę rejestracji **naraz**, każdy własnym rozgrzanym połączeniem.
  Zmierzone na żywym API: 4 strzały po kolei to 275 ms, salwą **73 ms** (3,8×).
  Przy kolejności `latest` czwarty termin dostawał dotąd strzał ~350 ms po pierwszym —
  i właśnie w tym oknie ginęły godziny 17:00–20:00 (potwierdzone logami z 5 i 7.08).
- **Limit `auto_register_max` nadal obowiązuje**: gdy salwa wygra więcej terminów,
  nadmiarowe są natychmiast anulowane, a zostaje ten najwyżej w `auto_register_order`.
  Oddane terminy trafiają na listę „już zapisane", żeby nie wpaść w pętlę
  rezerwuj–anuluj–zobacz-wolne–rezerwuj.
- **Połączenia salwy rozgrzewane na starcie zrywu** (~250 ms, z zapasem mieszczącym się
  w ośmiu sekundach do publikacji). Bez tego pierwszy strzał płaciłby ~160 ms za
  uzgodnienie TLS — czyli dokładnie to, co salwa ma wyeliminować.
- Terminy spoza salwy (np. bezpieczne 15:00) są nadal próbowane po kolei zaraz po niej,
  więc zabezpieczenie „przynajmniej cokolwiek" zostaje.
- Domyślne `auto_register_order` zmienione na `latest` (dotyczy tylko nowych instalacji).

## 0.5.1 — Dziennik z milisekundami w zrywie (diagnostyka wyścigu)
- **Znaczniki czasu z milisekundami na czas zrywu** (poza nim bez zmian). Sekundowa
  rozdzielczość przestała wystarczać: nie dało się odczytać, czy termin przegrywamy
  o 100 ms czy o 900 ms — a to zupełnie inne wnioski.
- **Czas pobrania danych** dopisywany do podsumowania kortu w zrywie (`pobranie 104 ms`) —
  oddziela opóźnienie sieci od opóźnienia wykrycia.
- **Czas każdej próby rejestracji** w jej linii (`[118 ms]`) — widać, ile kosztuje
  nieudany strzał i po jakim czasie od wykrycia poszedł zwycięski.
- Linia `⚡ Zryw koniec` podaje teraz **faktyczne granice okna**; leci przy następnym
  sprawdzeniu, więc bez tego sugerowałaby, że zryw skończył się kilka sekund później.
- Zmiany dotyczą wyłącznie logowania — działanie monitora i auto-rejestracji bez zmian.

## 0.5.0 — zryw: polowanie na sekundę publikacji grafiku
- **Nowy `burst`**: krótkie, gęste sprawdzanie wycelowane w moment publikacji grafiku
  (u nas zmierzone ~11:00:53, powtarzalnie co do sekundy przez dwa dni). Domyślnie
  `mon-sun:11:00:45`, 15 s co 0,5 s. Pętla dosypia **dokładnie** do startu zrywu,
  żeby go nie przespać.
- **Podtrzymane połączenie HTTPS** dla pobierania terminów i zapytania rejestrującego.
  Każde nowe połączenie to uzgodnienie TCP i TLS — zmierzone ~110 ms dla lekkiego pingu
  i ~200 ms dla pełnych danych, czyli więcej niż sam transfer (21 KB po kompresji).
- **W zrywie pomijany jest lekki ping** — kosztował całą rundę do serwera, a pełne dane
  niosą te same atrybuty kortu, więc nic przez to nie tracimy.
- **Naprawiony takt pętli**: uśpienie było doklejane PO pracy, więc ustawione 2 s dawało
  realnie ~2,4 s (widoczne w logu użytkownika). Teraz czas pracy jest odejmowany.
- Zmierzony efekt łącznie: cykl sprawdzenia **427 ms → 78 ms**, opóźnienie wykrycia
  z ~1,2 s do ~0,25 s. Przy tym zryw pozwala wyłączyć agresywne `intervals`:
  ~50 zapytań zamiast ~478 w oknie 10:50–11:10.

## 0.4.2 — ukrywanie anulowanych rezerwacji
- **Anulowane rezerwacje są domyślnie ukryte** — po anulowaniu termin znika z listy
  zamiast zostawać jako wyszarzona karta. Przełącznik **anulowane** (obok **minione**)
  przywraca je na widok.
- **Panel pamięta ustawienie obu filtrów** (`localStorage`), więc nie trzeba ich
  klikać przy każdym wejściu.
- Gdy filtry ukryją wszystko, panel mówi ile i czym — zamiast sugerować, że rezerwacji
  w ogóle nie ma.

## 0.4.1 — panel odporny na dołek odnowy tokenu
- **Naprawiony czerwony błąd w panelu mimo udanej operacji.** Panel nie miał osłony,
  którą monitor dostał w 0.3.2: strona odnawia JWT dopiero PO wygaśnięciu, więc
  kliknięcie w tym kilkunastosekundowym dołku kończyło się komunikatem „sesja
  odrzucona", choć sesja żyła (potwierdzone logiem użytkownika: anulowanie przeszło,
  a odświeżenie listy tuż po nim — nie). Teraz po HTTP 401 panel czeka do ~24 s na
  token odnowiony przez przeglądarkę i ponawia zapytanie.
- **Błędy panelu trafiają do Dziennika.** Wcześniej czerwony komunikat w interfejsie
  nie zostawiał żadnego śladu w logu i nie dało się dojść, co się stało.
- **Koniec ze spamem `(brak /data/__none__.json — używam wartości z ENV)`** — ta linia
  leciała przy każdej iteracji monitora i każdym zapytaniu panelu. Teraz raz na proces.

## 0.4.0 — moje rezerwacje: podgląd, anulowanie, kalendarz
- **Nowa zakładka „Rezerwacje" w panelu**: lista wszystkich rezerwacji z konta Decathlon GO
  (data, godziny, kort, adres, uczestnicy, stan), a przy każdej nadchodzącej dwa przyciski:
  **Dodaj do kalendarza** (plik `.ics` — telefon proponuje dodanie, z przypomnieniem 60 min
  przed) i **Anuluj rezerwację** (po potwierdzeniu; dodatek nigdy nie anuluje sam).
  Przycisk **Wszystkie do kalendarza** pobiera jeden plik ze wszystkimi terminami.
- **Panel serwuje teraz `panel.py`** (zakładki Rezerwacje / Przeglądarka). Ingress udostępnia
  jeden port, więc panel serwuje też pliki noVNC, a ruch websocketu przepuszcza do
  websockify (przeniesionego na lokalny 6080). Obraz z Chromium ładuje się dopiero po
  wejściu w zakładkę „Przeglądarka" — wcześniej strumień szedł zawsze.
- Odczyt rezerwacji korzysta z `users.getMe` + `transactions.list`, anulowanie z
  `transactions.cancel`. Odpowiedzi czytane są defensywnie (płaskie i `attributes`),
  a zakładka **Diagnostyka** pokazuje surową odpowiedź serwera.
- Uwaga: po anulowaniu auto-rejestracja **nie zajmie tego terminu ponownie** (jest na
  liście „już zapisane"). Żeby mogła — `clear_state: registered`.

## 0.3.2 — dołek odnowy tokenu bez fałszywych alarmów
- **Karencja na dołek odnowy** (`BROWSER_RENEW_GRACE`, 3 min): strona odnawia token
  dopiero PO wygaśnięciu, więc kilkanaście sekund „martwego" tokenu co ~15 min to norma.
  Wcześniej każdy cykl odnowy potrafił wysłać fałszywy push „⚠️ Token wygasł" i dwa
  wpisy `! Podtrzymanie sesji … nieudane` (widoczne w logu użytkownika z 0.3.1).
  Alarm pojawia się teraz dopiero, gdy token leży martwy dłużej niż karencja.
- **Rejestracja w dołku odnowy nie oddaje terminu walkowerem**: po HTTP 401 monitor
  czeka do ~24 s na świeży token z przeglądarki (plik) i ponawia — zamiast od razu
  meldować porażkę i odkładać termin do następnej iteracji.
- Czytnik tokenu: po nieudanym odczycie (błąd CDP, wygasły token, czekanie na
  logowanie) ponawia po ~45 s zamiast po pełnym `read_interval` — szybciej domyka
  dołek i mieści się w karencji monitora nawet po pojedynczej wpadce.
- Log czytnika pokazuje sekundy dla krótkich czasów (`jeszcze ~19 s`) zamiast
  mylącego `~0 min`.

## 0.3.1 — tryb przeglądarki: poprawki po pełnym przeglądzie projektu
- **Monitor nie próbuje już serwerowego `/auth/refresh`** (w trybie przeglądarki zawsze
  zwracał 401) — znika zalew `! Podtrzymanie sesji … nieudane` co iterację. Przy HTTP 401
  podczas rejestracji monitor zamiast tego **czyta plik tokenu ponownie** (przeglądarka
  mogła właśnie odnowić) i ponawia próbę.
- **Naprawiony fałszywy alarm „token wygasł"**: margines 5 min (`TOKEN_EXPIRY_MARGIN`)
  ma sens tylko dla proaktywnego refreshu, którego w trybie przeglądarki nie ma — strona
  odnawia token dopiero po wygaśnięciu. Wcześniej token z <5 min życia był uznawany za
  wygasły, co **blokowało rejestrację ważnym tokenem przez ~1/3 czasu**.
- **Komunikaty trybu przeglądarki rozpoznawane jako błąd auth** (`AUTH_FAILURE_MARKERS`) —
  bez tego nieudana rejestracja nie zapamiętywała terminu do ponowienia po zalogowaniu.
- **Wygrywa najświeższy token niezależnie od źródła** (plik z przeglądarki / opcja
  `decathlon_token` / stan) — wcześniej stary token z pliku zasłaniał ręcznie wklejony świeży.
- **Czytnik tokenu nie przeszkadza w panelu**: nie przeładowuje strony gdy token ważny
  ani gdy trwa logowanie (nie wyrywa formularza w trakcie wpisywania kodu z maila).
- **Adaptacyjny harmonogram odczytu**: czytnik budzi się tuż po wygaśnięciu tokenu
  (strona odnawia go dopiero wtedy), więc plik tokenu jest nieświeży najwyżej kilkanaście
  sekund — wcześniej do 5 min, co groziło utratą gorącego terminu.
- Wygasły token logowany jako `⚠ … WYGASŁ … sprawdź panel` zamiast mylącego
  `✓ JWT odczytany (jeszcze ~-3 min)`; plik tokenu nie jest nadpisywany wygasłym.
- Czytnik preferuje kartę z `go.decathlon.pl` (użytkownik może otworzyć inne zakładki).
- **Chromium w pętli z autorestartem** — crash przeglądarki nie zostawia dodatku ślepym.
- Alert ntfy o tokenie radzi teraz „otwórz panel Padel i zaloguj się" zamiast
  „wklej go-sdk-jwt"; README opisuje tryb przeglądarki jako główną drogę.

## 0.3.0 — scalenie: przeglądarka + monitor w jednym dodatku
- **Dodatek `padel_browser` przejmuje silnik monitora** (`check_padel.py`, dawniej osobny
  `padel_watch`). Teraz jeden dodatek: przeglądarka do logowania **oraz** monitorowanie i
  auto-rejestracja. Osobny `padel_watch` został usunięty (dwa równoległe dodatki groziły
  podwójnymi rezerwacjami).
- **Auto-rejestracja bez wklejania tokenu.** PoC potwierdził (2026-07-21): po jednorazowym
  ręcznym zalogowaniu w panelu sesja utrzymuje się na serwerze, a strona sama odnawia
  `go-sdk-jwt`. Przeglądarka (`read_token.py`) zapisuje świeży token do `/data/token.json`,
  monitor go stąd czyta (`DECATHLON_TOKEN_FILE`). `decathlon_token` w opcjach jest teraz
  tylko awaryjnym obejściem.
- Domyślny `read_interval` obniżony do 300 s, by token w pliku był zawsze świeższy niż
  margines wygaśnięcia (żywotność JWT ~15 min).
- `boot: auto` — dodatek wstaje po restarcie HA; profil Chromium w `/data` przeżywa restart,
  więc logowanie jest potrzebne tylko po faktycznym wygaśnięciu sesji.

## 1.15.0
- **Usunięta opcja `decathlon_sso_cookie` (wprowadzona w 1.14.0) — ta droga nie działa
  i jest szkodliwa.** Odtwarzanie logowania SSO z serwera (`SESSION` → `authorize` →
  `code` → `token`) Decathlon traktuje jako logowanie z nowego urządzenia i **wysyła kod
  weryfikacyjny na e-mail**. To celowa kontrola bezpieczeństwa (step-up auth), której nie
  należy obchodzić. Zostawienie opcji oznaczałoby generowanie alertów bezpieczeństwa na
  koncie użytkownika przy każdej próbie.
- Dokumentacja mówi teraz prawdę zamiast obietnicy: **JWT żyje ~15 min i NIE da się go
  odnowić** (`/api/auth/refresh` zwraca 401 nawet dla żywego tokenu — sprawdzone na
  tokenie z 4 min życia). Aplikacja webowa nigdy nie prosi o refresh token, więc
  `go-unsafe-rt` nie istnieje i nie ma czego wysłać.
- Praktyczne użycie auto-rezerwacji: wklej świeży `go-sdk-jwt` tuż przed oknem, w którym
  polujesz. Monitorowanie i powiadomienia ntfy działają non-stop i nie zależą od tokenu.

## 1.13.0
- **`test_token` sprawdza teraz token PRAWDZIWYM zapytaniem do API**, a nie tylko
  lokalnym odczytem `exp` z JWT. Wcześniej `✓ token OK` znaczyło jedynie „token się
  parsuje i ma przyszłą datę ważności" — serwer nigdy nie był pytany, więc komunikat
  dawał fałszywy spokój przy tokenie, którego API nie akceptuje.
- Nowy `verify_decathlon_token()`: uwierzytelniony GET `/api/user-consent/my-consents`
  (bez skutków ubocznych). 200 = działa, 401/403 = odrzucony, błąd sieci = „nie wiadomo"
  (nie wywołuje fałszywego alertu).
- Log rozróżnia trzy stany: `✓ token DZIAŁA — serwer potwierdził`,
  `✗ serwer ODRZUCIŁ token (HTTP 401)`, `? nie zweryfikowałem tokenu (sieć)`.

## 1.12.2
- **Naprawa: token gnił podczas ciszy i pierwsza okazja przepadała.** Sesja była
  odnawiana tylko przy starcie dodatku albo gdy było co rezerwować. Po dłuższym okresie
  bez wolnych terminów JWT wygasał, a `/auth/refresh` wygasłego tokenu zwraca **401**
  (sesja ślizgowa — odnawia się ŻYWY token). Efekt: gdy termin w końcu się pojawiał,
  auto-rejestracja padała na `nie udało się odświeżyć tokenu: HTTPError 401`.
  Teraz sesja jest podtrzymywana w **każdej iteracji** (bez ruchu sieciowego, dopóki
  do wygaśnięcia jest zapas).
- `TOKEN_EXPIRY_MARGIN` podniesiony z 60 s do **300 s** — margines musi być wyraźnie
  większy niż `check_interval`, inaczej token wygasa między jednym a drugim sprawdzeniem.

## 1.12.1
- Alert „Token wygasł" mówi teraz wprost: wklej świeży `go-sdk-jwt` w `decathlon_token`
  (wcześniej odsyłał do nieaktualnego `decathlon_cookie`).
- Usunięto martwą zmienną `DECATHLON_REFRESH_TOKEN` (nie była eksportowana w run.sh
  ani obecna w config.yaml); rotowany `rt` i tak przychodzi z serwera i żyje w stanie.

## 1.12.0
- **Poprawka modelu uwierzytelniania: wystarczy sam `decathlon_token` (`go-sdk-jwt`).**
  Decathlon GO trzyma auth w `localStorage`, a NIE w ciasteczku sesji — w nagłówku
  `Cookie` są wyłącznie Google Analytics/Hotjar. Wcześniejsze `decathlon_cookie` było
  oparte na błędnym założeniu i nie mogło działać.
- Refresh odwzorowuje teraz wywołanie aplikacji Decathlona: `Authorization: Bearer
  <obecny jwt>` + `unsafeRefreshToken` w body (gdy `go-unsafe-rt` istnieje). Poświadczeniem
  jest sam JWT, więc odświeżanie działa **bez cookie**.
- Rotowany refresh token (`rt` z odpowiedzi) jest zapamiętywany w `state.json` i odsyłany
  przy kolejnym odświeżeniu.
- Czytelniejsze komunikaty: `brak tokenu Decathlon GO (wklej go-sdk-jwt w decathlon_token)`
  oraz `token odrzucony (HTTP 401) — wklej świeży go-sdk-jwt`.
- `decathlon_cookie` zostaje jako opcja awaryjna, ale zwykle jest niepotrzebne.

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
