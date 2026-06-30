# Padel Watch (Decathlon) — dodatek Home Assistant

Monitoruje wolne terminy padla na Decathlon GO i wysyła **push przez ntfy.sh**, gdy
pojawi się **nowy wolny termin** w wybranych godzinach.

## Instalacja (repozytorium custom)

1. W HA: **Ustawienia → Dodatki → Sklep z dodatkami → ⋮ (prawy górny róg) → Repozytoria**.
2. Dodaj adres repozytorium z tym dodatkiem (np. `https://github.com/maz00r94/padel-watch`) → **Dodaj**.
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
| `check_interval` | co ile sekund sprawdzać (10–3600) | `60` |
| `filters` | godziny powiadomień; okna `;`, każde `DNI:HH:MM-HH:MM` | `mon-fri:15:00-02:00; sat-sun:00:00-24:00` |
| `listing_url` | link do kortu (Decathlon GO) | `https://go.decathlon.pl/l/8df055d5-...` |
| `timezone` | strefa czasowa filtra | `Europe/Warsaw` |

**`filters`:** DNI to zakres (`mon-fri`) lub lista (`sat,sun`); dni: `mon tue wed thu fri sat sun`.
Okno przez północ jest OK (`15:00-02:00` = wieczór + noc do 2:00). Cały dzień = `00:00-24:00`.

## Powiadomienia

- Przy każdym starcie dodatku: „✅ Monitor padla uruchomiony" (z linkiem do rezerwacji).
- Gdy zwolni się termin w Twoich godzinach: „🎾 Wolny kort padel!" (data, cena, link).

## Auto-start

`boot: auto` w manifeście + opcja **Uruchom przy starcie** sprawiają, że dodatek wstaje
po restarcie Home Assistant. Włącz też **Watchdog**, by HA podnosił go po ewentualnym
zawieszeniu. Stan zapisywany jest w `/data` (trwały między restartami).
