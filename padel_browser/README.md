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
| `ntfy_topic` | temat ntfy — **traktuj jak hasło**, patrz niżej | `your-ntfy-topic-here` |
| `check_interval` | bazowa częstotliwość sprawdzania w sekundach (10–3600) | `60` |
| `log_level` | ile ma być w logu: `debug` / `info` / `warn` / `error` | `info` |
| `filters` | godziny powiadomień; okna `;`, każde `DNI:HH:MM-HH:MM` | `mon-fri:15:00-02:00; sat-sun:00:00-24:00` |
| `intervals` | inna częstotliwość w zadanych godzinach: `DNI:HH:MM-HH:MM=SEKUNDY` | `mon-fri:15:00-02:00=30` |
| `burst` | **zryw**: krótkie, gęste sprawdzanie wycelowane w sekundę publikacji grafiku, `DNI:GG:MM:SS`. Puste = wyłączony | `mon-sun:11:00:45` |
| `burst_seconds` | ile sekund trwa zryw (1–120) | `15` |
| `burst_interval` | odstęp w zrywie, w sekundach (dozwolone poniżej 1 s) | `0.2` |
| `sprint` | **sprint**: wąskie okno pobierania BEZ PRZERW, `DNI:GG:MM:SS`. Puste = wyłączony | `mon-sun:11:00:51` |
| `sprint_seconds` | ile sekund trwa sprint (1–30) | `4` |
| `sprint_threads` | ile wątków pobiera równolegle w sprincie (1–4) | `3` |
| `auto_login` | gdy sesja GO wygaśnie, sam kliknij „ZALOGUJ SIĘ” w przeglądarce dodatku (nie wpisuje żadnych danych) | `true` |
| `token_check_before` | ile minut przed zrywem sprawdzić sesję i ostrzec pushem, gdy nie żyje (0–240); `0` = wyłączone | `30` |
| `remote_url` | adres funkcji AWS w eu-west-1, która wykona sprint i salwę. Puste = wszystko lokalnie | `` |
| `remote_secret` | sekret do tej funkcji (ta sama wartość co `PADEL_SECRET` w Lambdzie) | `` |
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
| `auto_register_lead` | najcenniejszy termin leci sam i pierwszy (**hipoteza obalona 31.08 — trzymaj wyłączone**) | `false` |
| `auto_register_hedge` | ile **równoległych zapisów w najcenniejszy termin** (1 = wyłączone, maks. 3) | `2` |
| `auto_register_salvo` | ile prób rejestracji wysyłać **równolegle** (0–6); `0`/`1` = po kolei, jak dawniej | `6` |
| `auto_register_stagger` | odstęp w ms między strzałami salwy (0–100); `0` = wszystkie naraz | `8` |
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

**Najpierw dodatek spróbuje sam.** Sesja u dostawcy tożsamości Decathlona żyje dłużej
niż sama sesja GO, więc bardzo często wystarczy kliknąć „ZALOGUJ SIĘ" — przeglądarka
odbija się przez OAuth i wraca zalogowana bez wpisywania czegokolwiek. Dodatek robi
dokładnie to jedno kliknięcie (opcja `auto_login`, domyślnie włączona).

Nie wpisuje przy tym **żadnych danych** — ani loginu, ani hasła, ani kodu z maila.
Gdy po kliknięciu pojawi się formularz, poddaje się i prosi Ciebie. Próbuje najwyżej
trzy razy, z dziesięciominutowymi przerwami.

Naprawa, gdy ciche logowanie nie wystarczy: **otwórz panel i zaloguj się ponownie**. Profil Chromium siedzi w `/data`, więc
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

## Strzał redundantny

Czas, w jakim serwer przetwarza nasz zapis, jest **loterią**. Zmierzone 30–31.08:
61, 62, 63, 71, 115, 150, 157, 178, 188, 236, 251, **730** ms — bez związku z czymkolwiek,
co robimy. Ten sam termin trafiony dwa razy dawał 62 i 251 ms (30.08, 12:00) oraz
700 i 68 ms (25.08, 20:00).

Skoro to losowanie, jedno można zamienić na **minimum z kilku**: `auto_register_hedge`
posyła 2–3 równoległe zapisy w najcenniejszy termin i liczy ten, który wróci pierwszy.
Kopie startują **razem** (odstęp salwy ich nie rozsuwa) — inaczej nie byłyby
równoczesnymi losowaniami.

Dotyczy **wyłącznie czołowego celu**. Rozciąganie tego na całą salwę mnożyłoby ryzyko
podwójnej rezerwacji bez żadnych danych, że pomaga.

**Jak czytać `✗` przy kopii.** Serwer odpowiada `Booking is already exists`, gdy miejsce
jest już nasze, i `No available seats`, gdy wziął je ktoś inny. Krzyżyk przy drugiej
kopii to prawie zawsze to pierwsze — nasz własny zapis odbijający się od naszej własnej
rezerwacji. W Dzienniku ma etykietę `miejsce już nasze` i nie liczy się do przegranych.

**Podwójna rezerwacja nie jest możliwa** — limit miejsc w terminie wynosi 1, więc gdy
jedna kopia zapisze się skutecznie, druga z definicji dostaje 409. Nie ma tu żadnego
anulowania i nie ma czego pilnować. Jedyne, co dodatek musi zrobić z dwiema odpowiedziami
na ten sam termin, to **nie pozwolić wolniejszej porażce nadpisać szybszego zwycięstwa** —
inaczej powiadomienie skłamałoby, że termin przepadł.

W Dzienniku kopie mają znacznik `⧉` przy godzinie.

**Jak ocenić, czy działa:** porównaj czasy kopii tego samego terminu. Jeśli regularnie
różnią się kilkukrotnie (np. 80 ms i 600 ms), redundancja robi dokładnie to, po co
powstała. Jeśli obie kopie wracają w podobnym czasie, loterii nie ma i opcję można
zdjąć do `1`.

## Strzał czołowy — hipoteza obalona

Dziennik z 25.08 pokazał cztery strzały, które ruszyły w tej samej chwili
(`start +0 / +0 / +8 / +16 ms`) i wróciły po **84 / 700 / 800 / 725 ms**. Wyglądało to
na kolejkę po stronie serwera, w której sami spychamy najcenniejszą godzinę na koniec —
strzelając w nią razem z pięcioma innymi terminami. Stąd `auto_register_lead`:
najpożądańszy termin miał iść sam i pierwszy.

**Pierwsza publikacja po włączeniu (31.08) obaliła to jednoznacznie:**

```
pon 07.09 19:00 ✗ +0 ms → 730 ms (dane sprzed 1 ms)
pon 07.09 17:00 ✗ +0 ms → 188 ms (dane sprzed 732 ms)
```

Czołowy strzał poszedł sam, na danych sprzed 1 ms — i trwał 730 ms. Przy zerowej
konkurencji z naszej strony. Kolejka nie jest nasza. A drugi strzał zapłacił za
czekanie: ruszył na informacji starszej o 732 ms.

Mediany: strzał samotny **251 ms** (n=4), strzał z salwy **157 ms** (n=8).
Opcja jest **domyślnie wyłączona** i taka ma zostać.


## Wiek danych w Dzienniku

Każdy strzał raportuje teraz `dane sprzed N ms` — ile czasu minęło od chwili, gdy serwer
oddał nam grafik, do chwili, gdy ruszył zapis.

Bez tej liczby nie dało się odróżnić dwóch zupełnie różnych porażek:

- **byliśmy za wolni** — zapis ruszył późno, choć dane były świeże,
- **patrzyliśmy na nieaktualny grafik** — zapis ruszył natychmiast, ale miejsce zniknęło,
  zanim w ogóle zapytaliśmy.

24.08 strzał w 20:00 trwał **74 ms** i wrócił 409. Sam zapis był na podłodze tego, co
osiągalne — więc winna była informacja, nie prędkość. To drugi rodzaj porażki i wymaga
zupełnie innej poprawki niż przyspieszanie strzału.


## Dlaczego zryw patrzy podczas strzelania

Publikacja przychodzi **partiami**, a nasz zapis trwa dłużej niż odstęp między nimi.
03.09 czekanie na odpowiedź serwera zajęło 1303 ms i przez cały ten czas nikt nie patrzył
na grafik — liczba dostępnych terminów skoczyła w tym oknie z 4 na 10, a wszystkie
wieczorne godziny zniknęły, zanim spojrzeliśmy ponownie.

Dlatego w zrywie, na czas rejestracji, rusza osobny wątek pobierający grafik bez przerw.
Terminy, które pojawią się w trakcie zapisu, dostają strzał **natychmiast** — przed
zapisem stanu, dziennikiem i powiadomieniami, bo one kosztowały kolejne ~470 ms.

W logu widać to jako:

```
⇉ Druga fala: 2 terminy pojawiły się w trakcie zapisu (7 pobrań) — strzelam od razu
```

Poza zrywem obserwator nie działa — tam ciągłe pobieranie to tylko zbędny ruch.

## Okno sprintu a rozrzut publikacji

Pora publikacji **nie jest stała**. Zmierzone przez 11 dni (23.08–03.09):

```
11:00:13  13  15  15  25  36  36  36  37  37  42      -> rozrzut 29 s
```

Okno sprintu musi ten rozrzut **pokryć**, bo poza nim Irlandia w ogóle nie obserwuje,
a zapas lokalny strzela pięciokrotnie wolniej (448–1303 ms wobec 66–178 ms z regionu).
Wąskie okno 11:00:30 + 10 s trafiało w 5 dni na 11.

Zalecane: `sprint: mon-sun:11:00:05`, `sprint_seconds: 40`.

**Timeout funkcji Lambda musi być większy niż okno sprintu** (przy 40 s ustaw 60 s).
Inaczej AWS ubije funkcję w trakcie obserwacji i nie odda nawet tego, co zdążyła
zarezerwować.

## Ciche logowanie

Gdy sesja w Decathlon GO wygaśnie, a sesja u dostawcy tożsamości jeszcze żyje, samo
kliknięcie „ZALOGUJ SIĘ" wystarcza — przeglądarka odbija się przez OAuth i wraca
zalogowana. Dodatek robi dokładnie to jedno kliknięcie we własnej przeglądarce.
Nie wpisuje loginu, hasła ani kodu z maila; jeśli pojawi się formularz, prosi Cię
o ręczne zalogowanie.

Bezpieczniki: najwyżej 3 próby, co najmniej 10 minut przerwy między nimi.

**Licznik prób kasuje każdy udany odczyt tokenu** — także po logowaniu ręcznym. Bez tego
jedna nieudana noc wyłączała ciche logowanie aż do restartu dodatku (naprawione w 0.20.1).
Niezależnie od tego limit przedawnia się po 6 godzinach ciszy, żeby awaria po stronie
Decathlona nie zabierała funkcji na stałe.

`auto_login: false` wyłącza mechanizm całkowicie.

**Powiadomienie o problemie z tokenem czeka 2 minuty.** Ciche logowanie żyje w drugim
procesie: po nieudanym odczycie ponawia po 45 s, a odbicie przez OAuth trwa do ~20 s.
Push wysłany natychmiast trafiał do Ciebie, zanim aplikacja w ogóle spróbowała się
naprawić — i najczęściej okazywał się fałszywym alarmem. Teraz przychodzi tylko wtedy,
gdy problem przetrwał karencję. W logu widać to jako:

```
~ Problem z tokenem (brak tokenu…) — daję cichemu logowaniu 120s, zanim powiadomię.
```

## Poziomy logowania

Doba pracy dodatku to ~2000 linii, a ~95 % z nich to dwa powtarzalne komunikaty:
`= Kort: N dostępnych…` przy każdym pytaniu o grafik i `Brak nowych wolnych terminów.`
Szukanie w tym publikacji albo nieudanego strzału to praca, której nie trzeba wykonywać.

| Poziom | Co widać |
|--------|----------|
| `debug` | wszystko — każde odpytanie, każdy pusty cykl, bicie serca czytnika tokenu |
| `info` (domyślnie) | zdarzenia: publikacja, grafik dnia, rezerwacje, salwa, zryw, dziennik polowań, powrót tokenu po awarii — **oraz pełny zapis z sekund zrywu** |
| `warn` | tylko kłopoty: `!` i `⚠` — nieudane powiadomienia, wygasły JWT, rozjazd okna publikacji |
| `error` | wyłącznie `✗` — martwy token, odrzucony strzał |

Dwie rzeczy warte uwagi:

- **W zrywie rutynowe linie wracają na `info`.** To one są materiałem dowodowym przy
  analizie polowania — bez nich nie widać, o której sekundzie termin pojawił się w API
  ani ile trwało pobranie. Poza zrywem te same linie są czystym szumem i idą na `debug`.
- **Bicie serca czytnika tokenu (`✓ JWT odczytany`) jest na `debug`**, ale odczyt po
  serii błędów wraca na `info`. Interesuje nas moment, w którym sesja wraca do życia,
  a nie 290 dziennych potwierdzeń, że nadal żyje.

Poziom obowiązuje oba procesy dodatku (monitor i czytnik tokenu). Zdalna Lambda czyta
ten sam `LOG_LEVEL` — ustaw go w zmiennych środowiskowych funkcji, jeśli chcesz ciszej
w CloudWatch. Błędna nazwa poziomu nie wycisza dodatku: nierozpoznana wartość cofa się
do `info`, żeby literówka w konfiguracji nie oślepiła cię w dniu publikacji.

## Panel: dziennik polowań

Zakładka **Polowania** to jeden wpis na dobę z tym, co naprawdę się liczy: o której
przyszła publikacja, ile terminów było w grafiku, co zdobyte, co przegrane i jak szybko
poszedł każdy strzał. Powstała po to, żeby **nie trzeba było codziennie przeglądać
Dziennika** — a te liczby i tak w nim giną wśród tysięcy linii.

Kluczowa linia w każdym wpisie odróżnia dwa problemy, które wcześniej wyglądały
tak samo:

- **Przegrane: 20:00 (zajęty 409)** — widzieliśmy termin wolny, strzeliliśmy,
  przegraliśmy zapis. To jest wyścig i da się go optymalizować.
- **Nigdy nie pokazane jako wolne: 20:00** — termin zniknął, zanim w ogóle spojrzeliśmy.
  Tego **nie wygra żadna prędkość ani żaden region**.

Push przychodzi **tylko wtedy, gdy dzień wymaga uwagi**:

- publikacja wypadła **poza oknem zrywu** (wtedy zryw i sprint się nie odpalają),
- **żadna rezerwacja się nie udała** mimo pasujących terminów.

Raz na dobę, nie przy każdej partii. Powiadomienie, które przychodzi codziennie,
przestaje być czytane.

> Skąd to się wzięło: 23.08.2026 publikacja przesunęła się z ~11:00:53 na **11:00:15**,
> więc zryw o 11:00:30 spóźnił się na własne przyjęcie i pięć z dwunastu terminów
> zniknęło, zanim monitor spojrzał. Wyszło to na jaw tylko dlatego, że ktoś przeczytał
> log. Teraz taki rozjazd zgłasza się sam.

Historia trzymana jest w `hunts.json` (katalog `/data`), ostatnie 60 dni.

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
terminami — razem z **odwołaniami** tych, które anulowałeś. Po ponownym wczytaniu
kalendarz doda nowe wydarzenia i usunie odwołane.

### Gdy anulujesz rezerwację

Termin **nie zniknie sam** z kalendarza — trzeba mu o tym powiedzieć. Są dwie drogi:

- zaraz po anulowaniu w panelu pojawia się link **pobierz odwołanie** — jedno kliknięcie
  i kalendarz usuwa ten jeden wpis,
- albo pobierz ponownie **Wszystkie do kalendarza** — plik niesie wtedy komplet zmian.

Technicznie: odwołanie ma ten sam `UID` co pierwotny wpis, `STATUS:CANCELLED`
i podniesiony `SEQUENCE`. Kalendarz łączy je w parę po `UID` i przyjmuje zmianę tylko
dlatego, że numer wersji jest wyższy. Pojedynczy plik dostaje dodatkowo `METHOD:CANCEL`,
bo aplikacje kalendarza reagują na niego pewniej niż na sam status.

> Reakcja bywa różna w różnych aplikacjach: część usuwa wpis od razu, część zostawia
> go przekreślonego jako „odwołany". Obie formy są poprawne.

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

#### Dlaczego strzały mają odstęp

14.08 zmierzone z Irlandii: cztery strzały ruszyły z `start +0 ms` **co do jednego**,
a wróciły po **21 / 37 / 117 / 282 ms**. Skoro startują razem, ten „schodek" powstaje
po stronie Decathlona — najpewniej serwer serializuje zapisy **per konto**, więc nasze
własne strzały stoją w kolejce jeden za drugim.

Miejsce w kolejce było przy tym **losowe**: 15:00, wymienione jako ostatnie, weszło
w 37 ms, a 17:00 jako trzecie czekało 282 ms. Odstęp (`auto_register_stagger`, domyślnie
8 ms) sprawia, że kolejność jest Twoja — najbardziej pożądany termin wchodzi pierwszy.
Przy 4 strzałach ostatni rusza 24 ms później, czyli o rząd wielkości mniej niż
obserwowany rozrzut.

To wynika z hipotezy, nie z pewnika. `auto_register_stagger: 0` wraca do strzelania
wszystkim naraz.

**Także pojedynczy termin idzie przez pulę salwy.** Publikacja przychodzi partiami
i taka partia potrafi mieć jeden termin — a wątek główny nie jest rozgrzewany.
9.08 kosztowało to termin: samotne 19:00 dostało strzał w 319 ms, podczas gdy strzały
z puli w tej samej sekundzie schodziły w 57–84 ms.

#### Pierwsza rejestracja dnia kosztuje ~300 ms i nie wiadomo dlaczego

Powtarzalnie: pierwszy zapis po publikacji zajmuje 232–319 ms, a każdy następny
w tej samej sekundzie 57–84 ms. Sprawdzone i **wykluczone**: wystygnięte połączenie
(gniazdo przeżywa 12 s bezczynności; rozgrzewka 2,5 s przed strzałem nic nie dała)
oraz walidacja tokenu przy pierwszym użyciu (uwierzytelniony `users.getMe` w tej samej
sekundzie kosztował 48 ms). Najpewniej coś po stronie Decathlonu przy pierwszym zapisie
do świeżo opublikowanego grafiku — czyli poza naszym zasięgiem, bo grafik przed
publikacją nie istnieje. W praktyce nie kosztowało dotąd żadnego terminu.

W Dzienniku:

```
[11:00:45.000] ⚡ Zryw START — co 0.2s przez 30s
[11:00:45.252] ⇉ Połączenia gotowe (salwa 6, sprint 3) [252 ms], uwierzytelnienie 70–310 ms
[11:00:53.010] = Kort: 11 dostępnych, 5 pasujących do filtra (pobranie 104 ms)
[11:00:53.015] ⇉ Salwa: 4 prób równolegle (pt 14.08 20:00, 19:00, 18:00, 17:00)
[11:00:53.140] ! Auto-rejestracja nieudana dla pt 14.08 20:00: … 409 … [125 ms]
[11:00:53.141] ✓ Auto-rejestracja: pt 14.08 19:00 — accepted [126 ms]
```

> **Zryw musi startować kilka sekund przed publikacją** — rozgrzanie połączeń salwy
> zajmuje ~250 ms i dzieje się na jego początku. Domyślne `11:00:45` przy publikacji
> ~11:00:53 daje ośmiosekundowy zapas, czyli z dużym nadmiarem.

### Ile terminów w ogóle było — licznik grafiku

Gdy pojawią się nowe terminy, Dziennik pokazuje cały grafik danego dnia:

```
📋 Grafik na pon 17.08: 3 wolne z 14 — 11 zajętych, zanim zobaczyliśmy grafik
```

To odpowiedź na pytanie „a może było więcej terminów, tylko ich nie złapaliśmy".
Bez tej liczby brakująca godzina wygląda tak samo, niezależnie od tego, czy nikt jej
nie wystawił, czy ktoś był szybszy. **W chwili publikacji** liczba „zajętych" to
dokładnie terminy, których nigdy nie zobaczyliśmy jako wolne. Później to już zwykłe
rezerwacje innych graczy.

### Powiadomienia w zrywie czekają

Push do ntfy.sh to zapytanie do zupełnie innego serwera. W zrywie wysyłaliśmy je
w najgorętszej sekundzie dnia, a monitor przez ten czas **przestawał patrzeć** na
grafik — zmierzone 643–819 ms między wykryciem jednej partii terminów a wznowieniem
sprintu, choć publikacja wciąż trwała.

Dlatego **w zrywie powiadomienia lądują w kolejce** i wychodzą po zamknięciu okna
(w Dzienniku: `📨 Powiadomienia odłożone na po zrywie`, potem `📨 Wysłano N odłożonych`).
Rezerwacja dzieje się natychmiast, jak dotąd — opóźniony jest wyłącznie push.
**Poza zrywem nic się nie zmienia**: powiadomienie leci od razu.

### Zdalny strzał z Irlandii (`remote_url`) — opcjonalny

`go.decathlon.pl` stoi w AWS eu-west-1. Zmierzone: runda do serwera **0,5 ms** stamtąd
wobec ~42 ms z domu, a ścieżka „termin się pojawia → nasze żądanie dociera" spada
ze **~107 ms do ~32 ms**. Konkurent zabiera wieczorne godziny w ~200 ms, więc to
spory kawałek jego przewagi.

Przenosi się **tylko te kilkanaście sekund**: sprint i salwa. Logowanie, przeglądarka,
token, panel, kalendarz, powiadomienia i stan zostają w Home Assistancie. Token leci
w treści żądania i **nigdzie w AWS nie jest zapisywany**.

Pełna instrukcja: [`aws_remote/README.md`](../aws_remote/README.md). Koszt: praktycznie
zero (~530 GB-s miesięcznie z 400 000 darmowych).

Gdy Irlandia odmówi lub nie odpowie, dodatek poluje lokalnie. Po **timeoucie** nie
strzela powtórnie — funkcja mogła zdążyć zarezerwować — i ostrzega w Dzienniku,
żeby sprawdzić panel.

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

### Ile z opóźnienia to sieć

Przy starcie dodatek mierzy rundę do serwera Decathlonu i wypisuje ją w Dzienniku:

```
📡 Runda do go.decathlon.pl: 58 ms (min 55, max 63). Rezerwacja potrzebuje DWÓCH
   takich rund (~116 ms) — tego nie da się skrócić kodem.
```

Ścieżka od publikacji do strzału to mniej więcej: **wykrycie (~20 ms) + dwie rundy
do serwera**. Jeśli runda wynosi 80 ms, sama sieć zjada ~160 ms z ~200 ms całości —
i żadne ustawienie tego nie zmieni.

Serwery Decathlon GO stoją w **AWS w Irlandii**. Z łącza domowego w Polsce to
zwykle 55–85 ms. Jeśli widzisz górne wartości, sprawdź, czy serwer z Home Assistantem
stoi na kablu — WiFi potrafi dołożyć kilkanaście milisekund, a liczy się to podwójnie.

> **VPN tu nie pomoże.** Nie skraca drogi, tylko dokłada przystanek: ruch idzie
> dom → serwer VPN → Decathlon. Pomogłoby wyłącznie przeniesienie samego kodu bliżej
> serwera (np. maszyna w AWS eu-west-1, runda ~2 ms) — to jednak oznacza wyprowadzenie
> tokenu sesji poza Home Assistant.

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

## Temat ntfy to jedyne zabezpieczenie

ntfy.sh nie ma kont ani haseł. **Kto zna nazwę tematu, czyta wszystkie Twoje
powiadomienia i może wysyłać Ci własne.** Nazwa tematu jest jedynym zabezpieczeniem,
jakie tam istnieje.

Zostawiona wartość domyślna (`your-ntfy-topic-here`) znaczy, że Twoje powiadomienia
o rezerwacjach czyta każdy inny użytkownik tego dodatku, który też jej nie zmienił —
i że Ty czytasz jego. Dodatek ostrzega o tym przy starcie.

Ustaw co najmniej 16 losowych znaków, na przykład:

```bash
python3 -c "import secrets; print('padel-' + secrets.token_urlsafe(16))"
```

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
