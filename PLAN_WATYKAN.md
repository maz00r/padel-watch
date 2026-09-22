# Plan wdrożenia: Watykan Watch

## 1. Cel i przyjęte założenia

- Osobny dodatek Home Assistant w tym samym repozytorium co Padel i Kino.
- Monitorowany termin: 24–28 września 2026 (włącznie).
- Liczba odwiedzających: 5.
- Interesujący produkt: zwykłe bilety wstępu do Muzeów Watykańskich i Kaplicy
  Sykstyńskiej, prezentowane w oficjalnym systemie jako
  `Musei Vaticani - Biglietti d'ingresso`. Identyfikator produktu jest zależny
  od dnia, więc nie może być podstawą dopasowania między różnymi datami.
- Zakup pozostaje ręczny. Dodatek wykrywa dostępność i prowadzi użytkownika do
  oficjalnego systemu, ale nie składa zamówienia i nie przechowuje danych osobowych
  ani płatniczych.
- Powiadomienia są wysyłane przez ntfy.sh, tak jak w istniejących dodatkach.

## 2. Potwierdzony punkt integracji

Oficjalna strona korzysta z publicznego zapytania tylko do odczytu:

`GET https://tickets.museivaticani.va/api/search/result`

Parametry istotne dla monitora:

- `lang=it`
- `visitorNum=5`
- `visitDate=DD/MM/YYYY`
- `area=1` (Musei Vaticani)
- `who=`
- `page=0`

Dla 24 września 2026 i 5 osób API zwraca właściwy produkt ze stanem
`SOLD_OUT`. Za dostępne uznajemy wyłącznie `AVAILABLE` i
`LOW_AVAILABILITY`. Sugestie innych obiektów (np. Castel Gandolfo) muszą być
ignorowane nawet wtedy, gdy są dostępne.

## 3. Architektura dodatku

Nowy katalog `vatican_watch/` będzie zawierał:

- `check_vatican.py` — lekki monitor w Pythonie, wyłącznie biblioteka standardowa;
- `run.sh` — mapowanie opcji Home Assistant na zmienne środowiskowe;
- `config.yaml` — metadane, wartości domyślne i walidacja opcji;
- `Dockerfile` — mały obraz Alpine z Pythonem i danymi stref czasowych;
- `README.md` — instalacja, konfiguracja, zachowanie alertów i diagnostyka;
- `CHANGELOG.md` — historia wersji od `0.1.0`.

Testy jednostkowe trafią do `test_check_vatican.py`. Główny `README.md` i
`repository.yaml` zostaną rozszerzone o trzeci dodatek.

## 4. Algorytm sprawdzania

1. Zweryfikować konfigurację: temat ntfy, zakres dat, liczba osób, interwał i
   strefa czasowa.
2. W każdym cyklu wysłać po jednym zapytaniu dla każdego jeszcze aktualnego dnia
   z zakresu.
3. Z odpowiedzi wybrać wyłącznie zwykły bilet do Muzeów Watykańskich po
   znormalizowanej nazwie i pustym polu `suggestion`. Zwrócony identyfikator
   zachować tylko diagnostycznie, ponieważ jest inny dla każdego dnia.
4. Odrzucić rekordy z polem `suggestion` oraz produkty prowadzone, szkolne,
   pielgrzymkowe i dotyczące innych obiektów.
5. Zapisać dla każdego dnia stan `AVAILABLE`, `LOW_AVAILABILITY`, `SOLD_OUT`,
   `NOT_ALLOWED` albo `MISSING`.
6. Wysłać alert tylko po przejściu dnia do stanu dostępnego. Jeśli push się nie
   powiedzie, nie zatwierdzać nowego stanu, aby następny cykl ponowił alert.
7. Po ponownym wyprzedaniu zapamiętać zmianę; kolejne pojawienie się biletów ma
   wywołać nowy alert.
8. Usuwać z aktywnego sprawdzania dni, które już minęły w strefie
   `Europe/Rome`, a po końcu całego zakresu przejść w spokojny tryb zakończony.

## 5. Częstotliwość i ochrona strony

- Domyślnie: jeden cykl co 60 sekund, czyli maksymalnie pięć małych zapytań na
  minutę na początku zakresu.
- Konfigurowalny zakres: 30–3600 sekund.
- Krótka przerwa pomiędzy datami i niewielki losowy jitter między cyklami, aby
  nie generować idealnie okresowego ruchu.
- Dla HTTP 429 i błędów 5xx: wykładnicze wycofanie z limitem, honorowanie
  `Retry-After`, brak modyfikacji stanu dostępności.
- Umiarkowany timeout, kompresja gzip i jednoznaczny User-Agent.
- Żadnego omijania CAPTCHA, blokad ani zabezpieczeń; jeśli API przestanie być
  publicznie dostępne, dodatek ma zgłosić czytelny błąd zamiast obchodzić ochronę.

## 6. Powiadomienia

- Start: informacja, jaki produkt, daty i liczbę osób monitoruje dodatek.
- Trafienie: powiadomienie ntfy o wysokim priorytecie z datą, statusem i linkiem
  otwierającym oficjalne wyniki dla 5 osób.
- Błąd trwały: pojedynczy alarm po serii kolejnych nieudanych cykli, bez zalewu
  wiadomości; wiadomość o odzyskaniu działania po powrocie API.
- Brak zmian: cisza, jedynie zwięzły wpis diagnostyczny w dzienniku.

## 7. Niezawodność i stan

- Stan w `/data/vatican_state.json`, zapisywany atomowo, aby przetrwał restart
  dodatku i awarię podczas zapisu.
- Rozdzielenie `last_observed` od `last_notified`, aby błąd ntfy nie zgubił
  trafienia.
- Uszkodzony plik stanu nie zatrzymuje procesu: jest zgłaszany, a monitor buduje
  nowy bez fałszywego alertu.
- Odpowiedź bez właściwego produktu nie jest traktowana jak `SOLD_OUT` i zostawia
  poprzedni stan nietknięty. Stan `MISSING` ma być raportowany bez zalewu alarmów;
  27.09.2026 obecnie zwraca tylko osobny produkt niedzielny, więc nie może być
  uznany za awarię całego cyklu.

## 8. Testy i kryteria odbioru

Testy jednostkowe obejmą:

- poprawne parsowanie dat i zakresu;
- rozpoznanie `AVAILABLE` oraz `LOW_AVAILABILITY`;
- ignorowanie dostępnych sugestii i niewłaściwych produktów;
- brak alertu dla `SOLD_OUT` i brak powtórzenia tego samego alertu;
- ponowny alert po sekwencji dostępny → wyprzedany → dostępny;
- zachowanie stanu przy błędzie HTTP, błędnym JSON-ie i braku produktu;
- ponowienie po nieudanym ntfy;
- atomowy zapis i odczyt stanu;
- format komunikatu i link do oficjalnej strony;
- zakończenie monitorowania po upływie zakresu.

Weryfikacja końcowa:

1. Uruchomić pełny zestaw istniejących i nowych testów.
2. Wykonać test na żywym API w trybie bez wysyłania ntfy i potwierdzić obecny
   stan: `SOLD_OUT` dla 24, 25, 26 i 28 września oraz `MISSING` dla niedzieli
   27 września, gdzie zwykły produkt nie jest obecnie wystawiony.
3. Zbudować obraz dodatku, jeśli lokalne środowisko Docker jest dostępne.
4. Sprawdzić spójność wersji, dokumentacji i listy dodatków w repozytorium.
5. Nie wysyłać testowego powiadomienia na prawdziwy temat bez jego jawnej
   konfiguracji; test transportu ma korzystać z atrap.

## 9. Otwarte kwestie, które nie blokują implementacji

- Potwierdzenie, czy rok 2026 jest właściwy (wynika z bieżącej daty 22.09.2026).
- Docelowy temat ntfy; dodatek może odziedziczyć ten sam temat dopiero po wpisaniu
  go w konfiguracji Home Assistant.
- Czy po pierwszym wdrożeniu zejść z domyślnych 60 do 30 sekund. Najpierw warto
  obserwować przez kilka godzin odpowiedzi API i ewentualne 429.
- Czy użytkownik chce również okresowe przypomnienia, jeśli dostępność utrzyma się
  dłużej; wersja pierwsza wysyła alert przy każdym nowym pojawieniu się biletów.
