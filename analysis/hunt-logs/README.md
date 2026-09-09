# Archiwum logów polowań

Surowe logi aplikacji Home Assistant i AWS Lambda są odkładane w `raw/RRRR-MM-DD/`.
Służą do wspólnej analizy po zebraniu kilku dni. Pliki w `raw/` są ignorowane przez
Git, ponieważ zawierają dane osobowe i identyfikatory operacyjne.

Zasady zbierania:

- nie zmieniaj zawartości plików źródłowych;
- zapisuj log aplikacji jako `application.log`;
- zapisuj eksport CloudWatch jako `lambda-cloudwatch.csv`;
- używaj daty polowania, a nie daty pobrania pliku;
- po dodaniu pliku dopisz jego rozmiar i SHA-256 do `MANIFEST.md`.

Analiza wielodniowa powinna łączyć zdarzenia po dacie, godzinie, koncie, terminie
i identyfikatorze slotu. Najważniejsze miary to czas wykrycia, wiek danych przy
strzale, opóźnienie startu, czas odpowiedzi, wynik zapisu, wykonawca AWS/lokalny,
liczba partii oraz błędy HTTP 401, 409, 429 i 5xx.
