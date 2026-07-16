# Padel Watch (Decathlon) — dodatek Home Assistant

Monitoruje wolne terminy padla na Decathlon GO i wysyła **push przez ntfy.sh**, gdy
pojawi się **nowy wolny termin** w wybranych godzinach.

## Instalacja (repozytorium custom)

1. W HA: **Ustawienia → Dodatki → Sklep z dodatkami → ⋮ (prawy górny róg) → Repozytoria**.
2. Dodaj adres repozytorium z tym dodatkiem (np. `https://github.com/maz00r/padel-watch`) → **Dodaj**.
3. Odśwież sklep, wejdź w **Padel Watch (Decathlon)** → **Zainstaluj**.
4. Zakładka **Konfiguracja** — ustaw opcje (niżej) → **Zapisz**.
5. Zakładka **Info** — włącz **Uruchom przy starcie** i **Watchdog**, potem **Uruchom**.
6. Logi w zakładce **Dziennik**; na telefonie zasubskrybuj temat ntfy.

> Dodatki działają tylko na **Home Assistant OS** lub **Supervised**. Na „HA Container"
> (czysty Docker) użyj zamiast tego obrazu `maz00r94/padel-watch` (zwykły kontener).

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
| `decathlon_token` | **JWT z localStorage (`go-sdk-jwt`) — to wystarczy**, app sam go odnawia | `eyJ...` |
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

JWT Decathlona żyje krótko, ale app **sam go odnawia** (`/api/auth/refresh`) i zapisuje
w stanie — wklejasz go raz. Gdy jednak łańcuch odnowień się urwie (np. dodatek był długo
wyłączony):

- dostaniesz **push ntfy „⚠️ Token Decathlon wygasł"** (raz na incydent, nie co minutę),
- w logu zobaczysz `token odrzucony (HTTP 401) — wklej świeży go-sdk-jwt`,
- **monitorowanie i powiadomienia o wolnych terminach działają dalej normalnie** —
  po prostu zarezerwujesz ręcznie z linku w powiadomieniu,
- termin, którego nie udało się zająć, jest **zapamiętany i ponawiany** po wklejeniu
  świeżego tokenu (max tyle terminów, ile i tak zapisałby `auto_register_max`).

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

## Powiadomienia

- Przy każdym starcie dodatku: „✅ Monitor padla uruchomiony" (z linkiem do rezerwacji).
- Gdy zwolni się termin w Twoich godzinach: „🎾 Wolny kort padel!" (data, cena, link).
- Jeśli `auto_register` jest włączone, alert zawiera wynik próby rejestracji.

## Automatyczna rejestracja

Automatyczna rejestracja jest domyślnie wyłączona. Po włączeniu app tworzy transakcję
Decathlon GO (`/api/v2/transactions.create`) dla terminów, które przeszły filtry czasu —
wymaga to sesji zalogowanego użytkownika.

### Wystarczy sam token (`go-sdk-jwt`)

Decathlon GO trzyma uwierzytelnienie w **localStorage**, nie w ciasteczku — dlatego
potrzebna jest jedna wartość: **`go-sdk-jwt`**.

**Skąd ją wziąć:** zalogowane `go.decathlon.pl` → DevTools → **Application**
(Firefox: **Storage**) → **Local Storage** → `https://go.decathlon.pl` → klucz
**`go-sdk-jwt`** → skopiuj wartość do opcji `decathlon_token`.

Dalej app radzi sobie sam:

1. sprawdza `exp` tokenu i **odświeża go proaktywnie**, zanim wygaśnie
   (`/api/auth/refresh` z `Authorization: Bearer <obecny jwt>` — dokładnie jak robi to
   strona Decathlona),
2. zapisuje świeży JWT w `/data/state.json`, więc przeżywa restart,
3. jeśli serwer zwróci rotowany refresh token (`rt`), też go zapamiętuje i odsyła,
4. w razie `401` odświeża i ponawia próbę.

Dzięki temu **wklejasz JWT raz** — łańcuch odnowień trwa, dopóki dodatek działa.

> **Kiedy trzeba wkleić ponownie?** Gdy dodatek jest zatrzymany dłużej niż żywotność
> tokenu, łańcuch się urywa. Dostaniesz wtedy push „⚠️ Token Decathlon wygasł" i wklejasz
> świeży `go-sdk-jwt`. Przy działającym dodatku nie powinno się to zdarzać.

> `decathlon_cookie` zostało jako opcja awaryjna, ale w GO **nie ma ciasteczka sesji** —
> w nagłówku `Cookie` znajdziesz wyłącznie Google Analytics i Hotjar. Zostaw puste.

### Sprawdzenie tokenu bez czekania na wolny termin (`test_token`)

Nie musisz czekać, aż kort się zwolni, żeby sprawdzić, czy token działa:

1. **Konfiguracja** → `test_token: true` → **Zapisz** → **Uruchom ponownie**.
2. W **Dzienniku** zobaczysz jeden z wpisów:

```
✓ Test poświadczeń: token OK, ważny do 2026-07-16 12:41:03 (jeszcze ~118 min).
✗ Test poświadczeń: nie udało się odświeżyć tokenu: <HTTPError 401: 'Unauthorized'>
✗ Test poświadczeń: brak tokenu Decathlon GO (wklej go-sdk-jwt w decathlon_token)
```

3. Gdy zobaczysz `✓`, wyłącz `test_token` i włącz `auto_register`.

Test **niczego nie rezerwuje** — tylko pobiera token. Działa nawet przy zerowej liczbie
wolnych terminów i przy wyłączonej auto-rejestracji. Gdy `auto_register` jest włączone,
ten sam test wykonuje się automatycznie przy każdym starcie dodatku.

> Cookie to **pełne poświadczenie sesji** — leży jawnym tekstem w `/data/options.json`
> dodatku i w backupach HA. Traktuj je jak hasło.

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
