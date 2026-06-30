# 🎾 Monitor wolnych terminów padla — Decathlon Targówek

Sprawdza co ~5 minut dostępność terminów na korcie padlowym Decathlon Targówek
i wysyła **push na telefon** (przez [ntfy.sh](https://ntfy.sh)), gdy pojawi się
**nowy wolny termin** (np. ktoś odwoła rezerwację).

Działa **w chmurze 24/7** (GitHub Actions) — nie musisz mieć włączonego komputera.
Bez kont, bez serwera, bez kosztów.

- **Kort:** `https://go.decathlon.pl/l/8df055d5-125d-4946-aa4b-962cb7ad4c94`
- **Domyślny filtr:** pon–pt 17:00–22:00 oraz soboty i niedziele cały dzień (czas Warszawa)
- **Bez zależności** — czysty Python (biblioteka standardowa)

---

## Jak to działa

Aplikacja odpytuje publiczny endpoint Decathlon GO dwustopniowo, żeby oszczędzać
transfer i być uprzejmą dla API:
1. **Lekki ping (~1 KB)** — `/api/listing/{id}` zwraca licznik `availableListingDates`.
   Gdy wynosi 0 (kort pełny — stan typowy), na tym koniec iteracji.
2. **Ciężki payload (~257 KB)** — `?include=dates` z pełną listą terminów pobierany
   **tylko wtedy, gdy licznik > 0**. Wtedy liczymy wolne terminy (termin wolny =
   nieodwołany, w przyszłości, `liczba zapisanych < limit miejsc`), filtrujemy po
   Twoich oknach czasowych i porównujemy ze stanem z `state.json`.

Gdy pojawi się termin, którego wcześniej nie było — wysyłany jest push ntfy.
Dzięki dwustopniowości typowe zużycie spada z ~185 MB/dobę do <1 MB/dobę.

> **Powiadomienia:**
> - Przy **każdym uruchomieniu aplikacji** (start/restart kontenera) przychodzi push
>   „✅ Monitor padla uruchomiony" — potwierdzenie, że działa.
> - Następnie alerty „🎾 Wolny kort padel!" tylko o **nowych** wolnych terminach.
> - **Każde** powiadomienie zawiera link do strony rezerwacji (w treści i jako akcja
>   kliknięcia — tapnięcie otwiera stronę kortu).

---

## Wdrożenie — wybierz jedną z dwóch dróg

- **A) Własny serwer / Docker** (zalecane jeśli masz serwer) — możesz sprawdzać częściej niż co 5 min, pełna kontrola. ⬇️ niżej.
- **B) GitHub Actions** (chmura, bez własnego serwera) — minimum co ~5 min. ⬇️ jeszcze niżej.

W obu przypadkach najpierw zrób **krok wspólny** (ntfy na telefonie).

---

## Krok wspólny: ntfy na telefonie

1. Zainstaluj aplikację **ntfy** ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/app/ntfy/id1625396347)).
2. Wymyśl **długą, unikalną nazwę tematu** (temat = Twój prywatny kanał; ktokolwiek
   zna nazwę, może czytać powiadomienia — dlatego nie używaj prostej nazwy).
   Przykład wygenerowany dla Ciebie: **`your-ntfy-topic-here`**
3. W aplikacji ntfy: **+ → Subscribe to topic** → wpisz tę samą nazwę → Subscribe.

---

## A) Docker / własny serwer

### Uruchomienie przez docker compose (najprościej)
1. Skopiuj cały katalog projektu na serwer.
2. W `docker-compose.yml` ustaw swój `NTFY_TOPIC` (ta sama nazwa co w apce) oraz
   ewentualnie `CHECK_INTERVAL` (sekundy, domyślnie 60 = co 1 min).
3. Uruchom:
   ```bash
   docker compose up -d --build
   docker compose logs -f          # podgląd działania
   ```
   Kontener startuje, działa w pętli i restartuje się sam (`restart: unless-stopped`),
   także po reboocie serwera (jeśli usługa Docker startuje z systemem).

### Albo czysty docker (bez compose)
```bash
docker build -t padel-watch .
docker run -d --name padel-watch --restart unless-stopped \
  -e NTFY_TOPIC="your-ntfy-topic-here" \
  -e CHECK_INTERVAL=60 \
  -v "$(pwd)/data:/data" \
  -v "$(pwd)/config.json:/app/config.json:ro" \
  padel-watch
docker logs -f padel-watch
```

### Przez Portainer

Ważne: Portainer w **Web editorze nie buduje obrazu ze źródeł** (`build: .` nie zadziała,
bo nie ma plików). Masz dwie drogi:

#### Droga A — gotowy obraz z Docker Hub + Web editor (najprościej)
Obraz jest opublikowany na Docker Hub: **`maz00r94/padel-watch:latest`** (multi-arch:
amd64 + arm64), więc niczego nie kopiujesz ani nie budujesz.
1. W Portainerze: **Stacks → Add stack → Web editor**, nazwa np. `padel-watch`.
2. Wklej zawartość pliku [`portainer-stack.yml`](portainer-stack.yml).
3. Ustaw `NTFY_TOPIC` (sekcja *Environment variables* lub wprost w treści) → **Deploy the stack**.

Portainer pobierze obraz z Docker Huba i uruchomi kontener. Stan (`state.json`) ląduje
w nazwanym wolumenie `padel_data`; `config.json` jest wbudowany w obraz.
> **Zmiana filtra** bez przebudowy obrazu: połóż własny `config.json` na serwerze i
> odkomentuj bind‑mount w [`portainer-stack.yml`](portainer-stack.yml) (linia
> `- /opt/padel/config.json:/app/config.json:ro`), potem **Update the stack**.
> Alternatywnie użyj Drogi B (Git) — wtedy filtr jedzie z repo.

#### Droga B — z repozytorium Git (obsługuje `build:` i auto-aktualizacje)
1. Wrzuć projekt do repo Git (GitHub/GitLab/Gitea).
2. W Portainerze: **Stacks → Add stack → Repository**.
3. Podaj **Repository URL**, **Reference** (np. `refs/heads/main`) i **Compose path**
   `docker-compose.yml`.
4. W *Environment variables* ustaw `NTFY_TOPIC` (i ewentualnie `CHECK_INTERVAL`),
   po czym **Deploy the stack**. Portainer sklonuje repo, zbuduje obraz i uruchomi kontener.
5. Możesz włączyć **GitOps updates / Automatic updates**, by Portainer sam wciągał zmiany z repo.

> W tej drodze działa istniejący [`docker-compose.yml`](docker-compose.yml) z `build: .`
> (bind‑mount `./config.json` i `./data` rozwiązuje się w sklonowanym katalogu stacka).

**Co warto wiedzieć:**
- `state.json` zapisywany jest do wolumenu `./data` (`/data` w kontenerze) — stan
  przetrwa restart kontenera, więc nie dostaniesz powtórek powiadomień.
- `config.json` zamontowany jako plik — zmieniasz filtr na serwerze bez przebudowy
  obrazu (po edycji: `docker compose restart`).
- `CHECK_INTERVAL` to częstotliwość w sekundach. Nie ustawiaj zbyt agresywnie
  (np. < 30 s) — bądź uprzejmy dla API Decathlonu; 60 s to rozsądna częstotliwość.
- Sterowanie: `docker compose down` (stop), `docker compose up -d` (start),
  `docker compose logs -f` (logi).

---

## B) GitHub Actions (alternatywa, bez własnego serwera)

> Najpierw wykonaj **krok wspólny** wyżej (ntfy na telefonie).

### 1. Wrzuć projekt na GitHub
W katalogu projektu:
```bash
git init
git add .
git commit -m "Monitor terminów padla"
gh repo create padel-watch --private --source=. --push
```
(Możesz też utworzyć puste repozytorium na github.com i wykonać `git remote add origin … && git push -u origin main`.)

### 2. Ustaw sekret z nazwą tematu ntfy
W repozytorium na GitHub: **Settings → Secrets and variables → Actions → New repository secret**
- **Name:** `NTFY_TOPIC`
- **Secret:** Twoja nazwa tematu (np. `your-ntfy-topic-here`)

Lub z terminala:
```bash
gh secret set NTFY_TOPIC --body "your-ntfy-topic-here"
```

### 3. Włącz i przetestuj
- Wejdź w zakładkę **Actions** w repo i włącz workflow, jeśli GitHub o to poprosi.
- Odpal ręcznie: **Actions → „Sprawdzaj wolne terminy padla" → Run workflow**.
- Na telefonie powinno przyjść powiadomienie „✅ Monitor padla uruchomiony".

Od tej chwili workflow chodzi sam co ~5 minut.

---

## Zmiana filtra (które terminy Cię interesują)

Edytuj `config.json` i zacommituj zmianę:
```json
"filters": [
  { "days": ["mon","tue","wed","thu","fri"], "start": "17:00", "end": "22:00" },
  { "days": ["sat","sun"], "start": "00:00", "end": "24:00" }
]
```
- `days`: `mon tue wed thu fri sat sun`
- godziny w formacie `HH:MM`, czas lokalny (`timezone` w config, domyślnie `Europe/Warsaw`)
- **chcesz wszystkie terminy?** ustaw `"filters": []`
- **inny/dodatkowy kort?** dopisz link do `listings` (obsługiwana jest lista)

---

## Uruchomienie lokalne (test)

```bash
NTFY_TOPIC=twoj-temat python3 check_padel.py
```
Bez `NTFY_TOPIC` skrypt działa w trybie „na sucho" — wypisze wolne terminy w
konsoli, ale nie wyśle powiadomień.

---

## Pliki

| Plik | Rola |
|------|------|
| `check_padel.py` | logika: pobranie, wyznaczenie wolnych terminów, filtr, porównanie, wysyłka ntfy. Tryb pojedynczy lub pętla (`CHECK_INTERVAL`) |
| `config.json` | konfiguracja (kort, strefa czasowa, filtry godzin) — **to edytujesz** |
| `state.json` | stan między biegami (auto; Docker: wolumen `./data`, GitHub: commit przez workflow) |
| `Dockerfile` / `docker-compose.yml` | obraz i uruchomienie na własnym serwerze |
| `.github/workflows/check.yml` | harmonogram co ~5 min w GitHub Actions |

### Zmienne środowiskowe

| Zmienna | Znaczenie | Domyślnie |
|---------|-----------|-----------|
| `NTFY_TOPIC` | nazwa tematu ntfy (push); puste = tryb „na sucho" bez wysyłki | — |
| `CHECK_INTERVAL` | sekundy między sprawdzeniami; `0` = jeden bieg i koniec | `0` (w obrazie Docker: `60`) |
| `STATE_DIR` | katalog na `state.json` | katalog skryptu (Docker: `/data`) |
| `CONFIG_PATH` | ścieżka do `config.json` | obok skryptu |

---

## Ograniczenia

- **GitHub Actions** uruchamia cron **minimum co 5 min** i bywa opóźniany przy dużym
  obciążeniu — bardzo „gorące" terminy mogą zniknąć przed kolejnym sprawdzeniem.
  Na **własnym serwerze (Docker)** ustawiasz `CHECK_INTERVAL` dowolnie (domyślnie 60 s),
  ale nie przesadzaj w dół, by nie obciążać API Decathlonu.
- Endpoint jest publiczny i nieoficjalny — gdyby Decathlon zmienił API, skrypt
  trzeba będzie zaktualizować (logika dostępności matchuje licznik kortu
  `datesStats.availableListingDates`, więc łatwo zweryfikować poprawność).
