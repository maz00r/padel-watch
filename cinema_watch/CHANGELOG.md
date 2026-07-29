# Changelog

## 0.2.0 — filtr typu seansu (IMAX, 4DX…)
- **Nowa opcja `attributes`**: zawężenie do konkretnego typu seansu, np. `imax`.
  Kilka wartości oznacza, że seans musi mieć **wszystkie** naraz.
- **Domyślne ustawienie to teraz `cinemas: Sadyba` + `attributes: imax`** (33 seanse
  zamiast 473 w całej Warszawie). Wyczyszczenie obu opcji przywraca śledzenie wszystkiego.
- Zawężenie **przy jednym atrybucie trafia też do zapytania** (`?attr=imax`), co zmniejsza
  pobierane dane z ~57 KB do ~4,5 KB na dzień. Przy kilku atrybutach filtrujemy wyłącznie
  u siebie — nie wiadomo, czy serwer łączy je przez „i" czy „lub", a nie chcemy zgubić seansu.
  Niezależnie od tego **każdy seans sprawdzamy jeszcze lokalnie**, więc zmiana po stronie
  Cinema City nie rozszerzy po cichu powiadomień.
- Dziennik pokazuje aktywne zawężenie przy starcie, a ostrzeżenie o pustym repertuarze
  wymienia je jako możliwą przyczynę.

## 0.1.0 — pierwszy dodatek monitorujący repertuar Cinema City
- Pilnuje wybranego filmu (link wklejony ze strony) i wysyła push przez ntfy, gdy
  pojawią się **nowe terminy**: nowy dzień w repertuarze (zbiorczo) albo dołożony
  seans w znanym dniu (wypisany pojedynczo).
- Miejsce z linku może być **miastem** (wszystkie kina, np. `warszawa`) albo
  **pojedynczym kinem** (id, np. `1074`); dodatkowo opcja `cinemas` zawęża do
  wybranych kin po fragmencie nazwy.
- Pierwszy bieg zapisuje punkt odniesienia i wysyła jedno powiadomienie startowe —
  bez zalewu alertów o seansach, które już były.
- **Ostrzeżenie przy pustym repertuarze**: zły identyfikator filmu nie zwraca błędu
  HTTP, tylko pustą listę, więc literówka w linku byłaby nie do odróżnienia od
  „nic nowego". Dodatek wypisuje w takim wypadku wyraźne ostrzeżenie.
- Błąd sieci zostawia stan nietknięty, a nieudany push jest ponawiany w kolejnym
  biegu — żaden termin nie ginie po cichu.
