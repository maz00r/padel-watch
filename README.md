# 🎾 Padel Watch — dodatek Home Assistant

Repozytorium dodatku **Home Assistant**, który monitoruje wolne terminy padla na
[Decathlon GO](https://go.decathlon.pl) i wysyła **push na telefon** (przez
[ntfy.sh](https://ntfy.sh)), gdy pojawi się nowy wolny termin w wybranych godzinach.

## Instalacja

1. W Home Assistant: **Ustawienia → Dodatki → Sklep z dodatkami → ⋮ → Repozytoria**.
2. Dodaj: `https://github.com/maz00r/padel-watch` → **Dodaj**.
3. Odśwież sklep i zainstaluj **Padel Watch (Decathlon)**.
4. Skonfiguruj (temat ntfy, godziny), włącz **Uruchom przy starcie** + **Watchdog** → **Uruchom**.

Pełna instrukcja i opis opcji: [padel_watch/README.md](padel_watch/README.md).
Historia zmian: [padel_watch/CHANGELOG.md](padel_watch/CHANGELOG.md).

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

## Rozwój

Silnik to jeden plik bez zależności: [padel_watch/check_padel.py](padel_watch/check_padel.py)
(czysty Python, stdlib). Testy: `python3 -m unittest -v test_check_padel` (uruchamiane
też w CI przy każdym PR).
