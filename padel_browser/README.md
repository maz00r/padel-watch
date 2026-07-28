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
| `auto_register_order` | kolejność prób: `earliest` (od najwcześniejszego) lub `latest` (od najpóźniejszego) | `earliest` |
| `test_token` | jednorazowy test poświadczeń przy starcie (nic nie rezerwuje) | `false` |
| `clear_state` | jednorazowe czyszczenie stanu: `registered` lub `all`; puste = nic nie rób | `` |

> **Bezpieczniki auto-rejestracji.** Domyślnie `auto_register_max: 1`, więc gdy pojawi się
> naraz wiele wolnych terminów, zapis obejmie tylko **najwcześniejszy** — reszta poczeka na
> kolejny przebieg. Twardy błąd autoryzacji przerywa przebieg (bez dobijania się do API).
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
