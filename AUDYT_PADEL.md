# Audyt skutecznosci rezerwacji

Data: 2026-09-08. Kod: `7a19d61`, wersja dodatku `0.28.0`.

Zakres: lokalny monitor, wielokontowa rejestracja, Lambda, zbieracz tokenow,
diagnostyka i potwierdzanie rezerwacji w panelu. Audyt kodu i symulacje offline,
bez rzeczywistych zapisow i bez dostepu do aktualnych logow HA/CloudWatch.
Harmonogram uzytkownika: sprint 11:00:00 / 50 s, burst 11:00:00 / 75 s / 0.2 s.

## Najwazniejsze ustalenia

### P1. Kolejna godzina czeka na najwolniejsze konto i kopie zapytan

`padel_browser/check_padel.py:3184`, `padel_browser/check_padel.py:2189`.

`auto_register_accounts` odbiera wszystkie wyniki rundy przed uruchomieniem
nastepnej. Wewnatrz konta `fire_salvo` rowniez czeka na wszystkie kopie strzalu.
Potwierdzony sukces jednego konta nie zwalnia pozostalych godzin do obslugi.
Lokalne czekanie na nowy token po 401 moze trwac 24 s i zatrzymac cala runde.

Reprodukcja: dwa konta, po dwie kopie zapytania, dwie godziny. Szybkie konto
potwierdzilo pierwsza godzine; nastepna nie ruszyla przez 150 ms kontrolowanego
zatrzymania drugiego konta. Ruszyla dopiero po jego zwolnieniu.

Zmiana: przetwarzac odpowiedzi na biezaco i zwalniac kolejne cele po rozstrzygnieciu
poprzedniego, z ograniczona liczba operacji w toku. Kazde sprawne konto, takze
zwyciezca, nadal uczestniczy w kolejnych celach. Pozne odpowiedzi wymagaja osobnego
rozliczenia; samo `Future.cancel()` nie przerywa wyslanego zadania HTTP.

### P1. Obserwator widzi nowe terminy, ale nie uruchamia ich zapisow

`padel_browser/check_padel.py:3251`, `padel_browser/check_padel.py:3464`.

Obserwator gromadzi nowe terminy w slowniku. Dopiero po zakonczeniu zapisow calej
pierwszej partii kod odczytuje ten slownik i zaczyna druga partie. Wtedy obserwator
jest juz zatrzymany, wiec podczas drugiej partii moze powstac kolejna luka.

Reprodukcja: nowy termin byl znany od 124 ms przed jego strzalem. Zapis ruszyl
dopiero po pierwszej partii, a obserwator byl wtedy zatrzymany.

Zmiana: jeden obserwator aktywny przez cale okno, przekazujacy nowe cele od razu
do wspolnej kolejki priorytetowej. Kolejka deduplikuje ID i pozwala kontom wykonywac
zapisy bez zatrzymywania obserwacji na granicy partii.

### P1. Brak wspolnego budzetu czasu dla klienta, Lambdy i HTTP

`padel_browser/check_padel.py:4059`, `aws_remote/handler.py:183`,
`padel_browser/check_padel.py:687`, `padel_browser/check_padel.py:2298`.

Klient czeka na zdalny wynik przez pozostale okno sprintu + 6 s, czyli na starcie
okolo 56 s. Lambda liczy swoje okno dopiero po rozgrzewce i moze rozpoczac serie
zapisow tuz przed jego koncem. Zapisy nie maja przekazanego wspolnego deadline.
Odpowiedz zawierajaca sukcesy moze dotrzec po timeoucie klienta.

Reprodukcja z zegarem symulowanym: publikacja w sekundzie 49, zakonczenie zapisow
w sekundzie 59. Lambda zwraca sukces po przekroczeniu limitu klienta 56 s.
To scenariusz awarii, a nie pomiar normalnego czasu odpowiedzi serwera.

Dodatkowo `open_url` ustawia timeout tylko przy tworzeniu polaczenia. Ponowne
wykorzystanie gniazda nie zmienia jego timeoutu. Potwierdzono: polaczenie utworzone
z 60 s nie dostaje krotszego limitu przy kolejnym zadaniu zadanym z 5 s.
Zwykly sprint pobiera z domyslnym timeoutem 60 s; zajete watki moga blokowac
kolejne zadania we wspolnej puli takze po zakonczeniu okna obserwacji.

Zmiana: wspolny budzet, rezerwa na dokonczenie operacji i odpowiedz, aktualizacja
timeoutu istniejacego gniazda oraz trwale rozliczanie wyniku po niepewnej odpowiedzi.
`60 s` dla Lambdy jest sensownym zapasem nominalnym przy sprincie 50 s, lecz obecny
kod nie gwarantuje zmieszczenia sie w nim. Samo ustawienie Lambdy na 90 s nie
naprawia limitu klienta ani blokujacych zapisow.

### P1. Token moze wygasnac w sprincie, a Lambda nie otrzyma odnowienia

`padel_browser/check_padel.py:4005`, `padel_browser/check_padel.py:2385`,
`aws_remote/handler.py:113`, `padel_browser/zbieracz.py:71`.

Tokeny sa odczytywane i przesylane raz, przy uruchomieniu zdalnego sprintu.
Token wazny o 11:00, ale wygasajacy o 11:00:20, nie wystarczy do publikacji
o 11:00:40. Lambda nie ma plikow tokenow ani kanalu aktualizacji; po odmowie
autoryzacji konto wypada z dalszych prob danego wywolania.

Zbieracz dodatkowo blokuje wizyty przez ostatnie 90 s przed startem. Token
wygasajacy w tym przedziale nie zostanie wtedy odnowiony. Strona wedlug obecnego
mechanizmu odnawia JWT dopiero po wygasnieciu; samo wczesniejsze wejscie na strone
nie gwarantuje nowego tokenu.

Zmiana: ocena gotowosci na cale okno, identyfikacja kont z ryzykownym `exp`,
przygotowanie ich profili przed startem oraz kontrolowane dostarczenie nowego tokenu
lub przejecie konta lokalnie po odnowieniu. Wymaga koordynacji wynikow obu stron.

### P2. Kontrola gotowosci myli aktualna sesje z prognoza na pol godziny

`padel_browser/check_padel.py:318`, `padel_browser/check_padel.py:372`,
`padel_browser/zbieracz.py:143`.

Kontrola okolo 10:30 weryfikuje w API konto glowne, a dodatkowe liczy po tym,
czy aktualny JWT przetrwa do 11:00. Przy zyciu JWT okolo 15 minut ta prognoza
bedzie negatywna nawet dla sprawnie odnawianych sesji. Konto glowne jest przy
liczeniu zawsze doliczane jako gotowe.

Reprodukcja: 10 aktualnie waznych kont daje 10/10 teraz i 1/10 na 30 minut pozniej.

Zmiana: osobno pokazywac stan sesji i czas waznosci JWT; wykonac finalna kontrole
wszystkich kont blisko startu. Raportowac takze liczbe unikalnych tozsamosci,
poniewaz kontrola stabilnosci `user_id` pojedynczego profilu nie wykrywa zalozenia
kilku profili tej samej osoby.

### P2. Zbieracz moze uruchomic Chromium w srodku polowania

`padel_browser/zbieracz.py:102`, `padel_browser/zbieracz.py:346`.

Cisza obejmuje jedynie czas przed startem i sama chwile startu. Sekunde pozniej
wygasle konto znow kwalifikuje sie do uruchomienia Chromium.
Reprodukcja: konto z wygaslym tokenem pominiete o 10:59:40 zostaje wybrane
o 11:00:01. To dowod decyzji harmonogramu, nie pomiar zuzycia CPU na HA.

Zmiana: skoordynowac okno ochronne z calym burstem i strategia odnowy tokenow.
Samo przedluzenie ciszy bez rozwiazania poprzedniego problemu zmniejszy liczbe
kont zdolnych do zapisu. Wplyw CPU bedzie szczegolnie istotny dla lokalnego zapasu.

### P2. Wspolna lista prob pomija konta z innymi filtrami

`padel_browser/check_padel.py:3142`, `padel_browser/check_padel.py:3207`.

Zbior `attempted` jest wspolny dla kont. Przy roznych kolejkach godzin porazka
jednego konta blokuje pozniejsze podejscie innego, ktore jeszcze nie probowalo.

Reprodukcja: konto A ma tylko 18:00, konto B ma 17:00 i 18:00. Oba pierwsze
zapisy zawodza. Konto B nigdy nie probuje 18:00. Przy jednakowych filtrach i
kolejkach ten konkretny przypadek nie wystepuje.

Zmiana: ewidencja prob `(konto, slot)`, oddzielona od globalnego zbioru sukcesow.

### P2. Diagnostyka zaniza opoznienie i nie pokazuje pelnego wyniku kont

`padel_browser/check_padel.py:3491`, `padel_browser/panel.html:215`,
`padel_browser/panel.py:73`, `padel_browser/panel.py:304`.

Druga partia dostaje `seen_at` z chwili rozpoczecia jej obslugi, zamiast z chwili
wykrycia. W reprodukcji dane mialy 124 ms, a raportowany wiek okolo 1 ms.
Panel strzalow nie wyswietla zapisanego `account_name`. Lista rezerwacji,
anulowanie i kalendarz nadal korzystaja z poswiadczen konta glownego.

Zmiana: znacznik wykrycia per slot, identyfikatory konta/slotu/transakcji przy
wyniku oraz lista rezerwacji ze wszystkich kont. Anulowanie musi uzywac
poswiadczen wlasciciela, a odczyty kont powinny korzystac z bufora.

## Kolejnosc prac

1. Ciagla obserwacja i kolejka celow, eliminacja bariery najwolniejszego konta,
   obsluga spoznionych wynikow i wspolny budzet czasu.
2. Gotowosc tokenow na cale okno i koordynacja zbieracza ze zdalnym wykonaniem.
3. Rzetelne czasy per slot i pelny wynik wszystkich kont w panelu.
4. Numer protokolu/capabilities Lambdy sprawdzany przed polowaniem. Stara paczka
   przyjmie obecny zgodny wstecz payload, ale moze obsluzyc tylko konto glowne.
5. Dopiero po pomiarach porownanie liczby kopii `hedge` i liczby watkow obserwacji.
   Dziesiec kont z hedge=2 daje do 20 zapisow na ten sam slot w rundzie. Wspolna
   kolejka backendu moze skorelowac opoznienia; wiecej zapytan nie gwarantuje zysku.
   Trzeba mierzyc pierwsze potwierdzenie, ogon opoznien i odpowiedzi 429/5xx.

Rozgrzewka przed 11 pozostaje opcjonalna. Przy publikacji kilkanascie sekund po
starcie jej potencjalny wplyw jest mniejszy od opoznien miedzy wykryciem i zapisem.
Osobne wywolanie `warm` potwierdza dostepnosc funkcji, lecz nie gwarantuje
zachowania tego samego kontenera ani rozgrzania wszystkich polaczen do publikacji.

## Weryfikacja i ograniczenia

- 7 symulacji offline potwierdzilo opisane zachowania. Skrypt:
  `/private/tmp/padel_audit_checks.py`. Uruchomienie: `python3 /private/tmp/padel_audit_checks.py`.
  Testy oczekuja obecnego wadliwego zachowania; ich zielony wynik potwierdza
  reprodukcje, nie naprawe aplikacji.
- 6 istniejacych testow wielokontowosci i Lambdy przeszlo. Pokrywaja podstawowe
  jednoczesne proby i izolacje bledow kont, ale nie powyzsze opoznienia i budzety.
- Lokalny `padel_browser/hunts.json` zawiera powtarzane sukcesy z czasem 0 ms
  i nie nadaje sie do wyliczenia produkcyjnej skutecznosci. Nie uzyto go do
  uzasadniania procentowej poprawy ani zmiany harmonogramu.
- Brak aktualnych logow wdrozonego HA/CloudWatch: nie ustalono, ktory problem
  rzeczywiscie wystapil w ostatnim polowaniu, ani o ile poprawki zwieksza skutecznosc.
- Kod produkcyjny, ustawienia, paczka Lambdy i historyczne logi pozostaly bez zmian.
