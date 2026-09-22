# Changelog

## 0.3.0 — wybór biletów i statusów

- Opcje `ticket_types` i `ticket_statuses` pozwalają śledzić kilka rodzajów biletów
  i powiadamiać o uzyskaniu lub utracie wybranych statusów.
- Zmiana wyboru tworzy nowy punkt odniesienia bez fałszywych alertów.

## 0.2.0 — wszystkie zmiany oferty

- Monitorowanie wszystkich produktów ze wszystkich stron wyników, bez filtrowania
  do jednego rodzaju biletu.
- Alerty o dodaniu lub usunięciu oferty oraz zmianie dostępności, komunikatu,
  ceny, zakresu uczestników albo typów odwiedzających.
- Domyślne wyszukiwanie dla jednej osoby jako najszerszy sygnał dostępności;
  liczba odwiedzających jest konfigurowalna w zakresie 1–20.
- Stabilne porównanie ignoruje techniczne identyfikatory i obrazy, które mogą się
  zmieniać bez faktycznej zmiany oferty.

## 0.1.0 — pierwszy monitor Watykan Watch

- Monitor zwykłych biletów do Muzeów Watykańskich dla 5 osób, 24–28.09.2026.
- Odczyt publicznego API bez logowania, rezerwacji, danych osobowych ani zakupu.
- Alerty ntfy przy pierwszej i każdej ponownej dostępności oraz atomowy, trwały stan.
- Ochrona API: odstępy, jitter, Retry-After i wykładnicze wycofanie po 429/5xx.
