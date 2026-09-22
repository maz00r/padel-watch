# Watykan Watch — zmiany w ofercie biletów

Dopóki nie ustawisz filtrów, dodatek Home Assistant monitoruje **wszystkie oferty**
zwracane przez oficjalny system biletowy Muzeów Watykańskich dla dni
24–28 września 2026. Wysyła
powiadomienie przez [ntfy.sh](https://ntfy.sh), gdy pojawi się albo zniknie
produkt lub zmieni się jego dostępność, komunikat, cena albo warunki uczestnictwa.

Domyślnie nie ogranicza wyników do biletu `Musei Vaticani - Biglietti d'ingresso`
i nie wymaga dostępności dla pięciu osób. Odpytuje system dla **jednej osoby**,
bo to najszerszy i najwcześniejszy sygnał, że pojawiło się choć jedno miejsce.
Można ustawić inną liczbę w opcji `visitors`. Trafienie dla jednej osoby nie
gwarantuje pięciu miejsc — liczbę miejsc zawsze potwierdź przed zakupem.

Dodatek tylko odczytuje publiczne API. Nie loguje się, nie przechowuje danych
osobowych ani płatniczych i **nigdy nie kupuje ani nie rezerwuje biletów**.
Zakup pozostaje ręczny po kliknięciu powiadomienia.

## Instalacja

1. W Home Assistant otwórz **Ustawienia → Dodatki → Sklep z dodatkami → ⋮ → Repozytoria**.
2. Dodaj `https://github.com/maz00r/padel-watch`, odśwież sklep i zainstaluj
   **Watykan Watch — bilety do Muzeów Watykańskich**.
3. W **Konfiguracji** wpisz ten sam `ntfy_topic`, którego używa aplikacja Padel,
   zapisz ustawienia i uruchom dodatek.
4. Przebieg i ewentualne problemy są widoczne w zakładce **Dziennik**.

## Opcje

| Opcja | Znaczenie | Domyślnie |
|---|---|---|
| `ntfy_topic` | temat subskrybowany w aplikacji ntfy | pusty (bez push) |
| `check_interval` | odstęp między pełnymi sprawdzeniami; 30–3600 s | `60` |
| `start_date` | pierwszy obserwowany dzień, `RRRR-MM-DD` | `2026-09-24` |
| `end_date` | ostatni obserwowany dzień, `RRRR-MM-DD` | `2026-09-28` |
| `visitors` | liczba osób używana w wyszukiwaniu, 1–20 | `1` |
| `ticket_types` | fragmenty nazw rodzajów biletów rozdzielone średnikiem; puste = wszystkie | puste |
| `ticket_statuses` | statusy rozdzielone przecinkiem; puste = wszystkie | puste |

Przykład: `ticket_types: "Musei Vaticani - Biglietti d'ingresso; Visita guidata"`
i `ticket_statuses: "AVAILABLE, LOW_AVAILABILITY"`. Nazwy są dopasowywane bez
rozróżniania wielkości liter i znaków diakrytycznych, jako fragment pełnej nazwy
oferty. Statusy wpisuj tak, jak zwraca je API, np. `AVAILABLE`, `LOW_AVAILABILITY`,
`SOLD_OUT`. Można wybrać jeden albo kilka rodzajów i statusów.

Po wybraniu statusów powiadomienia dotyczą uzyskania **lub utraty** wybranego statusu,
a także innych zmian w ofercie, gdy jest w tym statusie. Na przykład wybór
`AVAILABLE` powiadamia o pojawieniu się dostępnego biletu, przejściu z `SOLD_OUT`
do `AVAILABLE`, przejściu z `AVAILABLE` do `SOLD_OUT` oraz zniknięciu dostępnej oferty.
Oferty są nadal sprawdzane przy każdym cyklu, więc ponowna dostępność nie ginie.
Zmiana filtra lub liczby osób tworzy nowy punkt odniesienia bez alertów o dawnych
ofertach.

Monitor działa według strefy **Europe/Rome** i pomija dni, które już minęły.
Każdy cykl pobiera wszystkie strony wyników dla każdego aktywnego dnia. Pomiędzy
zapytaniami robi krótkie przerwy, a do odstępu między cyklami dodaje niewielki
losowy jitter. Przy HTTP 429 albo błędach serwera stosuje wykładnicze wycofanie
i respektuje `Retry-After`.

## Co jest uznawane za zmianę

- dodanie lub usunięcie dowolnego produktu, w tym wycieczki, oferty innego obiektu,
  produktu szkolnego albo pielgrzymkowego;
- zmiana statusu dostępności, np. `SOLD_OUT` → `AVAILABLE`;
- zmiana komunikatu dostępności albo ceny od;
- zmiana zakresu uczestników lub dostępnych typów odwiedzających;
- zmiana nazwy, opisu albo sugestii — widoczna jako usunięcie starej i dodanie
  nowej wersji produktu.

Kolejność wyników, techniczny identyfikator produktu i nazwa obrazu są pomijane,
ponieważ mogą zmieniać się bez znaczenia dla kupującego. Pierwszy poprawny cykl
tworzy punkt odniesienia i nie generuje kilkudziesięciu fałszywych alertów.
Następne cykle zgłaszają każdą różnicę. Zmiana prowadząca do stanu `AVAILABLE`
lub `LOW_AVAILABILITY` ma wysoki priorytet; pozostałe zmiany mają zwykły priorytet.

## Stan i odporność na błędy

Stan jest atomowo zapisywany w `/data/vatican_state.json`, więc przetrwa restart.
Nieudany push ntfy nie zatwierdza nowego obrazu oferty i zostanie ponowiony w
następnym cyklu. Błędna lub niepełna odpowiedź API również nie nadpisuje ostatniego
poprawnego stanu. Po trzech błędnych cyklach wysyłany jest jeden alarm awarii,
a po powrocie API — jeden komunikat o odzyskaniu działania.

Zmiana opcji `visitors`, `ticket_types` lub `ticket_statuses` tworzy nowy punkt
odniesienia bez fałszywego alertu. Po końcu zakresu dat dodatek kończy
sprawdzanie i nie generuje dalszego ruchu.

## Rozwój

Silnik [check_vatican.py](check_vatican.py) korzysta wyłącznie z biblioteki
standardowej Pythona. Testy bez sieci: `python3 -m unittest -v test_check_vatican`.
