# Plan wdrożenia: Watykan Watch

## 1. Cel i zakres

- Osobny dodatek Home Assistant w repozytorium Padel/Kino.
- Monitorowany termin: 24–28 września 2026 włącznie, według czasu rzymskiego.
- Monitorowane są wszystkie oferty zwracane przez oficjalny system dla wybranej
  liczby odwiedzających — bez ograniczenia do jednego produktu ani dostępności
  dla pięciu osób.
- Domyślne `visitors=1` daje możliwie wczesny sygnał o choć jednym miejscu;
  wartość pozostaje konfigurowalna w zakresie 1–20.
- Zakup jest ręczny. Dodatek nie loguje się, nie rezerwuje, nie kupuje i nie
  przechowuje danych osobowych ani płatniczych.
- Powiadomienia korzystają z tego samego transportu ntfy.sh co aplikacja Padel.

## 2. Punkt integracji

Oficjalna strona korzysta z publicznego zapytania tylko do odczytu:

`GET https://tickets.museivaticani.va/api/search/result`

Parametry monitora:

- `lang=it`;
- `visitorNum=<1–20>`;
- `visitDate=DD/MM/YYYY`;
- `area=1`;
- `who=`;
- `page=0`, `page=1`, … aż do pobrania `totalResults`.

Weryfikacja na żywo wykazała, że pojedyncza data może mieć 23 wyniki, podczas
gdy pierwsza strona zwraca tylko 10. Pełna paginacja jest więc warunkiem
wykrywania wszystkich zmian.

## 3. Architektura

Katalog `vatican_watch/` zawiera:

- `check_vatican.py` — monitor w Pythonie bez zewnętrznych bibliotek;
- `run.sh` — mapowanie opcji Home Assistant na zmienne środowiskowe;
- `config.yaml` — metadane, wartości domyślne i walidacja;
- `Dockerfile` — obraz dodatku;
- `README.md` — instalacja, konfiguracja i semantyka alertów;
- `CHANGELOG.md` — historia wersji.

Testy jednostkowe znajdują się w `test_check_vatican.py`, a CI uruchamia je razem
z pełnym zestawem testów repozytorium.

## 4. Algorytm sprawdzania

1. Zweryfikować zakres dat, temat ntfy, interwał, liczbę osób i strefę czasową.
2. Dla każdego aktywnego dnia pobrać wszystkie strony wyników. Niepełna lista,
   błędny JSON lub niespójne `totalResults` oznaczają błąd całego cyklu.
3. Zbudować stabilny obraz każdej oferty: nazwa, opis, sugestia, status,
   komunikat, cena, zakres uczestników i typy odwiedzających.
4. Pominąć pola techniczne, takie jak zmienny identyfikator i obraz, aby nie
   alarmować o różnicy niewidocznej dla użytkownika.
5. W pierwszym poprawnym cyklu zapisać punkt odniesienia bez alertu.
6. W kolejnych cyklach wykrywać dodanie, usunięcie oraz zmianę każdego
   semantycznego pola. Zmiana nazwy/opisu/sugestii pojawia się jako usunięcie
   starej i dodanie nowej wersji.
7. Wysłać jeden zbiorczy alert ntfy. Jeśli nowa lub zmieniona oferta przechodzi
   do `AVAILABLE` albo `LOW_AVAILABILITY`, nadać wysoki priorytet.
8. Dopiero po skutecznym powiadomieniu atomowo zapisać nowy obraz. Nieudany push
   pozostawia poprzedni stan i powoduje ponowienie alertu.
9. Usuwać minione dni z aktywnego zakresu, a po 28.09.2026 zakończyć ruch.

## 5. Częstotliwość i ochrona serwisu

- Domyślny pełny cykl co 60 sekund; konfiguracja dopuszcza 30–3600 sekund.
- Krótkie odstępy między stronami i datami oraz mały losowy jitter między cyklami.
- Umiarkowany timeout, gzip i jednoznaczny User-Agent.
- Dla 429 i 5xx: respektowanie `Retry-After` oraz wykładnicze wycofanie do godziny.
- Brak omijania CAPTCHA, blokad albo innych zabezpieczeń. Utrata dostępu do
  publicznego API ma wywołać alarm techniczny, nie próbę obejścia ochrony.

## 6. Powiadomienia i stan

- Start: zakres dat, liczba osób i interwał.
- Zmiana: data, nazwa oferty oraz konkretna różnica; kliknięcie otwiera oficjalne
  wyniki dla najwcześniejszego dnia objętego zmianą.
- Błąd: jeden alarm po trzech kolejnych nieudanych cyklach oraz jeden komunikat
  po odzyskaniu działania.
- Brak zmian: cisza w ntfy i krótki wpis w Dzienniku.
- Stan: `/data/vatican_state.json`, zapis atomowy i migracja ze starego formatu
  przez utworzenie nowego punktu odniesienia.
- Zmiana `visitors` zeruje tylko obraz ofert i tworzy nowy punkt odniesienia,
  dzięki czemu różne katalogi wyników nie generują fałszywej lawiny alertów.

## 7. Testy i kryteria odbioru

Testy jednostkowe obejmują:

- konfigurację, zakres dat i liczbę osób;
- kanonizację wszystkich rodzajów produktów i ignorowanie technicznych ID;
- pełną paginację oraz odrzucenie niepełnej odpowiedzi;
- dodanie, usunięcie, zmianę statusu, komunikatu i ceny;
- priorytet alertu oraz poprawny oficjalny link;
- pierwszy punkt odniesienia, zmianę `visitors` i brak fałszywych alarmów;
- ponowienie po błędzie ntfy i zachowanie stanu po błędzie API;
- atomowy zapis, alarm awarii, odzyskanie oraz zakończenie zakresu.

Weryfikacja końcowa:

1. Sprawdzenie składni Pythona i skryptu startowego.
2. Uruchomienie testów Watykan Watch oraz całego zestawu repozytorium.
3. Odczyt wszystkich dat z żywego API bez prawdziwego powiadomienia ntfy i
   potwierdzenie pełnej liczby wyników.
4. Kontrola diffu, spójności wersji `0.2.0` i dokumentacji.
5. Commit, push oraz obserwacja wyniku GitHub Actions.

## 8. Pozostały krok wdrożeniowy

Po aktualizacji repozytorium użytkownik odświeża dodatek w Home Assistant,
wpisuje ten sam `ntfy_topic` co w Padel i uruchamia wersję `0.2.0`. Pierwszy cykl
zapisze punkt odniesienia; od następnego poprawnego cyklu każda istotna różnica
dla wybranych dat będzie zgłaszana.
