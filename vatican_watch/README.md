# Watykan Watch — bilety do Muzeów Watykańskich

Lekki dodatek Home Assistant, który monitoruje **zwykłe bilety wstępu do Muzeów
Watykańskich i Kaplicy Sykstyńskiej** dla **5 osób** od 24 do 28 września 2026.
Gdy bilet stanie się dostępny, wyśle powiadomienie przez [ntfy.sh](https://ntfy.sh).
To zwykły bilet w cenie orientacyjnej około 25 EUR; ostateczną cenę i warunki zawsze
potwierdź w oficjalnym systemie przed ręcznym zakupem.

Dodatek wyłącznie odczytuje publiczne API oficjalnego systemu. Nie loguje się, nie
przechowuje danych osobowych ani płatniczych i **nigdy nie kupuje ani nie rezerwuje
biletów**. Zakup robisz ręcznie po kliknięciu powiadomienia.

## Instalacja

1. W Home Assistant otwórz **Ustawienia → Dodatki → Sklep z dodatkami → ⋮ → Repozytoria**.
2. Dodaj `https://github.com/maz00r/padel-watch`, odśwież sklep i zainstaluj
   **Watykan Watch — bilety do Muzeów Watykańskich**.
3. W **Konfiguracji** wpisz swój `ntfy_topic`, zapisz i uruchom dodatek.
4. Postęp i ewentualne problemy zobaczysz w zakładce **Dziennik**.

## Opcje

| Opcja | Znaczenie | Domyślnie |
|---|---|---|
| `ntfy_topic` | temat, który subskrybujesz w aplikacji ntfy | pusty (bez push) |
| `check_interval` | odstęp między pełnymi sprawdzeniami; dozwolone 30–3600 s | `60` |
| `start_date` | pierwszy obserwowany dzień, `RRRR-MM-DD` | `2026-09-24` |
| `end_date` | ostatni obserwowany dzień, `RRRR-MM-DD` | `2026-09-28` |

Monitor używa strefy **Europe/Rome** i sprawdza po kolei wyłącznie dni, które jeszcze
nie minęły. Pomiędzy zapytaniami jest krótka przerwa, a między cyklami mały losowy
jitter. Przy 429 lub błędach serwera wydłuża przerwę wykładniczo i respektuje nagłówek
`Retry-After`.

## Alerty i bezpieczeństwo stanu

- **Dostępne** oznacza wyłącznie status `AVAILABLE` albo `LOW_AVAILABILITY` dla
  dokładnie nazwanego zwykłego biletu `Musei Vaticani - Biglietti d'ingresso`, z pustym
  polem `suggestion`. Dostępne sugestie innych obiektów (np. Castel Gandolfo), wycieczki,
  grupy szkolne i pielgrzymki są ignorowane.
- Pierwszy odczyt dostępnych biletów **od razu alarmuje**. `SOLD_OUT` pozostaje cichy.
- Po sekwencji dostępne → wyprzedane → dostępne dostaniesz nowy alert. Kliknięcie otwiera
  oficjalną stronę wyników dla właściwej daty i 5 osób.
- Stan jest atomowo zapisywany w `/data/vatican_state.json`. Nieudany push ntfy nie
  zatwierdza nowego stanu, więc trafienie zostanie ponowione.
- Gdy dla danego dnia API nie wystawia zwykłego produktu, zapisuje diagnostyczne `MISSING`,
  ostrzega tylko raz w Dzienniku i **nie zastępuje** poprzedniego stanu dostępności. Nadal
  sprawdza ten dzień; gdy produkt się pojawi, dostępność może wywołać alert. Błędny JSON,
  brak listy produktów i inne błędy API nie zmieniają stanu. Po trzech takich błędnych
  cyklach jest jeden alarm awarii; po powrocie API jeden komunikat o odzyskaniu działania.

## Dziennik i zakończenie

Przy starcie dodatek wpisuje zakres, liczbę osób i interwał, a przy każdym cyklu krótkie
statusy dni. Po 28.09.2026 (według czasu rzymskiego) przechodzi w spokojny stan
zakończony i nie generuje dalszego ruchu.

## Rozwój

Silnik: [check_vatican.py](check_vatican.py), wyłącznie Python stdlib.

Testy bez sieci: `python3 -m unittest -v test_check_vatican`.
