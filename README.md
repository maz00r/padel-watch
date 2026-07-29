# 🎾 Padel (Decathlon) — dodatki Home Assistant

Repozytorium zawiera **dwa niezależne dodatki** — możesz zainstalować oba albo tylko jeden:

| Dodatek | Co robi |
|---------|---------|
| 🎾 **[Padel (Decathlon)](padel_browser/README.md)** | monitoruje wolne terminy padla, rejestruje automatycznie, pokazuje i anuluje Twoje rezerwacje |
| 🎬 **[Kino (Cinema City)](cinema_watch/README.md)** | pilnuje repertuaru wybranego filmu i daje znać, gdy pojawią się nowe seanse |

Oba wysyłają push na telefon przez [ntfy.sh](https://ntfy.sh).

---

## 🎾 Padel (Decathlon)

Dodatek **Home Assistant**, który monitoruje wolne terminy padla na
[Decathlon GO](https://go.decathlon.pl), wysyła **push na telefon** (przez
[ntfy.sh](https://ntfy.sh)) i (opcjonalnie) **rejestruje automatycznie**. W środku
działa prawdziwa przeglądarka w panelu — **logujesz się raz**, a dodatek sam
podtrzymuje sesję i token. W panelu obejrzysz też **swoje rezerwacje**: anulujesz je
jednym kliknięciem albo dodasz do kalendarza w telefonie.

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
- Panel (`panel.py`) pokazuje rezerwacje z konta (`users.getMe` + `transactions.list`),
  anuluje je (`transactions.cancel`) i generuje pliki `.ics` dla kalendarza w telefonie.

---

## 🎬 Kino (Cinema City)

Pilnuje repertuaru wybranego filmu i wysyła push, gdy pojawią się **nowe terminy** —
nowy dzień w repertuarze albo seans dołożony w dniu, który już znasz. Lekki dodatek:
czysty Python, bez przeglądarki i bez logowania (repertuar jest publiczny).

Wklejasz link do filmu ze strony Cinema City (z wybranym miastem lub kinem), resztę
dodatek wyciąga sam. Pełna instrukcja: [cinema_watch/README.md](cinema_watch/README.md).

## Rozwój

Każdy dodatek to jeden plik silnika bez zależności (czysty Python, stdlib):
[padel_browser/check_padel.py](padel_browser/check_padel.py) i
[cinema_watch/check_cinema.py](cinema_watch/check_cinema.py).

Testy (uruchamiane też w CI przy każdym PR):

```
python3 -m unittest -v test_check_padel
python3 -m unittest -v test_check_cinema
```
