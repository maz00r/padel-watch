# 🎾 Padel (Decathlon) — dodatek Home Assistant

Repozytorium dodatku **Home Assistant**, który monitoruje wolne terminy padla na
[Decathlon GO](https://go.decathlon.pl), wysyła **push na telefon** (przez
[ntfy.sh](https://ntfy.sh)) i (opcjonalnie) **rejestruje automatycznie**. W środku
działa prawdziwa przeglądarka w panelu — **logujesz się raz**, a dodatek sam
podtrzymuje sesję i token.

## Instalacja

1. W Home Assistant: **Ustawienia → Dodatki → Sklep z dodatkami → ⋮ → Repozytoria**.
2. Dodaj: `https://github.com/maz00r/padel-watch` → **Dodaj**.
3. Odśwież sklep i zainstaluj **Padel (Decathlon)**.
4. Skonfiguruj (temat ntfy, godziny) → **Uruchom**.
5. Otwórz **panel** (ikona w menu bocznym) i **zaloguj się** na go.decathlon.pl.

Pełna instrukcja i opis opcji: [padel_browser/README.md](padel_browser/README.md).
Historia zmian: [padel_browser/CHANGELOG.md](padel_browser/CHANGELOG.md).

## Jak to działa

- Co `check_interval` sekund lekki ping (~1 KB) sprawdza licznik dostępności kortu;
  pełne dane (~257 KB) pobierane są tylko, gdy coś jest wolne.
- Terminy filtrowane po Twoich oknach czasowych (`filters`), porównywane ze stanem
  z poprzedniego biegu — alert tylko o **nowych** wolnych terminach (z linkiem do rezerwacji).
- Opcja `intervals` pozwala sprawdzać częściej w wybranych godzinach
  (np. co 30 s wieczorem, co 5 min w nocy).
- App podąża za przekierowaniem strony kortu, więc zmiana adresu/ID po stronie
  Decathlonu nie psuje monitoringu.
- Nieudana wysyłka ntfy jest ponawiana w kolejnej iteracji — alert nie ginie.
- Opcjonalnie app może spróbować automatycznie zarejestrować użytkownika na nowy
  darmowy termin Decathlon GO, używając tokenu z zalogowanej przeglądarki w panelu.
- Token krąży wewnątrz kontenera przez plik `/data/token.json`: przeglądarka
  (`read_token.py`) zapisuje świeży JWT, monitor (`check_padel.py`) go czyta.

## Rozwój

Silnik to jeden plik bez zależności: [padel_browser/check_padel.py](padel_browser/check_padel.py)
(czysty Python, stdlib). Testy: `python3 -m unittest -v test_check_padel` (uruchamiane
też w CI przy każdym PR).
