# Changelog

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
