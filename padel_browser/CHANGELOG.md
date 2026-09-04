# Changelog

## 0.24.1 — NameError zabijał kontrolę sesji przed zrywem

Zaraz po wydaniu 0.24.0 w logu produkcyjnym pojawiło się:

```
! Kontrola sesji nieudana: NameError("name 'cfg_startowy' is not defined")
```

— i powtarzało się **co 30 sekund**. Kontrola sesji przed zrywem, czyli jedyne
zabezpieczenie przed martwym tokenem o 11:00, była martwa.

**Przyczyna to zderzenie dwóch zmian z dwóch sesji.** 0.23.0 wprowadziło `cfg_startowy`
wewnątrz `main()`. 0.24.0 wydzieliło z `main()` funkcję `wczytaj_nastawy` — zmienna
przeniosła się do jej wnętrza, a jedno użycie zostało na zewnątrz. Temat ntfy jedzie
teraz w `Nastawy.topic`.

**Dlaczego nic tego nie złapało — i co z tym zrobiono:**

- `py_compile` sprawdza składnię, a ta była poprawna,
- `main()` nie ma pokrycia testami, bo to nieskończona pętla,
- błąd siedział w gałęzi odpalanej **raz na dobę**, pół godziny przed zrywem.

Nowy `sprawdz_nazwy.py` — statyczny strażnik nieokreślonych nazw na samej bibliotece
standardowej. Rozumie zasięgi zagnieżdżone, argumenty, importy, `global`/`nonlocal`
i `except ... as`. Wpięty do CI **przed** sprawdzeniem składni i do zestawu testów,
razem z testem dowodzącym, że łapie dokładnie ten błąd, dla którego powstał.

Przy okazji sprawdzone dwie inne klasy kolizji między zmianami: zgodność sygnatur
z wywołaniami między plikami (bez zastrzeżeń) i klucze stanu pisane przez jedną zmianę,
a czytane przez drugą (bez zastrzeżeń).

## 0.24.0 (ciąg dalszy) — audyt powiadomień z perspektywy użytkownika

Poprzedni audyt sprawdzał, czy kod jest poprawny. Nie zadał najprostszego pytania:
**kiedy każdy komunikat leci, jak często i czy jest prawdziwy.** Problem z pchnięciami
o tokenie zgłosił użytkownik, nie ja. Ten przebieg nadrabia całą tę kategorię.

### Sprint dało się PRZESPAĆ w całości

`plan_sleep` znało tylko zryw. Przy domyślnej konfiguracji (sprint 11:00:05–11:00:45,
zryw dopiero 11:00:45) pętla budziła się o 10:59:50, liczyła sen „dokładnie do startu
zrywu" i spała 55 s — **przez całe okno sprintu**. Irlandia nie była wywoływana ani razu,
publikacja przechodziła bokiem, a w logu nie było śladu, bo z punktu widzenia pętli
wszystko poszło zgodnie z planem.

Zamaskowane tylko dlatego, że u użytkownika zryw startuje o tej samej sekundzie co
sprint. Wystarczyłoby przesunąć zryw później, żeby zdalny strzał cicho przestał istnieć.

### Kontrola sesji przed zrywem alarmowała natychmiast — i nigdy nie odwoływała alarmu

`preflight_token` wysyłał push o **najwyższym priorytecie** (`urgent`, `rotating_light`)
przy pierwszym niepowodzeniu, nie dając cichemu logowaniu żadnej szansy. Gorzej:
znacznik „sprawdzone na dziś" ustawiał się **przed** sprawdzeniem, więc gdy sesja wracała
minutę później, użytkownik nie dostawał już nic — jechał do komputera na darmo.

- Pierwsze niepowodzenie jest **ciche**; kontrola czuwa dalej, aż do startu zrywu.
- Push idzie dopiero po `AUTH_ALERT_GRACE` (120 s).
- **Powrót sesji po alarmie jest ogłaszany** („✅ Sesja wróciła").
- Znaczniki są ważne tylko dziś — bez tego nierozwiązany problem z wczoraj kazałby dziś
  rano odpytywać API w KAŻDEJ iteracji aż do zrywu.

### „✅ Monitor uruchomiony" przy każdym starcie procesu

Proces wstaje przy restarcie Home Assistanta, aktualizacji dodatku, zadziałaniu watchdoga
i po każdym crashu. Pętla restartów zasypywała telefon, a push przychodzący bez powodu
uczy ignorowania wszystkich pozostałych. Teraz najwyżej raz na godzinę, ze znacznikiem
w stanie — żeby dławik przeżył restart.

### Tryb próbny nie mówił o sobie

`auto_register_dry_run: true` jest **wartością domyślną**: dodatek sprawdza wszystko
i nie rezerwuje niczego. Wygląda identycznie jak działające polowanie — jedyny ślad to
linia pojawiająca się raz na dobę wśród tysiąca innych. Można stracić tydzień, zanim się
zauważy.

Przy starcie leci teraz jedna linia mówiąca wprost, co dodatek zrobi:

```
! TRYB PRÓBNY: auto_register_dry_run=true — … NIE REZERWUJĘ NICZEGO.
✓ Auto-rejestracja WŁĄCZONA: do 2 terminów na przebieg, kolejność „latest”, uczestnik „Jan Kowalski”.
! Brak auto_register_name — serwer odrzuci KAŻDĄ rezerwację.
```

## 0.24.0 — rozbicie wielkich funkcji, wspólny rdzeń rejestracji, testy panelu

Refaktor z przeglądu kodu. **Zachowanie nie zmienia się w żadnym miejscu** — zmienia się
to, ile trzeba przeczytać, żeby cokolwiek zrozumieć.

| funkcja | przed | po |
|---------|-------|-----|
| `run_once` | 267 linii, 67 rozgałęzień | **152 / 34** |
| `main` | 258 linii, 66 rozgałęzień | **123 / 31** |
| `auto_register_new_slots` | 228 linii, 51 rozgałęzień | **181 / 39** |

Powód nie jest estetyczny. Pięć błędów rodzaju „etykieta twierdziła więcej, niż mówiły
dane" (0.14.1, 0.16.1, 0.18.0, 0.19.1, 0.20.2) wyszło dopiero z produkcji — bo w funkcji
na 260 linii nie widać, że dwie listy powstają z różnych zbiorów.

Nowe jednostki, każda testowalna bez udawania połowy aplikacji:

- **`zbierz_terminy` → `Grafik`** — obchód kortów. Jedyna część biegu rozmawiająca
  z siecią. Zwraca `blad=True` zamiast `return 2` z połowy funkcji.
- **`zarejestruj_z_obserwacja`** — rejestracja z równoległą obserwacją i druga fala.
- **`wczytaj_nastawy` → `Nastawy`** — 60 linii parsowania opcji wyjętych z `main`.
- **`wykonaj_sprint`** — jedno okno sprintu: rozgrzewka, strzał z Irlandii, zapas lokalny.
- **`oddaj_salwe`** — wybór kształtu salwy (redundantna / zwykła / czołowa).

### Dwa błędy znalezione przy okazji

- **`warm_size` nie powstawało przy błędnym `auto_register_salvo`.** Poprawność zależała
  wyłącznie od strażnika `if salvo_size > 1` stojącego sto linii dalej. Teraz zmienna
  istnieje zawsze.
- **Literówka w adresie kortu wywracała cały proces** nieobsłużonym wyjątkiem — a w Home
  Assistancie znaczy to pętlę restartów bez wyjaśnienia. Każda inna błędna opcja wyłącza
  tylko swoją funkcję; ta nie może być wyjątkiem. Teraz mówi, co jest nie tak.

### Wspólny rdzeń rejestracji — i Irlandia, która wreszcie patrzy w trakcie zapisu

Obserwacja w trakcie zapisu (0.22.0) istniała **tylko lokalnie**, a to Irlandia strzela
w sekundzie publikacji. Jej okno ślepoty było krótsze (zapis ~100–200 ms zamiast
~1300 ms), ale publikacja sypie partiami co ~450 ms — więc i tam mieściła się cała partia.

- **`rejestruj_obserwujac`** — jeden rdzeń używany po obu stronach.
- **`cfg["shots"]` dokłada się samo.** Zerowanie zmuszało każdego wołającego, żeby
  pamiętał o sklejeniu list po sobie; robiły to **trzy** miejsca. Zapomniana kopia nie
  krzyczy, tylko cicho gubi strzały z Dziennika — jedyny ślad po sekundzie publikacji.
  Pierwszy przebieg po zmianie od razu pokazał podwójne liczenie w Lambdzie.

### Zapis stanu jest wreszcie atomowy

`read_token.py` od początku zapisywał token przez plik tymczasowy i `os.replace`
(„by monitor nie czytał połówki"), ale **stan i dziennik zapisywały się w miejscu**.
Ubicie dodatku w trakcie zapisu — restart HA, zatrzymanie kontenera, zanik zasilania —
zostawiało ucięty `state.json`, a to znaczy:

- punkt odniesienia zerowany → KAŻDY termin wygląda na nowy → **lawina powiadomień**,
- `registered_ids` przepadają → możemy strzelić w termin, który już mamy.

`write_state_doc` nie obsługiwał też `OSError` — pełny dysk wywracał polowanie, choć
`save_hunts` ten sam błąd łykał. Teraz oba idą przez `zapisz_json_atomowo`.

### Panel dostał pierwsze testy w historii

`panel.py` — 322 linie, jedyne miejsce potrafiące **anulować rezerwację** i jedyne
wystawione przez Ingress — nie miał ani jednego testu. Nowy `test_panel.py` sprawdza:

- `/api/cancel` wymaga `Content-Type: application/json` (jedyna bariera przed
  anulowaniem cudzym żądaniem z obcej strony), odrzuca puste ID, przekazuje identyfikator
  bez zmian i nie połyka nieudanego anulowania,
- serwowanie plików nie wychodzi poza katalog noVNC (`..`, `%2e%2e`, katalogi),
- uszkodzony dziennik nie wywraca panelu.

Testy sprawdzone metodą mutacji: po usunięciu obu zabezpieczeń padają.

### Czego NIE zrobiłem i dlaczego

**Podziału `check_padel.py` na osobne pliki.** Pomiar pokazał 210 wywołań
`mock.patch.object(cp, ...)` na 29 różnych celach. Po przeniesieniu funkcji do submodułu
takie łatanie cicho przestaje działać: test przechodzi, nie testując niczego. Podział na
pliki nie skraca ani jednej funkcji — a to długość funkcji, nie długość pliku, była
źródłem pięciu błędów produkcyjnych. Ryzyko bez zysku.

## 0.23.0 — trzy opcje rozstrzygały się różnie w różnych miejscach

Ciąg dalszy przeglądu kodu. Ta sama opcja czytana w dwóch funkcjach dawała dwa różne
wyniki — bez żadnego ostrzeżenia, po prostu dwie części aplikacji działały na innych
ustawieniach. W dodatku HA `run.sh` eksportuje wszystko do ENV, więc problem był
zamaskowany i wychodził tylko przy konfiguracji z samego `config.json`.

| opcja | kto uwzględniał `config.json` | skutek pominięcia |
|-------|-------------------------------|-------------------|
| `TIMEZONE` | `run_once` tak, `main` i `_log_tz` **nie** | znaczniki czasu i okno zrywu w innej strefie niż filtrowanie terminów |
| `NTFY_TOPIC` | `run_once` tak, kontrola tokenu **nie** | brak ostrzeżenia o martwym tokenie przed zrywem |
| `LISTINGS` | `run_once` tak, rozgrzewka i sprint **nie** | sprint i zdalny strzał po cichu NIE BIORĄ UDZIAŁU w polowaniu |

- **`opcja(env, cfg, klucz, domyślna)`** — jedno miejsce rozstrzygające pierwszeństwo
  źródeł. Używane we wszystkich powyższych ścieżkach.
- **`listings_z_konfiguracji(cfg)`** — lista kortów niezależnie od źródła (`config.json`
  trzyma listę, ENV napis po przecinkach).
- **`zryw_z_otoczenia()`** — parsowanie zrywu stało w **trzech** identycznych kopiach
  (`burst_start_today`, `hunt_window`, `main`). Zapomniana kopia nie krzyczy, tylko
  cicho liczy inaczej niż pozostałe.
- **Uszkodzony `config.json` nie zatrzymuje polowania.** `load_config` łapał wyłącznie
  `FileNotFoundError`; odkąd strefa czasowa logu pochodzi z konfiguracji, jeden zabłąkany
  przecinek w JSON-ie wywracałby **każdą linię logu**, czyli cały dodatek.

## Druga fala trafiała do niewłaściwego kortu

Blok drugiej fali (0.22.0) brał `canon_url` i `listing_price` z **ostatniego obiegu pętli
po kortach**, a obserwator patrzył na ostatni kort zamiast na ten, który wydał terminy
do rejestracji. Reszta `run_once` trzyma mapy „per termin" właśnie po to, żeby wiele
kortów działało.

Test z dwoma kortami pokazał, że skutek był gorszy, niż zakładałem: przy publikacji na
pierwszym korcie **druga fala nie dostawała strzału w ogóle**, bo obserwator patrzył
w drugą stronę.

Naprawione mapami `lid_by_id` i `meta_by_lid`. Przy jednym korcie — a tak jest
w praktyce — zachowanie bez zmian.

## 0.22.1 — zestaw testów wykonywał 46 testów po raz drugi

Przegląd kodu wykazał, że pięć klas testowych dziedziczyło po **konkretnych** klasach
`TestCase`, przez co testy rodzica biegły ponownie w każdym dziecku:

| klasa | własnych | powtórzonych |
|---|---|---|
| `RemoteBatchesTest(RemoteHandlerTest)` | 6 | 14 |
| `RemoteLossReachesTheJournalTest` | 5 | 10 |
| `CancellationIsNotPublicationTest` | 4 | 10 |
| `NeverSeenTest`, `PublicationWindowTest` | 7 | 20 |

`RemoteBatchesTest` uruchamiał 20 testów zamiast własnych 6 i zajmował 9,4 s. Wzorzec
był w pliku od początku — `SalvoHelpers` ma komentarz „Bez TestCase, żeby testy bazowe
nie biegły dwa razy" — ale został złamany pięć razy.

- `HuntJournalTest` i `RemoteHandlerTest` rozdzielone na **mixin z `setUp` i pomocnikami**
  (bez `TestCase`) oraz cienką klasę z testami. Klasy pochodne dziedziczą po mixinie.
- **Żaden test nie zniknął:** 358 unikalnych metod `test_*` przed i po. Zniknęły wyłącznie
  powtórzenia wykonań: 471 → 418 uruchomień, **22,3 s → 13,3 s**.
- **Strażnik `NoDuplicatedTestRunsTest`** przechodzi po drzewie AST wszystkich plików
  testowych i nie przepuszcza klasy z testami dziedziczącej po innej klasie z testami.
  Sprawdzony na sztucznym złym przypadku — łapie go.

## 0.22.0 — zryw patrzy DALEJ, kiedy strzelamy

Log z 03.09 pokazał ostatnie okno ślepoty, tym razem po stronie lokalnej:

```
11:00:23.856   widzimy 4 dostępne, 1 pasujący: 18:00 — START salwy
11:00:25.162   salwa wraca po 1303 ms — 409, ktoś był pierwszy
11:00:25.628   dziennik, grafik dnia, zapis stanu, kolejka powiadomień
11:00:26.156   PIERWSZE spojrzenie po strzale: 10 dostępnych, 0 pasujących
```

**2,3 sekundy bez jednego pobrania.** W tym oknie liczba dostępnych skoczyła z 4 na 10 —
sześć terminów pojawiło się, gdy byliśmy zajęci własnym strzałem. Potem 80 pobrań przez
39 sekund i ani jedna wieczorna godzina nie wróciła jako wolna.

Publikacja przychodzi partiami, a nasz zapis trwa dłużej niż odstęp między nimi. Zapis
i obserwacja muszą więc dziać się **jednocześnie**.

- **Obserwator zapisu:** w zrywie, na czas rejestracji, rusza wątek pobierający grafik
  bez przerw. Zbiera terminy, które pojawiły się, gdy czekaliśmy na własną odpowiedź.
- **Druga fala dostaje strzał NATYCHMIAST** — przed księgowaniem. Dziennik, grafik, stan
  i kolejka powiadomień kosztowały 03.09 kolejne ~470 ms, a to więcej, niż trwa cudzy zapis.
- **Strzały z obu fal trafiają do Dziennika.** `shots` jest zerowane przy każdej
  rejestracji, więc bez sklejenia wpis pokazywałby wyłącznie drugą falę.
- **Grafik dnia liczymy z najświeższego dokumentu.** Stąd brał się dziwny wpis
  „4 wolne z 4" z 03.09 — grafik policzono ze zdjęcia sprzed publikacji, choć chwilę
  później kort miał ich jedenaście.
- **Tylko w zrywie.** Poza nim ciągłe pobieranie to zbędny ruch; jest na to test.
- **Awaria obserwacji nie może wywrócić polowania** — wyjątek w wątku jest połykany,
  a `stop` ustawiany w `finally`.

To ten sam błąd, który 0.20.0 naprawiło w Irlandii, tylko po stronie lokalnej i cztery
razy dłuższy — bo zapis z domu trwa 1303 ms zamiast ~100 ms z regionu.

## 0.21.1 — po logu nie dało się poznać, czy Lambda jest zaktualizowana

Licznik partii pojawiał się w Dzienniku tylko wtedy, gdy był niezerowy. W logu z 03.09
Irlandia nie znalazła nic, więc linia wyglądała identycznie jak przy kodzie sprzed
0.20.0 — a właśnie ten licznik miał być sygnałem, że nowa paczka jest wgrana.

- Zero partii jest teraz wypisywane wprost: `..., 0 partii`.
- Brak pola `batches` w odpowiedzi daje jednoznaczne
  `(stara wersja funkcji — wgraj paczkę)`.

## 0.21.0 — okno sprintu nie nadążało za publikacją

Wpis z 03.09 nie miał odznaki „Irlandia", a strzały trwały **1303 ms i 448 ms** zamiast
typowych 66–178 ms z regionu. Powód: publikacja przyszła o **11:00:25**, pięć sekund
przed startem sprintu, więc strzelaliśmy z domu.

Zebrane pory publikacji z 11 dni (23.08–03.09):

```
11:00:13  13  15  15  25  36  36  36  37  37  42
```

**Rozrzut 29 sekund.** Dotychczasowe okno sprintu (11:00:30 + 10 s) trafiało w **5 dni
na 11**. W pozostałe dni Irlandia nie obserwowała wcale, a zapas lokalny strzelał
pięciokrotnie wolniej — i to wystarczało, żeby stracić termin.

- **`MAX_SPRINT_SEKUND` w Lambdzie: 20 → 45.** Wcześniej funkcja i tak przycięłaby
  dłuższe okno do 20 s, więc sama zmiana ustawień w dodatku nic by nie dała.
- **Sufit lokalny: 30 → 60 s.** Zapas na wypadek awarii Irlandii musi umieć pokryć
  to samo okno, inaczej cofa nas do punktu wyjścia.
- **Nowe wartości domyślne: `sprint: mon-sun:11:00:05`, `sprint_seconds: 40`** —
  pokrywają wszystkie 11 zaobserwowanych dni z zapasem po obu stronach.

**UWAGA — dwie rzeczy do zrobienia ręcznie:**

1. **Home Assistant nie nadpisuje zapisanych opcji.** Ustaw `sprint` na `mon-sun:11:00:05`
   i `sprint_seconds` na `40` w konfiguracji dodatku.
2. **Timeout Lambdy musi być większy niż okno sprintu.** Przy 30 s i oknie 40 s funkcja
   zostanie ubita w trakcie obserwacji i nie odda nawet tego, co zdążyła zarezerwować.
   Ustaw **60 s** w konsoli AWS.

**Koszt:** 1769 MB × ~40 s to ~71 GB-s na dobę, czyli ~2100 GB-s miesięcznie —
około 0,5 % darmowego limitu 400 000 GB-s.

## 0.20.2 — przegrana zdalnego strzału nie docierała do Dziennika

Wpis z 02.09 twierdził „nigdy nie pokazane jako wolne: **17:00**, 18:00, 19:00", a tego
samego dnia oddaliśmy w 17:00 **dwa strzały**. Obie rzeczy nie mogą być prawdziwe.

**Przyczyna:** `failed` powstawało wyłącznie z `new_slots` — z terminów, które strona
lokalna NADAL widzi jako wolne. Termin przegrany w Irlandii jest już zajęty, gdy dokument
wraca do domu, więc wypadał z listy przegranych i lądował w „nigdy nie pokazane jako
wolne".

**Dlaczego to bolało bardziej niż zwykła literówka:** `never_seen` to dokładnie ta liczba,
na podstawie której decydujemy, czy o daną godzinę w ogóle warto walczyć. „Przegraliśmy
wyścig" i „ta godzina nigdy nie jest publikowana" to dwa różne światy z dwoma różnymi
wnioskami — a od wprowadzenia zdalnego strzału wpis mieszał je ze sobą.

**Poprawka:**

- **Oddany strzał jest dowodem, że godzinę widzieliśmy** — nawet jeśli zniknęła zaraz
  potem. Takie godziny nie trafiają już do `never_seen`.
- **Przegrana zdalnego strzału pojawia się jako przegrana**, z prawdziwym powodem.
  Powód wędruje teraz razem ze strzałem (`why`), bo tylko strzał dociera do Dziennika,
  gdy termin zdążył zniknąć z grafiku.
- Kopie strzału redundantnego liczą się jako **jedna** przegrana godzina, nie cztery.
- Godzina wygrana przez którąkolwiek kopię nigdy nie jest raportowana jako przegrana.
- Godzina, w którą **nie** strzelaliśmy, nadal trafia do `never_seen` — diagnostyka
  nie została stępiona.

To piąty raz ten sam gatunek błędu (0.14.1, 0.16.1, 0.18.0, 0.19.1, teraz): **etykieta
twierdziła więcej, niż mówiły dane.** Za każdym razem wychodziło to dopiero wtedy, gdy
dwie liczby w jednym wpisie zaczęły sobie przeczyć.

## 0.20.1 — ciche logowanie milkło na zawsze po jednej nieudanej nocy

Zgłoszone: „czemu zdarza się, że konto się wyloguje, a aplikacja nie próbuje ponownie
kliknąć zaloguj? Robię to manualnie i jest to wystarczające."

**Przyczyna:** licznik `_auto_login_tries` zerował się WYŁĄCZNIE po udanym logowaniu
**cichym**. Logowanie ręczne go nie dotykało. Sekwencja, która wyłączała funkcję na stałe:

1. sesja pada w nocy — trzy ciche próby w 20 minut, wszystkie nieudane, bo sesja
   u dostawcy tożsamości też już nie żyje,
2. licznik stoi na 3, ciche logowanie milknie,
3. logujesz się ręcznie, dodatek działa tygodniami,
4. sesja pada znowu — i **nic się nie dzieje**, bo licznik nadal stoi na 3.

Od tego momentu ręczne logowanie było jedynym wyjściem aż do restartu dodatku.

**Poprawka:**

- **Każdy udany odczyt ważnego tokenu kasuje licznik prób**, niezależnie od tego, kto
  przywrócił sesję. To jedyne miejsce, które wyłapuje logowanie ręczne.
- **Limit przedawnia się po 6 godzinach ciszy** (`AUTO_LOGIN_RESET_AFTER`). Awaria
  dostawcy tożsamości nie może wyłączać funkcji aż do restartu — jedno kliknięcie po
  pół dnia nic nie kosztuje.
- Limit **nadal chroni** przed młóceniem strony w obrębie okna; jest na to osobny test.

Przy okazji: istniejący test omijał karencję ustawiając „ostatnia próba: 1970", co po
tej zmianie przedawniałoby również sam limit — mierzyłby więc co innego, niż deklarował.
Cofa teraz czas dokładnie o karencję.

## 0.20.0 — Irlandia nie przestaje patrzeć po pierwszej partii

Log z 01.09 pokazał, gdzie naprawdę traciliśmy wieczorne godziny. Nie w wyścigu:

```
11:00:36.79  Irlandia znajduje 1. partię (19:00) i PRZESTAJE PATRZEĆ
11:00:37.08  wraca do domu z gotową rezerwacją
11:00:37.35  dopiero teraz wywołanie 2   <- 271 ms lokalnej obróbki
11:00:37.41  widzi 2. partię: 17:00 wolne, 18:00 i 20:00 JUŻ ZAJĘTE
```

**~620 ms bez jednego spojrzenia, dokładnie w kaskadzie publikacji.** W tym oknie 18:00
i 20:00 pojawiły się i zniknęły. Nie oddaliśmy w nie ani jednego strzału. Drugie
wywołanie znalazło nowe terminy po **56 ms** — one już tam czekały.

Przyczyna była w konstrukcji: `run_sprint` zatrzymuje się na PIERWSZYM trafieniu.
Irlandia rejestrowała jedną partię i wracała do domu, a publikacja sypała dalej.

- **Jedno wywołanie obserwuje teraz do końca okna** i rejestruje każdą partię, jaka się
  pojawi. Powrót do domu następuje raz, po wszystkim.
- **Limit obowiązuje całe wywołanie, nie partię.** Bez odejmowania zdobyczy trzy partie
  przy `auto_register_max: 2` dałyby sześć rezerwacji. Jest na to osobny test.
- **Gwarancja postępu:** do punktu odniesienia trafia każdy wolny termin z dokumentu,
  także odfiltrowany. Bez tego runda, w której wszystko odpadło na filtrze, wracałaby
  w kółko do końca okna i paliła procesor zamiast obserwować.
- **Wyniki i czasy strzałów ze WSZYSTKICH partii wracają do domu.** `shots` jest zerowane
  przy każdej rejestracji, więc zbieramy je po każdej partii — inaczej Dziennik
  pokazywałby wyłącznie ostatnią i kłamał o całym polowaniu.
- **Do domu jedzie najświeższy dokument** — strona lokalna liczy z niego grafik dnia.
- **Wyczerpany limit kończy wywołanie**, zamiast dobijać do końca okna. Dalszą
  obserwację i tak przejmuje zryw w domu, który leci co 0,2 s.
- Dziennik podaje teraz liczbę partii: `☁ Irlandia: sprint 6498 ms, całość 6785 ms, 2 partie`.

**Koszt:** wywołanie trwa teraz zwykle pełne okno sprintu zamiast kończyć się na
pierwszym trafieniu. Przy jednym wywołaniu dziennie to nadal ułamek darmowego limitu.

**Czego to nie rozstrzyga:** jeśli po tej zmianie 18:00 i 20:00 nadal ani razu nie
pokażą się jako wolne, znaczy to, że nigdy nie trafiają do puli — i nie ma tam czego
wygrywać. To jest właśnie eksperyment, który tę odpowiedź da.

## 0.19.1 — odbita kopia to nie przegrana

Log z 01.09 pokazał, że serwer rozróżnia dwie sytuacje, które dodatek zlewał w jedną:

```
HTTP 409 {"message":"Booking is already exists"}   <- miejsce jest JUŻ NASZE
HTTP 409 {"message":"No available seats"}          <- wziął je ktoś inny
```

Pierwszy komunikat dostaje **każda kopia strzału redundantnego poza zwycięską** — to
oczekiwany koniec drugiego losowania, a nie przegrany wyścig. Dodatek pokazywał go jako
`! Auto-rejestracja nieudana` i zapisywał w dzienniku jako `zajęty (409)`.

- **Rozróżnienie w `skroc_powod`:** własny dublet dostaje etykietę `miejsce już nasze`.
  Kolejność warunków ma znaczenie — dublet też niesie „409", więc musi być sprawdzony
  przed warunkiem ogólnym.
- **Log przestał krzyczeć:** `= Kopia strzału w 19:00 odbita: miejsce już nasze`.
  Ostrzeżenie, które przychodzi codziennie, przestaje być czytane — a przy włączonym
  `auto_register_hedge` przychodziłoby po każdej wygranej.
- **Alarm nie odpala na dniu, który wygraliśmy.** `hunt_alert_reason` liczy teraz tylko
  realne porażki. Bez tego dzień, w którym termin trzymamy w kieszeni (ponowienie na
  już zarezerwowany termin), wysyłałby push „żadna rezerwacja się nie udała".

To czwarty raz ten sam gatunek błędu (0.14.1, 0.16.1, 0.18.0, teraz): **etykieta
twierdziła więcej, niż mówiły dane.**

## 0.19.0 — strzał redundantny w najcenniejszy termin

Wniosek z pomiaru wieku danych (0.18.0) był taki, że wykrywanie i wysyłka są już na
podłodze: strzał rusza 1–2 ms po zobaczeniu grafiku. Cała zmienność siedzi w czasie,
w jakim serwer przetwarza nasz zapis — i jest to **loteria**:

```
61, 62, 63, 71, 115, 150, 157, 178, 188, 236, 251, 730 ms
```

Ten sam termin trafiony dwukrotnie dawał 62 i 251 ms (30.08, 12:00) oraz 700 i 68 ms
(25.08, 20:00). Jednego losowania nie da się przyspieszyć — ale można wziąć **minimum
z kilku**.

- **`auto_register_hedge` (domyślnie 2, maks. 3)** — tyle równoległych zapisów leci
  w najcenniejszy termin; liczy się ten, który wróci pierwszy.
- Kopie mają tę samą **rangę**, więc odstęp salwy ich nie rozsuwa. Muszą być
  równoczesnymi losowaniami, inaczej pomiar nie znaczy nic.
- Pula wątków urosła do `SALVO_MAX + HEDGE_MAX - 1` (8). Przy starych sześciu kopie
  czekałyby w **kolejce puli** zamiast lecieć równolegle — czyli dokładnie odwrotnie,
  niż wymaga eksperyment. Rozgrzewanie połączeń obejmuje teraz też te dodatkowe gniazda.
- Dotyczy **wyłącznie czołowego celu.** Rozciąganie na całą salwę mnożyłoby ryzyko
  podwójnej rezerwacji bez danych, że pomaga.
- Ustawienie jedzie do Irlandii — to ona strzela w sekundzie publikacji.

### Dlaczego nie ma obrony przed podwójną rezerwacją

Bo podwójna rezerwacja nie jest możliwa: **limit miejsc w terminie wynosi 1**, więc gdy
jedna kopia zapisze się skutecznie, druga z definicji dostaje 409. Zbudowałem najpierw
anulowanie dubletu — niepotrzebnie, a był to najbardziej ryzykowny fragment całej zmiany,
bo przy pomyłce anulowałby prawdziwą rezerwację. Został usunięty.

Zostaje jedna rzecz, i nie dotyczy ona dubletów tylko scalania dwóch odpowiedzi na ten
sam identyfikator: **sukces jest lepki.** Porażka wolniejszej kopii nie nadpisuje
zwycięstwa szybszej — inaczej powiadomienie skłamałoby, że termin przepadł.

### Drobne

- Dziennik oznacza kopie znacznikiem `⧉` przy godzinie. Bez tego identyczne wpisy
  wyglądałyby na błąd zamiast na celowy strzał.
- Potwierdzone z logu 31.08 (`8 dostępnych, 2 pasujących do filtra`): dobór celów działa
  poprawnie — w poniedziałek tylko dwa terminy przeszły przez filtr `16:00+`.

## 0.18.1 — strzał czołowy obalony po jednym dniu, domyślnie wyłączony

Eksperyment z 0.18.0 dostał odpowiedź przy pierwszej publikacji i jest to odpowiedź
przecząca. **Wpis z 31.08:**

```
pon 07.09 19:00 ✗ +0 ms → 730 ms (dane sprzed 1 ms)
pon 07.09 17:00 ✗ +0 ms → 188 ms (dane sprzed 732 ms)
```

- **Czołowy strzał poszedł SAM, na danych sprzed 1 ms — i trwał 730 ms.** Przy zerowej
  konkurencji z naszej strony. Gdyby kolejka była nasza, samotny zapis musiał wrócić
  w ~100 ms. Kolejkowanie jest po stronie serwera i dostajemy je tak samo przy jednym
  strzale, jak przy sześciu. Hipoteza z 0.18.0 upadła.
- **Co gorsza, kosztowało to drugi strzał.** 17:00 czekało na czołowego i ruszyło na
  danych sprzed **732 ms** zamiast ~1 ms. Sami zestarzyliśmy sobie informację o 731 ms.
- **Mediany po dwóch dniach:** strzał samotny **251 ms** (n=4), strzał z salwy
  **157 ms** (n=8). Samotność nie skraca zapisu — pogarsza świeżość danych dla reszty.
- `auto_register_lead` jest teraz **domyślnie wyłączony**. Wyłącznik zostaje, gdyby
  kiedyś trzeba było powtórzyć pomiar.

**UWAGA przy aktualizacji:** Home Assistant NIE nadpisuje zapisanych opcji nowymi
wartościami domyślnymi. Jeśli masz w konfiguracji dodatku `auto_register_lead: true`,
**musisz przestawić to ręcznie na `false`** — sama aktualizacja tego nie zrobi.

### Co zostaje z tego eksperymentu

Pomiar wieku danych, i to on jest tu prawdziwym zyskiem. Pokazał coś, czego nie
wiedzieliśmy: **nasza informacja jest praktycznie idealna.** Czołowy strzał miał dane
sprzed 1 ms. Nie przegrywamy dlatego, że patrzymy na stary grafik, ani dlatego, że
zwlekamy z zapisem. Przegrywamy w środku przetwarzania naszego zapisu przez serwer,
które trwa od 61 do 730 ms bez związku z czymkolwiek, co robimy.

To zamyka całą klasę „przyspieszmy się" — wykrywanie i wysyłka są już na podłodze.

## 0.18.0 — strzał czołowy i wiek danych

Wpis powstał z pięciu dni Dziennika (24–28.08), po odrzuceniu hipotezy, że serwer
pozwala nam wygrać tylko jeden termin: 26.08 i 24.08 padły po **trzy** trafienia
w jednej salwie. Ta teoria jest martwa.

Dane z samej publikacji układają się w idealny gradient po atrakcyjności godziny:

| godzina | wygrane | przegrane |
|---------|---------|-----------|
| 20:00 | 0 | 5 |
| 19:00 | 0 | 3 |
| 18:00 | 2 | 1 |
| 17:00 | 1 | 0 |
| 15:00 | 4 | 0 |

Czasy strzałów nie przewidują niczego: **74 ms przegrało 20:00, 992 ms wygrało 15:00.**

### Strzał czołowy (`auto_register_lead`, domyślnie włączony)

- 25.08 cztery strzały ruszyły w tej samej chwili (`start +0 / +0 / +8 / +16 ms`)
  i wróciły po **84 / 700 / 800 / 725 ms**. Równoległe żądania nie różnią się
  dziesięciokrotnie, jeśli nic ich nie blokuje — **serwer najpewniej obsługuje zapisy
  do tego kortu po kolei.**
- Skoro tak, to salwa w sześć terminów spychała najcenniejszą godzinę na koniec
  **naszej własnej** kolejki. Najpożądańszy termin idzie teraz sam i pierwszy,
  reszta salwy zaraz po nim.
- **To jest eksperyment, nie pewnik.** Obalenie: czołowy wraca po ~700 ms albo przegrywa
  mimo ~80 ms. Wtedy `auto_register_lead: false` przywraca strzelanie wszystkim naraz.
  Koszt, gdyby hipoteza padła: reszta salwy startuje o jeden zapis później.
- Ustawienie jedzie też do Irlandii — bez tego wyłącznik nie działałby tam, gdzie
  w sekundzie publikacji naprawdę się strzela.

### Wiek danych (`dane sprzed N ms`)

- Każdy strzał raportuje, ile czasu minęło od chwili, gdy serwer oddał nam grafik, do
  chwili, gdy ruszył zapis. Widać to w logu i w Dzienniku.
- **To rozdziela dwie porażki, które dotąd wyglądały tak samo:** „byliśmy za wolni"
  (zapis późno, dane świeże) i „patrzyliśmy na nieaktualny grafik" (zapis natychmiast,
  miejsce zniknęło, zanim zapytaliśmy). Wymagają innych poprawek.
- 24.08 strzał w 20:00 trwał **74 ms** — podłoga tego, co osiągalne — i wrócił 409.
  Winna była informacja, nie prędkość. Tej liczby nie mieliśmy, więc przez tygodnie
  przyspieszaliśmy zapis, który już był szybki.
- Punkt odniesienia wędruje od sprintu (a w zdalnym strzale — od sprintu w Irlandii)
  aż do salwy. Brak punktu odniesienia daje `None`, nie zero: zero kłamałoby, że dane
  były świeże.

## 0.17.0 — poziomy logowania + naprawa: odwołanie brane za publikację

### Poziomy logowania i ich filtracja

- Nowa opcja **`log_level`**: `debug` / `info` (domyślnie) / `warn` / `error`. Doba pracy
  dodatku to ~2000 linii, z czego ~95 % to dwa powtarzalne komunikaty — `= Kort: N
  dostępnych…` przy każdym pytaniu o grafik i `Brak nowych wolnych terminów.` Na `info`
  zostaje kilkadziesiąt linii: publikacja, grafik dnia, rezerwacje, salwa, dziennik.
- **W zrywie rutynowe linie wracają na `info`.** To one są materiałem dowodowym przy
  analizie polowania — pokazują, o której sekundzie termin pojawił się w API i ile trwało
  pobranie. Poza zrywem te same linie są szumem, więc idą na `debug`. Wyciszamy pytanie
  „czy coś się zwolniło?", nie odpowiedzi.
- **Bicie serca czytnika tokenu (`✓ JWT odczytany` co 5 min, ~290 linii dziennie) jest na
  `debug`**, ale pierwszy udany odczyt po serii błędów wraca na `info`. Chcemy widzieć
  moment, w którym sesja wraca do życia, a nie ciągłe potwierdzenia, że jeszcze żyje.
- **Poziom czytamy z pierwszego znaku komunikatu.** Kod od początku znakuje wagę: `!` i `⚠`
  to kłopot, `✗` to nieudany strzał albo martwy token. Czytanie tej konwencji było lepsze
  niż dopisanie `level=` w czterdziestu miejscach — i nowe linie same trafiają na właściwą
  półkę, bez pamiętania o tym przy każdej zmianie.
- **Błędna nazwa poziomu nie wycisza dodatku** — nierozpoznana wartość cofa się do `info`.
  Literówka w konfiguracji nie może oślepić cię w dniu publikacji; jest na to test.
- Poziom obowiązuje oba procesy dodatku, a zdalna Lambda czyta ten sam `LOG_LEVEL`.

### Naprawa: odwołanie na dziś ogłaszane jako publikacja

- 28.08 o 08:57 ktoś zwolnił termin **na ten sam dzień**. Dziennik wziął pierwsze nowe
  terminy doby za publikację, zapisał „publikacja 08:57:20" i wysłał alarm „poza zrywem".
  Prawdziwa publikacja przyszła o **11:00:36** — czyli tak samo jak dzień wcześniej.
- **Skutek był gorszy niż fałszywy push:** na podstawie tego wpisu doradzałem przebudowę
  okien zrywu, której nie było potrzeba. Dziennik ma być podstawą decyzji, więc pomyłka
  w etykiecie kosztuje więcej niż zgubiona linia logu.
- **Poprawka:** za publikację uznajemy tylko wykrycie, którego horyzont sięga co najmniej
  **6 dni w przód** (`PUBLIKACJA_MIN_DNI`). Odwołanie dotyczy dnia dzisiejszego albo
  jutrzejszego i nie ustawia już godziny publikacji ani nie odpala alarmu — ale nadal
  trafia do dziennika jako wygrany termin. Prawdziwy rozjazd okna wciąż krzyczy.
- To trzeci raz ten sam gatunek błędu (0.14.1, 0.16.1, teraz): **etykieta twierdziła
  więcej, niż mówiły dane.**

## 0.16.1 — naprawa: „nigdy nie pokazane jako wolne" liczyło całą dobę

- Wpis z 28.08 mówił `8 wolnych z 11` i **jednocześnie** wymieniał osiem godzin jako
  zniknięte przed naszym pierwszym spojrzeniem. Te liczby nie mogą być naraz prawdziwe.
- **Przyczyna:** zajęte godziny sumowały się przez **wszystkie** wykrycia w ciągu doby,
  więc godzina zarezerwowana przez kogoś sześć godzin po publikacji lądowała
  w diagnostyce publikacji. To zwykły ruch, a nie przegrany wyścig.
- **Poprawka:** zajęte godziny zbieramy tylko przez **2 minuty od pierwszego wykrycia**.
  Publikacja sypie partiami przez ~sekundę, więc jedna migawka nie pokazuje całego
  grafiku — dwie minuty to zapas z naddatkiem i jednocześnie nic, co dałoby się pomylić
  ze zwykłym ruchem w ciągu dnia. Późniejsze migawki nadal aktualizują licznik wolnych.
- To ten sam gatunek błędu co w 0.14.1: **etykieta twierdziła więcej, niż mówiły dane.**


## 0.16.0 — ciche logowanie: dodatek sam klika „ZALOGUJ SIĘ”

- **Gdy sesja Decathlon GO wygaśnie, czytnik tokenu klika przycisk logowania sam.**
  Bardzo często to wystarcza: sesja u dostawcy tożsamości żyje dłużej, więc przeglądarka
  odbija się przez OAuth i wraca zalogowana **bez wpisywania czegokolwiek**.
  Robimy dokładnie to samo kliknięcie, które robiłeś ręcznie.
- **Czego to NIE robi:** nie wpisuje loginu, hasła ani kodu z maila, nie dotyka
  formularzy, nie obchodzi żadnego zabezpieczenia. Jeśli po kliknięciu pojawi się
  formularz — poddajemy się i prosimy o ręczne logowanie. Jest na to osobny test,
  który sprawdza, że jedyną interakcją ze stroną jest kliknięcie linku.
- **Bezpieczniki:** najwyżej 3 próby, co najmniej 10 minut przerwy między nimi,
  wyłącznie gdy tokenu nie ma i nie stoimy na stronie logowania. Po sukcesie licznik
  prób się zeruje. Opcja `auto_login: false` wyłącza mechanizm całkowicie.
- **Selektor po `href="/login"`, nie po klasie CSS.** Klasy w tej aplikacji są
  zahaszowane (`Topbar_navbarLogin__4Hfnb`) i zmieniają się przy każdym wydaniu —
  selektor po klasie zepsułby się po cichu przy pierwszym deployu Decathlonu.
- **Usunięty przycisk „Otwórz w Decathlon GO"** z zakładki Rezerwacje (dodany
  w 0.15.0). Prowadził na stronę kortu, a nie do konkretnego terminu — bo aplikacja
  GO nie ma takiego adresu — więc nie robił tego, po co powstał.
- `test_read_token.py` to nowy plik testów; CI uruchamia go razem z pozostałymi.


## 0.15.0 — kontrola sesji przed polowaniem i link do kortu przy rezerwacji

- **Push o martwej sesji przychodzi ZANIM otworzy się okno** (`token_check_before`,
  domyślnie 30 minut przed zrywem). 27.08 wszystkie pięć strzałów padło w **0 ms**
  z powodem `token` — sesja przeglądarki nie żyła. Zero milisekund znaczy, że żadne
  żądanie nie wyszło. Kosztowało to **cztery wolne terminy (15, 17, 19, 20)**,
  w tym 20:00, o które walczymy od tygodni, a dowiedzieliśmy się o tym **po** fakcie.
- **Kontrola jest dwustopniowa**, bo dwa różne uszkodzenia wyglądają identycznie
  dopiero przy strzale: najpierw lokalnie (czy token istnieje i nie wygasł — to
  złapałoby 27.08), potem na żywo jednym `users.getMe` (czy serwer go jeszcze
  akceptuje — sesja bywa unieważniona przy ważnym `exp`).
- **Awaria sieci NIE jest raportowana jako martwa sesja.** Fałszywy alarm o północy
  byłby gorszy niż jego brak.
- To **nie** jest powrót do rozgrzewki uwierzytelnionej z 0.9.0, którą obalił pomiar.
  Tam chodziło o przyspieszenie strzału; tu o jedno zapytanie na dobę, pół godziny
  wcześniej, wyłącznie po to, żeby zdążyć się zalogować.
- **Przycisk „Otwórz w Decathlon GO"** przy każdej rezerwacji w panelu — także przy
  odwołanej, bo stamtąd rezerwuje się ją z powrotem. Otwiera w nowej karcie, żeby nie
  wyrzucać Cię z Home Assistanta.
  > Aplikacja GO nie ma adresu prowadzącego do KONKRETNEGO terminu — strona kortu to
  > SPA bez parametru daty (sprawdzone w jej HTML-u). Link prowadzi więc na stronę
  > kortu, gdzie termin widać.
- Dopasowanie dni zrywu przeszło na `DAY_NAMES` zamiast `strftime("%a")`, który zależy
  od ustawień językowych kontenera i przy innym locale cicho przestałby pasować.


## 0.14.1 — naprawa dziennika: zły dzień i własne terminy liczone jako stracone

Pierwszy produkcyjny wpis nowej diagnostyki (26.08) obnażył dwa moje błędy — oba
sprawiały, że raport **wprowadzał w błąd**, choć same rezerwacje były poprawne.

- **Raport opisywał zły dzień.** Nowe terminy potrafią przyjść z DWÓCH dni naraz:
  publikacja dotyczy dnia +7, a odwołania dni bliższych. 26.08 tak właśnie było
  (01.09 z odwołań, 02.09 z publikacji), a `log_day_grids` brało dzień **najwcześniejszy**.
  Teraz bierze **horyzont**, czyli dzień najdalszy — ten świeżo opublikowany.
- **Nasze własne rezerwacje liczyły się jako stracone.** 01.09 18:00 mieliśmy od 25.08,
  a raport wypisał je wśród „nigdy nie pokazane jako wolne". `day_grid` przyjmuje teraz
  listę terminów, które już trzymamy, i wyłącza je z zajętych.
- **„Zanim zobaczyliśmy grafik" tylko dla dnia publikacji.** W dniach wcześniejszych
  zajęte godziny to zwykły ruch z ostatniej doby, a nie przegrany wyścig — teraz
  opisane jako „przez innych".


## 0.14.0 — dziennik mówi, KTÓRE godziny zniknęły przed nami

- **Nowa linia: „Nigdy nie pokazane jako wolne: 20:00".** Dotąd dziennik mówił tylko
  `7 wolnych z 11` — a to za mało, żeby odpowiedzieć na pytanie „jakim cudem ktoś
  zawsze zajmuje 20:00". Teraz widać różnicę między dwoma zupełnie różnymi problemami:
  - **przegrany wyścig** (`Przegrane: 20:00 (zajęty 409)`) — widzieliśmy termin wolny,
    strzeliliśmy i przegraliśmy zapis. To da się optymalizować.
  - **godzina, której nigdy nie zobaczyliśmy wolnej** — zniknęła przed naszym pierwszym
    spojrzeniem. **Tego nie wygra żadna prędkość ani żaden region**, więc nie ma sensu
    gonić króliczka.
- **Dziennik dodatku też wymienia godziny**: `📋 Grafik na pon 31.08: 7 wolnych z 11 —
  4 zajęte (09:00, 10:00, 12:00, 20:00), zanim zobaczyliśmy grafik`.
- **Nasze własne rezerwacje są odejmowane** — w kolejnej migawce termin zdobyty przez
  nas też jest „zajęty", więc bez tego dziennik oskarżałby nas o kradzież własnych godzin.
- Zajęte godziny sumują się przez wszystkie partie publikacji, bo każda migawka pokazuje
  inny fragment grafiku.
- `hunts.json` wypadł z repozytorium do `.gitignore` — to plik runtime, jak `state.json`,
  a trafił tam przez moje `git add -A` przy 0.13.0.


## 0.13.0 — dziennik polowań: zakładka „Polowania" i alert o rozjeździe

- **Nowy plik `hunts.json` i zakładka „Polowania" w panelu.** Jeden wpis na dobę:
  o której przyszła publikacja, ile terminów było w grafiku, co zdobyte, co przegrane
  i z jakim czasem poszedł każdy strzał. Najważniejsze liczby z sekundy publikacji
  przestają ginąć w tysiącach linii Dziennika dodatku.
- **Push, gdy dzień wymaga uwagi** — żeby nie trzeba było zaglądać codziennie.
  Alarmujemy w dwóch przypadkach: publikacja wypadła **poza oknem zrywu**, albo
  **żadna rezerwacja się nie udała**. Raz na dobę, nie przy każdej partii terminów:
  push, który przychodzi codziennie, przestaje być czytany.
- **Powód powstania:** 23.08 publikacja przesunęła się z ~11:00:53 na **11:00:15**,
  więc zryw (11:00:30) i sprint (11:00:51) w ogóle nie zdążyły wystartować.
  Pięć z dwunastu terminów zniknęło, zanim spojrzeliśmy. Zauważyliśmy to wyłącznie
  dlatego, że przyszedł log — inaczej wyglądałoby to jak seria gorszych dni.
- **Czasy strzałów trafiają do wpisu z OBU stron** — zdalnej i lokalnej. Handler
  odsyła je w odpowiedzi; bez tego przy zdalnym strzale dziennik wiedziałby, CO się
  udało, ale nie JAK szybko, a to ta liczba rozstrzygała każdą dotychczasową zagadkę.
- **Historia obcięta do 60 dni**, uszkodzony plik nie wywraca polowania, a błąd zapisu
  dziennika jest głośny, ale nie przerywa biegu — rezerwacja jest cenniejsza niż historia.
- Zakładka czyta **wyłącznie plik z dysku**, więc odświeżanie panelu o 11:00:15
  nie może zaszkodzić polowaniu.


## 0.12.0 — odstęp między strzałami salwy (`auto_register_stagger`)

- **Strzały salwy ruszają teraz w odstępie 8 ms, w kolejności Twoich preferencji.**
  Powód jest zmierzony: 14.08 cztery strzały z Irlandii ruszyły z `start +0 ms`
  co do jednego, a wróciły po **21 / 37 / 117 / 282 ms**. Skoro startują razem,
  ten „schodek" powstaje po stronie Decathlona — najpewniej serwer **serializuje
  zapisy per konto**, więc nasze własne strzały stoją w kolejce jeden za drugim.
- **Miejsce w tej kolejce było LOSOWE.** 15:00, wymienione w salwie jako ostatnie,
  weszło w 37 ms; 17:00 jako trzecie czekało 282 ms. Odstęp sprawia, że kolejność
  jest NASZA: najbardziej pożądany termin wchodzi pierwszy.
- **Kilka ms wystarczy.** W regionie rozrzut sieci jest poniżej milisekundy, więc nie
  czekamy na odpowiedź — gwarantujemy tylko kolejność dotarcia. Przy 4 strzałach
  ostatni rusza 24 ms później, czyli o rząd wielkości mniej niż obserwowany rozrzut.
- **To wynika z HIPOTEZY**, nie z pewnika. Gdyby kolejka per konto okazała się fałszem,
  kosztem jest te kilkanaście ms na dalszych strzałach. `auto_register_stagger: 0`
  przywraca strzelanie wszystkim naraz.
- **Log sam pokaże, czy zadziałało**: `start +N ms` odzwierciedla odstęp, a `ms` mierzy
  wyłącznie żądanie (uśpienie jest przed pomiarem). Jeśli termin nr 1 zacznie
  konsekwentnie wracać w ~21–37 ms, hipoteza się broni.
- Odstęp jedzie w treści żądania do Irlandii, więc obie strony strzelają identycznie.


## 0.11.2 — diagnostyka zdalnych strzałów

- **Każdy strzał salwy pokazuje, KIEDY ruszył** względem startu salwy:
  `[start +1 ms, 155 ms]`. To rozstrzyga, kto odpowiada za „schodek" czasów.
  13.08 z Irlandii cztery strzały naraz zajęły **185/217/326/382 ms**, a jeden samotny
  **121 ms** — jeśli starty są bliskie zeru, kolejkuje serwer Decathlona; jeśli się
  rozjeżdżają, wina jest po naszej stronie (pula wątków, DNS, TLS). Bez tej liczby
  to była zgadywanka.
- **Hipoteza „wygłodzony rdzeń Lambdy" ODPADA** — funkcja ma 1769 MB, czyli pełny
  rdzeń, potwierdzone w konsoli. Zdalne strzały były wolniejsze od lokalnych mimo
  80× krótszej drogi do serwera i to nadal nie jest wyjaśnione.

- **Handler raportuje swój przydział pamięci** (czyli w Lambdzie przydział CPU)
  i ostrzega, gdy jest poniżej 1769 MB. 13.08 strzały z Irlandii zajmowały
  **185–382 ms**, podczas gdy lokalne z Polski w tej samej sekundzie **53–81 ms** —
  czyli zdalne były 3–5× WOLNIEJSZE mimo rundy 0,5 ms zamiast ~42 ms. Nie dało się
  rozstrzygnąć dlaczego, bo nie logowałem najważniejszej zmiennej. W sondzie
  (`aws_probe/`) raportuję ją od początku; w handlerze zabrakło.
- **Połączenia salwy są rozgrzewane w Lambdzie**, tak samo jak lokalnie. W regionie
  kosztuje kilka ms. To NIE jest wyjaśnienie wolnych strzałów, tylko usunięcie
  jednej zmiennej z równania.


## 0.11.1 — naprawa: zdalna rejestracja padała na ważnym tokenie

- **Zdalna strona uznawała ważny token za wygasły i próbowała go odświeżyć.**
  W `handler.py` ustawiłem `browser_mode: False`, przez co `ensure_decathlon_token`
  traktował token jako wygasły już **300 s (`TOKEN_EXPIRY_MARGIN`) przed czasem**
  i szedł po serwerowy `/auth/refresh` — a ten w Decathlon GO **zawsze** zwraca 401.
  Token żyje ~15 min, więc rejestracja z Irlandii padała przez **ostatnią 1/3 jego
  życia**. 12.08 tak przepadło **17:00**: obie próby zwróciły
  `nie udało się odświeżyć tokenu: HTTPError 401` po 8–9 ms.
  To dokładnie ten sam błąd, który naprawiono lokalnie w 0.3.1 — wprowadzony ponownie
  po zdalnej stronie.
- **Poprawka: `browser_mode: True` w Lambdzie**, mimo że żadnej przeglądarki tam nie ma.
  Chodzi o semantykę wygaśnięcia, nie o przeglądarkę: w tym trybie „wygasły" znaczy
  `exp` w przeszłości i nie ma żadnego refreshu.
- **`wait_for_fresher_token` wraca natychmiast, gdy nie ma pliku tokenu.** Bez tego
  po HTTP 401 czekałaby ~24 s na przeglądarkę, której w danym środowisku nie ma —
  i to w samej sekundzie publikacji.
- **Zapas lokalny zadziałał** i uratował 15:00: po wpadce zdalnej dodatek zarejestrował
  z domu 209 ms później. To była pierwsza produkcyjna próba tej ścieżki.


## 0.11.0 — zdalny strzał z eu-west-1 (opcjonalny)

- **Sprint i salwa mogą wykonywać się w AWS Irlandia**, tuż obok serwera Decathlona.
  Włącza się dwiema opcjami: `remote_url` i `remote_secret`. Puste = wszystko dzieje
  się lokalnie, dokładnie jak dotąd. Instrukcja krok po kroku: `aws_remote/README.md`.
- **Zmierzony zysk** (`aws_probe/`, Lambda 1769 MB): runda do serwera 0,5 ms zamiast
  ~42 ms, ciężkie pobranie 39 ms zamiast ~80 ms. Ścieżka „termin pojawia się na
  serwerze → nasze żądanie tam dociera" spada ze **~107 ms do ~32 ms**.
- **W Home Assistancie zostaje wszystko poza tymi 15 sekundami**: logowanie,
  przeglądarka, token, panel, kalendarz, powiadomienia, stan.
- **Token nie jest w AWS zapisywany.** Leci w treści żądania, żyje w pamięci przez
  jedno wywołanie i znika; w dzienniku funkcji widać wyłącznie jego długość.
- **Zdalna strona używa TEGO SAMEGO `check_padel.py`.** Paczka Lambdy zawiera silnik
  dodatku, więc filtry, limity i salwa są identyczne. Dwie osobne implementacje
  rozjechałyby się przy pierwszej zmianie — i skończyło się rezerwacją terminu,
  którego nie chcesz, albo o jeden za dużo.
- **Zapas lokalny.** Gdy Irlandia odmówi lub nie odpowie szybko, dodatek poluje sam.
  Po timeoucie NIE strzela powtórnie (funkcja mogła zdążyć zarezerwować) i mówi
  o tym wprost w Dzienniku.
- **`remote_url` bez `remote_secret` wyłącza zdalny strzał** zamiast wysyłać token
  pod adres bez żadnej bramki.
- Dziennik z Irlandii jest przepisywany do Dziennika dodatku (linie z `☁`) — bez tego
  jedyny ślad po sekundzie publikacji zostawałby w CloudWatch.
- `run_sprint` przyjmuje filtry z zewnątrz (w Lambdzie nie ma `config.json`), a budowa
  ustawień auto-rejestracji trafiła do wspólnego `build_reg_cfg`.

## 0.10.0 — cały grafik w logu, push poza sekundą publikacji

- **Log pokazuje CAŁY grafik dnia, nie tylko wolne terminy**:
  `📋 Grafik na pon 17.08: 3 wolne z 14 — 11 zajętych, zanim zobaczyliśmy grafik`.
  Dotąd zajęte terminy były wycinane przy parsowaniu, więc godzina, której nigdy nie
  zobaczyliśmy jako wolnej, wyglądała identycznie jak godzina nigdy niewystawiona.
  Nie dało się odpowiedzieć na pytanie „czy ktoś zdążył przed nami" — teraz odpowiedź
  jest w Dzienniku każdego dnia, bez dopytywania. Dane były w każdej odpowiedzi API
  od zawsze; po prostu je wyrzucaliśmy.
- **W zrywie powiadomienia ntfy czekają w kolejce** i idą po zamknięciu okna.
  Zmierzone w logach z 9. i 10.08: między wykryciem partii terminów a wznowieniem
  sprintu mijało **643–819 ms, w większości na pushu do ntfy.sh** — czyli na
  zapytaniu do zupełnie innego serwera. Przez ten czas NIE PATRZYLIŚMY na grafik,
  choć publikacja wciąż trwała i sypały się kolejne partie.
  **Poza zrywem nic się nie zmienia** — push leci natychmiast, jak dotąd.
- **Odłożone powiadomienia są ponawiane, a porzucane głośno** (po 3 próbach).
  Ciche zgubienie powiadomienia o wolnym terminie wygląda jak brak terminów.
- **Zapisu stanu NIE odłożyliśmy**, wbrew wcześniejszemu planowi. Pomiar: 0,12 ms
  (mediana, 300 terminów). Odłożenie dałoby zysk w granicach szumu, a kosztowałoby
  ryzyko podwójnej rezerwacji przy restarcie procesu w złym momencie.
- **Usunięta rozgrzewka uwierzytelniona z 0.9.0 — hipoteza UPADŁA.** 10.08 pomiar
  rozstrzygnął: `users.getMe` w rozgrzewce kosztował **48 ms**, a pierwsza rejestracja
  i tak **232 ms**. Gdyby brama walidowała JWT przy pierwszym użyciu, drogi byłby ten
  POST — było odwrotnie. Koszt pierwszego zapisu nie siedzi ani w połączeniu, ani
  w uwierzytelnieniu; zostaje w kodzie ostrzeżenie, żeby nie wracać do tego pomysłu.
- Zostaje to, co się obroniło w 0.9.0: **pojedynczy termin idzie przez pulę salwy**
  (10.08 samotne 15:00 zarezerwowane tą drogą).

## 0.9.0 — rozgrzewka uwierzytelniona, salwa także dla jednego terminu
- **Pojedynczy termin idzie teraz przez pulę salwy.** Dotąd salwa włączała się dopiero
  przy dwóch terminach, więc samotny strzał leciał z wątku głównego — poza pulą, którą
  rozgrzewamy tuż przed sprintem. 9.08 kosztowało to termin: 19:00 przyszedł w osobnej
  partii, sam, strzał zajął **319 ms** wobec 57–84 ms strzałów z puli w tej samej
  sekundzie, i termin przepadł na 409.
- **Rozgrzewka wysyła też uwierzytelniony POST** (`users.getMe` — bez skutków ubocznych
  na koncie). Powód: hipoteza „wystygnięte gniazda" upadła po raz drugi. 9.08
  połączenia salwy odświeżono 2,5 s przed strzałem, a pierwsza rejestracja i tak
  zapłaciła nadmiar (8.08 identycznie: 294 ms wobec 73 ms). Skoro gniazdo było ciepłe,
  koszt siedzi gdzie indziej — jedyne, czym rejestracja różni się od rozgrzewkowego
  GET-a, to metoda POST i nagłówek `Authorization`. Jeśli brama waliduje JWT przy
  pierwszym użyciu, płacimy to teraz **poza ścieżką krytyczną**.
- **Czas tej rozgrzewki trafia do Dziennika** (`uwierzytelnienie 70–310 ms`). To jest
  pomiar powyższej hipotezy, nie ozdobnik: jeśli rozgrzewka jest droga, a pierwszy
  strzał spadnie do ~70 ms — hipoteza się broni. Jeśli rozgrzewka jest szybka,
  a strzał dalej wolny — upada i trzeba szukać dalej.
- **Wygasły token nie uruchamia odnowienia w rozgrzewce.** Refresh token bywa
  jednorazowy, więc rozgrzewka spaliłaby go tuż przed rejestracją. Martwy token
  to po prostu brak rozgrzewki.
- **Rozgrzewka ma własny, krótki limit czasu (3 s)** zamiast domyślnych 30 s. Stoi
  tuż przed startem sprintu i go blokuje — zawieszone zapytanie zjadłoby całą sekundę
  publikacji, a przecież bez rozgrzewki polujemy dalej.
- **Nieudana rozgrzewka jest widoczna w Dzienniku** (`uwierzytelnienie NIEUDANE`).
  Bez tego cicha awaria wyglądałaby dokładnie tak samo jak wyłączona opcja — czyli
  jak nic.
- **`auto_register_salvo` domyślnie 6** zamiast 4. 9.08 publikacja przyniosła 6 terminów
  naraz, więc dwa ostatnie poszły ogonem sekwencyjnym, ~120 ms później.

## 0.8.0 — kalendarz usuwa odwołane rezerwacje
- **Plik `.ics` niesie teraz odwołania.** Dotąd anulowane rezerwacje były z niego
  wycinane, więc kalendarz nigdy się nie dowiadywał, że termin przepadł — wisiał
  w nim w nieskończoność. Teraz nadchodzące anulowane trafiają do pliku
  z `STATUS:CANCELLED`.
- **`SEQUENCE` rośnie przy odwołaniu** (0 → 1). To nie kosmetyka: kalendarz dopasowuje
  wydarzenie po `UID` i przyjmuje zmianę wyłącznie wtedy, gdy numer wersji jest wyższy
  niż zapamiętany. Bez tego plik zostałby po cichu zignorowany.
- **Anulowana rezerwacja ma w panelu przycisk „Usuń z kalendarza"**, a pojedynczy plik
  dla niej to `METHOD:CANCEL` — jednoznaczny sygnał iTIP, interpretowany przez
  aplikacje kalendarza pewniej niż samo `STATUS:CANCELLED`.
- **Po anulowaniu link pojawia się od razu w komunikacie.** Karta znika z listy
  (anulowane są domyślnie ukryte), więc bez tego trzeba by go szukać pod przełącznikiem.

## 0.7.3 — odświeżenie połączeń salwy tuż przed sprintem
- **Połączenia salwy są rozgrzewane ponownie na starcie sprintu**, a nie tylko na
  starcie zrywu. Między jednym a drugim mija kilkanaście sekund, przez które pula
  salwy leży bezczynnie — teraz ma gwarantowanie ciepłe gniazda sekundy przed użyciem.
- **Uczciwie o powodzie:** w logu z 8.08 pierwsza salwa zajęła 294 ms i 217 ms na
  próbę, druga (sekundę później) 66–78 ms. Podejrzewałem wystygnięcie połączeń przez
  9 s przerwy, ale **pomiar to obalił** — gniazdo przeżywa 12 s bezczynności bez
  straty (66–70 ms). Najpewniejsze wyjaśnienie tamtej różnicy to obciążenie serwera
  w samej sekundzie publikacji, czyli coś poza naszą kontrolą.
- Zmiana zostaje jako **tanie ubezpieczenie**: kosztuje ~250 ms na starcie sprintu
  (2–3 s przed publikacją, więc bez wpływu na wynik) i zdejmuje z równania jedną
  zmienną, gdyby serwer zachowywał się pod obciążeniem inaczej niż w spokojnym pomiarze.

## 0.7.2 — pomiar opóźnienia do serwera przy starcie
- **Dziennik pokazuje przy starcie rundę do Decathlon GO** (mediana z 5 prób, czysty TCP,
  bez dotykania API). Ścieżka rezerwacji ma DWIE takie rundy, więc to najtwardsza
  część budżetu czasowego — i jedyna, której nie da się skrócić kodem.
- Gdy opóźnienie jest wysokie (≥75 ms), dodatek podpowiada sprawdzenie, czy serwer
  stoi na kablu zamiast na WiFi: zmierzone u nas ~10 ms idzie na samą bramę lokalną,
  a przy dwóch rundach liczy się to podwójnie.
- Powód: pomiary robiłem dotąd z laptopa po WiFi (80 ms do Irlandii). Liczba z
  faktycznego serwera Home Assistant może być zupełnie inna i dopiero ona mówi,
  czy jest tu jeszcze co zbierać.

## 0.7.1 — sprint po przeglądzie: ciepłe połączenia i sprostowane liczby
- **Połączenia sprintu rozgrzewane na starcie zrywu**, tak jak połączenia salwy.
  Sprint ma własną pulę, więc jej gniazda były ZIMNE w chwili startu — trzy
  równoczesne uzgodnienia TLS potrafią zająć sekundy, czyli większość okna sprintu.
- **Sprostowanie pomiaru z 0.7.0.** Tabelę przepustowości zmierzyłem wtedy na lekkim
  pingu, a sprint pobiera pełne dane. Rzeczywiste wartości: 1 wątek 8,3 zap/s
  (świeży wynik co 117 ms), 2 wątki 17,0 (56 ms), **3 wątki 24,0 (35 ms)** — a nie
  20 ms, jak podawałem. Zysk sprintu to więc ~80 ms, nie ~90 ms.
- **Zapas miejsc w puli sprintu.** Zwycięzca wraca natychmiast, a maruderzy jeszcze
  pobierają; bez zapasu jeden wolniejszy strzał zabierałby miejsce kolejnej rundzie
  i sprint po cichu działałby węższym frontem, niż prosił użytkownik.
- Milisekundy w Dzienniku włączają się też **na sam sprint**, nawet gdy nie trwa zryw —
  to najbardziej czasowo-krytyczny moment całego polowania.

## 0.7.0 — sprint: pobieranie bez przerw w sekundzie publikacji
- **Nowy `sprint`** (domyślnie `mon-sun:11:00:51`, 4 s, 3 wątki): przez kilka sekund
  wokół sekundy publikacji kilka wątków pobiera repertuar **bez przerw**. Zmierzone:
  3 wątki dają świeży obraz co **20 ms** zamiast co 200 ms, więc średnie opóźnienie
  wykrycia spada ze ~100 ms do ~10 ms.
- **Zwycięski wątek oddaje gotowe dane** prosto do rejestracji (`prefetched`). Bez tego
  trzeba by pobrać je jeszcze raz i stracić całą rundę do serwera (~92 ms) dokładnie
  w chwili, gdy liczy się najbardziej.
- **Punkt odniesienia z zapisanego stanu, nie z pierwszego pobrania** — inaczej
  publikacja trafiająca w pierwsze ~90 ms sprintu wpadłaby do punktu odniesienia
  i sprint nigdy by się nie odpalił.
- Sprint ma **własną pulę wątków**, osobną od salwy: wątki zajęte pobieraniem nie mogą
  blokować strzałów rejestracji.
- Zwycięzca wraca **natychmiast**, bez czekania na pozostałe wątki — maruder w trakcie
  pobierania kosztowałby do ~90 ms, czyli tyle, ile sprint ma zaoszczędzić.
- Domyślny `burst_interval` obniżony do 0,2 s (zmierzone: dotrzymywane co do 4 ms).
- Ścieżka od publikacji do strzału: **~290 ms → ~200 ms**. Pozostałe ~190 ms to dwie
  rundy do serwera w Irlandii (RTT 92 ms) — tego z domowego łącza nie da się skrócić.

## 0.6.1 — salwa odporna na awarie (przegląd 0.6.0)
- **KRYTYCZNE: wyjątek w jednym wątku salwy wywracał całą salwę.** `pool.map` podnosi
  błąd dopiero przy odczycie wyników, więc jedna zepsuta odpowiedź serwera (np. błędny
  JSON) sprawiała, że rezerwacje zrobione przez pozostałe wątki **nie były ani zapisane,
  ani anulowane, ani zgłoszone** — zostawały zajęte korty bez śladu w logu i stanie.
  Każdy strzał ma teraz własną osłonę i zwraca porażkę zamiast wybuchać.
- **Nieudane anulowanie nadmiaru jest teraz głośne.** Wcześniej powiadomienie i tak
  mówiło „anulowano", niezależnie od wyniku — użytkownik nie wiedziałby, że został mu
  zajęty kort. Teraz komunikat i log mówią wprost: anuluj ręcznie w panelu.
- **Rezerwacja bez ID transakcji** (serwer go nie zwrócił) też jest raportowana zamiast
  po cichu pomijana — inaczej nadmiarowy kort zostawał zajęty bez ostrzeżenia.
- **Token odnowiony w wątku salwy trafia z powrotem do konfiguracji.** Wątki pracują na
  kopii, więc odnowienie po HTTP 401 ginęło, a próby sekwencyjne po salwie czekały
  drugi raz na to samo (do ~24 s w gorącym oknie).

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
