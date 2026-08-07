# Padel (Decathlon) — dodatek Home Assistant

Monitoruje wolne terminy padla na Decathlon GO, wysyła **push przez ntfy.sh** i
(opcjonalnie) **rejestruje automatycznie**. W środku działa prawdziwa przeglądarka
(Chromium) w panelu — **logujesz się w niej raz**, a dodatek sam podtrzymuje sesję i
odświeża token, więc auto-rejestracja działa bezobsługowo.

## Dlaczego przeglądarka

Token Decathlon GO (`go-sdk-jwt`) żyje ~15 min i **nie da się go odnowić po stronie
serwera** — próba odtworzenia logowania SSO uruchamia weryfikację mailową (kontrola
„nowe urządzenie"). Rozwiązanie: raz logujesz się ręcznie w przeglądarce działającej
na serwerze; strona **sama odnawia token** przy kolejnych wczytaniach, a dodatek
odczytuje go z `localStorage` i przekazuje monitorowi. Nic nie jest obchodzone —
przechodzisz normalne logowanie, łącznie z kodem z maila.

## Instalacja (repozytorium custom)

1. W HA: **Ustawienia → Dodatki → Sklep z dodatkami → ⋮ (prawy górny róg) → Repozytoria**.
2. Dodaj adres repozytorium (np. `https://github.com/maz00r/padel-watch`) → **Dodaj**.
3. Odśwież sklep, wejdź w **Padel (Decathlon)** → **Zainstaluj**.
4. Zakładka **Konfiguracja** — ustaw opcje (niżej) → **Zapisz**.
5. Zakładka **Info** → **Uruchom**. W menu bocznym pojawi się ikona **Padel** (panel).
6. Otwórz **panel** i **zaloguj się** na go.decathlon.pl (przyjdzie kod na maila — wpisz go).
7. Logi w zakładce **Dziennik**; na telefonie zasubskrybuj temat ntfy.

> Dodatek działa tylko na **Home Assistant OS** lub **Supervised** (wymaga panelu Ingress).
> Obraz zawiera Chromium (~760 MB) — na mini PC bez znaczenia, na Raspberry Pi odradzam.

## Opcje

| Opcja | Znaczenie | Przykład |
|-------|-----------|----------|
| `ntfy_topic` | temat ntfy (ten sam, co subskrybujesz w apce) | `your-ntfy-topic-here` |
| `check_interval` | bazowa częstotliwość sprawdzania w sekundach (10–3600) | `60` |
| `filters` | godziny powiadomień; okna `;`, każde `DNI:HH:MM-HH:MM` | `mon-fri:15:00-02:00; sat-sun:00:00-24:00` |
| `intervals` | inna częstotliwość w zadanych godzinach: `DNI:HH:MM-HH:MM=SEKUNDY` | `mon-fri:15:00-02:00=30` |
| `burst` | **zryw**: krótkie, gęste sprawdzanie wycelowane w sekundę publikacji grafiku, `DNI:GG:MM:SS`. Puste = wyłączony | `mon-sun:11:00:45` |
| `burst_seconds` | ile sekund trwa zryw (1–120) | `15` |
| `burst_interval` | odstęp w zrywie, w sekundach (dozwolone poniżej 1 s) | `0.2` |
| `sprint` | **sprint**: wąskie okno pobierania BEZ PRZERW, `DNI:GG:MM:SS`. Puste = wyłączony | `mon-sun:11:00:51` |
| `sprint_seconds` | ile sekund trwa sprint (1–30) | `4` |
| `sprint_threads` | ile wątków pobiera równolegle w sprincie (1–4) | `3` |
| `listing_url` | link do kortu (Decathlon GO); app sam podąża za zmianą adresu | `https://go.decathlon.pl/l/1c0ec93e-...` |
| `timezone` | strefa czasowa filtrów i logów | `Europe/Warsaw` |
| `auto_register` | próba automatycznego zapisu na nowy termin | `false` |
| `auto_register_dry_run` | testuje zapis bez tworzenia rezerwacji | `true` |
| `start_url` | strona wczytywana w panelu i przy odczycie tokenu | `https://go.decathlon.pl` |
| `read_interval` | co ile sekund odświeżać stronę i odczytywać token (60–3600) | `300` |
| `decathlon_token` | **awaryjnie**: ręcznie wklejony JWT, gdy nie korzystasz z panelu. Normalnie zostaw puste — token bierze się z zalogowanej przeglądarki | `` |
| `decathlon_cookie` | opcjonalne; zwykle **niepotrzebne** — GO nie używa ciasteczka sesji | `` |
| `auto_register_name` | imię i nazwisko uczestnika wysyłane w rezerwacji | `Jan Kowalski` |
| `auto_register_age` | wiek uczestnika, jeśli wydarzenie go wymaga | `34` |
| `auto_register_paid` | pozwól tworzyć transakcje także dla płatnych terminów; płatność nadal trzeba dokończyć ręcznie | `false` |
| `auto_register_max` | ile terminów maksymalnie zapisać w jednym przebiegu (0–10); `0` = nic | `1` |
| `auto_register_order` | kolejność prób: `earliest` (od najwcześniejszego) lub `latest` (od najpóźniejszego) | `latest` |
| `auto_register_salvo` | ile prób rejestracji wysyłać **równolegle** (0–6); `0`/`1` = po kolei, jak dawniej | `4` |
| `test_token` | jednorazowy test poświadczeń przy starcie (nic nie rezerwuje) | `false` |
| `clear_state` | jednorazowe czyszczenie stanu: `registered` lub `all`; puste = nic nie rób | `` |

> **Bezpieczniki auto-rejestracji.** Domyślnie `auto_register_max: 1`, więc gdy pojawi się
> naraz wiele wolnych terminów, zostanie **jedna** rezerwacja — ta najwyżej w Twojej
> kolejności (`auto_register_order`). Reszta poczeka na kolejny przebieg. Twardy błąd autoryzacji przerywa przebieg (bez dobijania się do API).
> Zacznij od `auto_register_dry_run: true` — wtedy app tylko **waliduje** zapis
> (`speculative`), niczego nie rezerwując. Dopiero gdy w logach zobaczysz
> `~ Auto-rejestracja (test, bez rezerwacji): … walidacja OK`, przełącz `dry_run` na `false`.

### Gdy token przestanie działać

Dopóki jesteś zalogowany w panelu, strona **sama odnawia token** — nic nie robisz.
Token wygasa tylko wtedy, gdy **sesja w przeglądarce padnie** (np. Decathlon wyloguje
Cię po długim czasie). Wtedy:

- w Dzienniku pojawi się `✗ strona przekierowała na logowanie` (czytnik tokenu),
- dostaniesz **push ntfy „⚠️ Token Decathlon wygasł"** (raz na incydent),
- **monitorowanie i powiadomienia działają dalej normalnie** — zarezerwujesz ręcznie
  z linku w powiadomieniu,
- termin, którego nie udało się zająć, jest **zapamiętany i ponawiany**, gdy token wróci.

Naprawa: **otwórz panel i zaloguj się ponownie**. Profil Chromium siedzi w `/data`, więc
przeżywa restart dodatku — logowanie jest potrzebne tylko po faktycznym wygaśnięciu sesji.

### Czyszczenie zapisanych terminów (`clear_state`)

App pamięta w `state.json` (katalog `/data` dodatku), na które terminy już się zapisał —
dzięki temu nie próbuje drugi raz. Jeśli **anulowałeś rezerwację** i chcesz, by app mógł
zapisać się ponownie, wyczyść tę listę:

1. **Konfiguracja** → `clear_state` ustaw na **`registered`** → **Zapisz** → **Uruchom ponownie**.
2. W **Dzienniku** zobaczysz: `🧹 Wyczyszczono listę zapisanych terminów (N szt.)`.
3. Możesz zostawić opcję ustawioną — czyszczenie działa **jednorazowo** i nie powtórzy się
   przy kolejnych restartach.

| Wartość | Co czyści |
|---------|-----------|
| `registered` | tylko listę zapisanych terminów (śledzone terminy i token zostają) |
| `all` | cały stan: zapisane + śledzone terminy **oraz zapisany token** |

> Żeby wyczyścić **ponownie** tą samą opcją, zmień wartość (np. na puste i z powrotem) —
> to celowe, żeby włączona opcja nie kasowała stanu przy każdym restarcie.
> Po `all` pierwszy przebieg zapisze nowy baseline (bez alertów o istniejących terminach).

**`filters`:** DNI to zakres (`mon-fri`) lub lista (`sat,sun`); dni: `mon tue wed thu fri sat sun`.
Okno przez północ jest OK (`15:00-02:00` = wieczór + noc do 2:00). Cały dzień = `00:00-24:00`.

**`intervals`:** ten sam format okien co `filters`, z doklejonym `=SEKUNDY`. W godzinach
pasujących do okna app sprawdza z podaną częstotliwością, poza nimi wg `check_interval`.
Np. `mon-fri:15:00-02:00=30; sat-sun:08:00-22:00=30` = co 30 s wieczorami i w weekendowe
dnie, a co `check_interval` (np. 300 s) w pozostałych porach. Puste = zawsze `check_interval`.
Minimum 2 s (niższa wartość jest podbijana, z wpisem w logu; poniżej 5 s logowane jest
ostrzeżenie — używaj tylko w wąskich oknach, bo grozi blokadą po IP). Zmiana interwału jest logowana (`⏱ aktualny interwał: ...`).

## Panel: moje rezerwacje

Ikona **Padel** w menu bocznym otwiera panel z dwiema zakładkami:

- **Rezerwacje** — wszystkie Twoje rezerwacje z konta Decathlon GO (nie tylko te
  zrobione przez dodatek): data, godziny, kort, adres, uczestnicy i stan.
- **Przeglądarka** — Chromium z sesją, w którym się logujesz (jak dotąd).

Każda nadchodząca rezerwacja ma dwa przyciski:

| Przycisk | Co robi |
|----------|---------|
| **Dodaj do kalendarza** | pobiera plik `.ics` — telefon proponuje dodanie do kalendarza (z przypomnieniem 60 min przed, adresem kortu i linkiem) |
| **Anuluj rezerwację** | anuluje ją w Decathlon GO po potwierdzeniu w oknie dialogowym |

Domyślnie widać tylko to, co jeszcze przed Tobą. Dwa przełączniki u góry dokładają
**minione** i **anulowane** terminy (anulowane są wyszarzone, bez przycisków). Panel
pamięta Twój wybór.

Przycisk **Wszystkie do kalendarza** pobiera jeden plik ze wszystkimi nadchodzącymi
terminami. Plik `.ics` jest **zdjęciem stanu z chwili pobrania** — po anulowaniu lub
nowej rezerwacji pobierz go ponownie (kalendarz się sam nie zaktualizuje).

> **Anulowanie jest nieodwracalne** i wykonuje się wyłącznie po Twoim kliknięciu —
> dodatek nigdy nie anuluje niczego sam.
>
> Po anulowaniu auto-rejestracja **nie zajmie tego terminu ponownie**: jest on na liście
> „już zapisane" w `state.json`. Jeśli chcesz, żeby mogła — użyj `clear_state: registered`
> (opis niżej).

Gdy panel pokazuje `brak tokenu — zaloguj się w panelu Padel`, przejdź na zakładkę
**Przeglądarka** i zaloguj się — lista pojawi się od razu po odświeżeniu.

### Zryw (`burst`) — polowanie na publikację grafiku

Decathlon wypuszcza grafik o **stałej porze**, 7 dni naprzód. Na kortach Targówka
zmierzone (dwa dni z rzędu, co do sekundy): **około 11:00:53**.

Zamiast młócić szybkim taktem przez kwadrans, dodatek robi **krótki zryw** wycelowany
w tę sekundę: budzi się dokładnie o `burst`, przez `burst_seconds` sprawdza co
`burst_interval`, po czym wraca do zwykłego taktu. W zrywie dodatkowo:

- **pomija lekki ping** — oszczędza on transfer, ale kosztuje całą rundę do serwera,
  a pełne dane i tak są potrzebne po identyfikatory terminów,
- korzysta z **podtrzymanego połączenia** — bez uzgadniania TCP i TLS przy każdym zapytaniu.

Zmierzony efekt: cykl sprawdzenia **427 ms → 78 ms**, a opóźnienie wykrycia spada
z ~1,2 s do ~0,25 s średnio.

> **Zryw zwykle pozwala wyłączyć agresywne `intervals`.** Okno `mon-sun:10:55-11:10=2`
> to ~478 zapytań; sam zryw z domyślnymi ustawieniami to ~50 w tym samym czasie —
> i wykrywa szybciej. Mniej ruchu na serwerze Decathlonu i mniejsze ryzyko blokady po IP.

Podłoga taktu w zrywie to 0,2 s (poza zrywem obowiązuje minimum 2 s z `intervals`) —
świadomie niższa, bo zryw trwa kilkanaście sekund, a nie godzinami.

### Salwa (`auto_register_salvo`) — kiedy przegrywasz o ułamek sekundy

Przy zapisie po kolei każda nieudana próba kosztuje ~120 ms, więc czwarty termin
dostawał strzał dopiero **~350 ms** po pierwszym. Tyle wystarczy, żeby wieczorne
godziny zdążył zabrać ktoś inny.

Salwa wysyła najbardziej pożądane terminy **naraz**, każdy własnym, wcześniej
rozgrzanym połączeniem. Zmierzone na żywym API:

| | Czas |
|---|---|
| 1 strzał | ~69 ms |
| 4 strzały po kolei | 275 ms |
| 4 strzały salwą | **73 ms** |

**Limit nadal obowiązuje.** Jeśli salwa wygra więcej terminów niż `auto_register_max`,
nadmiarowe są **natychmiast anulowane** — zostaje ten najwyżej w Twojej kolejności.
Terminy wracają do puli po ułamku sekundy, ale to znaczy, że przy włączonej salwie
w historii konta mogą pojawiać się anulowania. Jeśli Ci to przeszkadza, ustaw
`auto_register_salvo: 0` i wróć do zapisu po kolei.

Gdy anulowanie nadmiaru się **nie powiedzie** (albo serwer nie zwróci ID transakcji),
dodatek mówi o tym wprost w Dzienniku i w powiadomieniu — `anuluj ręcznie w panelu Padel`.
Nigdy nie raportuje oddania terminu, którego nie oddał.

Salwa strzela w pierwsze `auto_register_salvo` terminów według `auto_register_order`;
reszta (np. bezpieczne 15:00, o które nikt nie walczy) jest próbowana po kolei zaraz
potem — więc zabezpieczenie „przynajmniej cokolwiek" zostaje.

W Dzienniku:

```
[11:00:45.000] ⚡ Zryw START — co 0.2s przez 30s
[11:00:45.252] ⇉ Salwa gotowa: 4 ciepłych połączeń [252 ms]
[11:00:53.010] = Kort: 11 dostępnych, 5 pasujących do filtra (pobranie 104 ms)
[11:00:53.015] ⇉ Salwa: 4 prób równolegle (pt 14.08 20:00, 19:00, 18:00, 17:00)
[11:00:53.140] ! Auto-rejestracja nieudana dla pt 14.08 20:00: … 409 … [125 ms]
[11:00:53.141] ✓ Auto-rejestracja: pt 14.08 19:00 — accepted [126 ms]
```

> **Zryw musi startować kilka sekund przed publikacją** — rozgrzanie połączeń salwy
> zajmuje ~250 ms i dzieje się na jego początku. Domyślne `11:00:45` przy publikacji
> ~11:00:53 daje ośmiosekundowy zapas, czyli z dużym nadmiarem.

### Sprint (`sprint`) — ostatnie 90 ms

Nawet w zrywie między odpytaniami **stoimy bezczynnie**: przy takcie 0,2 s to średnio
~100 ms straty. Sprint na kilka sekund przechodzi w tryb ciągły — kilka wątków pobiera
bez przerw, więc obraz repertuaru jest świeży cały czas.

Zmierzone na żywym API:

| Wątki | Zapytań/s | Świeży wynik co |
|---|---|---|
| 1 | 8,3 | 117 ms |
| 2 | 17,0 | 56 ms |
| **3** | **24,0** | **35 ms** |

(pomiar na pełnych danych — tych, które sprint faktycznie pobiera; lekki ping jest
szybszy, ale nie zawiera identyfikatorów terminów, więc jest tu bezużyteczny)

Zwycięski wątek oddaje **gotowe dane** prosto do rejestracji — bez tego trzeba by
pobrać je jeszcze raz i stracić całą rundę do serwera (~92 ms) dokładnie w chwili,
gdy liczy się najbardziej.

Punkt odniesienia („co jest nowe") bierze się z **zapisanego stanu**, a nie z pierwszego
pobrania sprintu. Inaczej publikacja, która trafiłaby w pierwsze ~90 ms sprintu,
wpadłaby do punktu odniesienia i sprint nigdy by się nie odpalił.

> **Sprint ma być wąski.** Trzy wątki to ~24 zapytania na sekundę — domyślne 4 sekundy
> dają ~95 zapytań, czyli tyle co zryw przez 30 s. Nie ustawiaj `sprint_seconds`
> na kilkadziesiąt sekund, bo to już dobijanie się do serwera.

Domyślnie `mon-sun:11:00:51` przez 4 s — okno 11:00:51–11:00:55 obejmuje zmierzoną
sekundę publikacji (~11:00:53) z zapasem po obu stronach.

### Czytanie logu po polowaniu

W czasie zrywu Dziennik przechodzi na **znaczniki z milisekundami** (poza zrywem zostają
sekundy, żeby nie zaśmiecać). Dochodzą też dwie liczby, dzięki którym da się rozłożyć
przegraną na czynniki:

```
[11:00:45.000] ⚡ Zryw START — co 0.2s przez 30s
[11:00:52.800] = Kort: 2 dostępnych, 1 pasujących do filtra (pobranie 98 ms)
[11:00:53.010] = Kort: 11 dostępnych, 5 pasujących do filtra (pobranie 104 ms)
[11:00:53.130] ! Auto-rejestracja nieudana dla pt 14.08 20:00: … 409 … [118 ms]
[11:00:53.250] ✓ Auto-rejestracja: pt 14.08 15:00 — accepted [115 ms]
[11:01:15.010] ⚡ Zryw koniec (okno 11:00:45–11:01:15) — wracam do zwykłego taktu
```

Jak to czytać:

- **Moment publikacji** leży między ostatnim sprawdzeniem „bez zmian" a pierwszym z nowymi
  terminami — wyżej: między 11:00:52.800 a 11:00:53.010.
- **`(pobranie N ms)`** to czas samej rundy do serwera. Jeśli rośnie, problemem jest sieć,
  a nie ustawienia.
- **`[N ms]`** przy każdej próbie rejestracji mówi, ile kosztuje nieudany strzał i po jakim
  czasie od wykrycia poszedł ten zwycięski.

Jeśli od pierwszego wykrycia do udanej rezerwacji mija np. 240 ms, a termin i tak przepadł,
to znaczy, że konkurent jest szybszy w tej samej klasie czasowej — i dalsze skracanie
interwału niczego nie zmieni.

Jak ustalić własną porę publikacji: zostaw `intervals` na kilka sekund w szerokim oknie
i sprawdź w Dzienniku, o której pierwszy raz pojawia się nowy dzień. Potem ustaw `burst`
kilka sekund wcześniej i `intervals` możesz wyczyścić.

## Powiadomienia

- Przy każdym starcie dodatku: „✅ Monitor padla uruchomiony" (z linkiem do rezerwacji).
- Gdy zwolni się termin w Twoich godzinach: „🎾 Wolny kort padel!" (data, cena, link).
- Jeśli `auto_register` jest włączone, alert zawiera wynik próby rejestracji.

## Automatyczna rejestracja

Automatyczna rejestracja jest domyślnie wyłączona. Po włączeniu app tworzy transakcję
Decathlon GO (`/api/v2/transactions.create`) dla terminów, które przeszły filtry czasu —
wymaga to sesji zalogowanego użytkownika.

### Skąd bierze się token

Decathlon GO trzyma uwierzytelnienie w **localStorage** przeglądarki (klucz
**`go-sdk-jwt`**), a token żyje **~15 minut**. Serwerowo nie da się go odnowić
(`/api/auth/refresh` zwraca 401), ale **zalogowana strona odnawia go sama** przy
każdym ładowaniu. Dlatego dodatek trzyma prawdziwe Chromium: czytnik tokenu budzi się
tuż po wygaśnięciu, wczytuje stronę, a świeży token zapisuje do `/data/token.json`,
skąd bierze go monitor. Czytnik **nie przeszkadza Ci w panelu**: nie przeładowuje
strony, gdy token jest ważny ani gdy trwa logowanie.

W Dzienniku wygląda to tak:

```
✓ JWT odczytany, ważny do 2026-07-21 13:18:10 (jeszcze ~14 min). Kolejny odczyt za ~14 min.
✗ strona logowania otwarta (…) — czekam, dokończ w panelu
⚠ JWT w localStorage WYGASŁ 3 min temu — sesja mogła paść; sprawdź panel i zaloguj się ponownie.
```

> **Dlaczego nie w pełni serwerowo?** Odtworzenie logowania SSO z serwera Decathlon
> traktuje jako logowanie z nowego urządzenia i **wysyła kod weryfikacyjny na e-mail**.
> To celowa kontrola bezpieczeństwa i nie należy jej obchodzić. Dlatego logujesz się
> ręcznie w panelu — raz, jak na każdym nowym urządzeniu.

> **Awaryjnie** możesz wkleić `go-sdk-jwt` ręcznie w opcję `decathlon_token`
> (DevTools → Application → Local Storage). Wygrywa zawsze token o najdalszej dacie
> ważności — niezależnie od źródła. `decathlon_cookie` zostaw puste (GO nie używa
> ciasteczka sesji).

### Sprawdzenie tokenu bez czekania na wolny termin (`test_token`)

Nie musisz czekać, aż kort się zwolni, żeby sprawdzić, czy auto-rezerwacja zadziała:

1. **Konfiguracja** → `test_token: true` → **Zapisz** → **Uruchom ponownie**.
2. W **Dzienniku** zobaczysz jeden z wpisów:

```
✓ Test poświadczeń: token DZIAŁA — serwer potwierdził (HTTP 200). Ważny do …
✗ Test poświadczeń: serwer ODRZUCIŁ token (HTTP 401) — zaloguj się w panelu Padel
✗ Test poświadczeń: brak tokenu — zaloguj się w panelu Padel
```

3. Gdy zobaczysz `✓`, wyłącz `test_token` i włącz `auto_register`.

Test **niczego nie rezerwuje** — wykonuje jedno uwierzytelnione zapytanie GET. Działa
nawet przy zerowej liczbie wolnych terminów i przy wyłączonej auto-rejestracji. Gdy
`auto_register` jest włączone, ten sam test wykonuje się przy każdym starcie dodatku.

> Sesja w profilu Chromium (`/data`) i token w `/data/token.json` to **pełne
> poświadczenia konta** — trafiają też do backupów HA. Traktuj je jak hasło.

`auto_register_dry_run` jest domyślnie włączone: app wykonuje walidację/wstępną wycenę,
ale nie zapisuje uczestnika. Ustaw `auto_register_dry_run: false` dopiero po sprawdzeniu
logów i powiadomień z trybu testowego.

Domyślnie rejestrowane są tylko darmowe terminy. Dla płatnych terminów ustawienie
`auto_register_paid: true` może utworzyć transakcję oczekującą na płatność, ale płatność
trzeba dokończyć ręcznie na stronie Decathlon GO. Udane rejestracje są zapisywane w
`state.json` jako `registered_ids`, żeby app nie próbowała zapisywać drugi raz na ten sam termin.

## Auto-start

`boot: auto` w manifeście + opcja **Uruchom przy starcie** sprawiają, że dodatek wstaje
po restarcie Home Assistant. Włącz też **Watchdog**, by HA podnosił go po ewentualnym
zawieszeniu. Stan zapisywany jest w `/data` (trwały między restartami).
