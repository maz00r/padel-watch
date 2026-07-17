# Padel Browser (PoC)

**To jest proof-of-concept, nie gotowa funkcja.** Ma odpowiedzieć na **jedno pytanie**:

> Czy po jednorazowym zalogowaniu na serwerze (i przejściu weryfikacji mailowej) sesja
> Decathlon GO **utrzyma się**, tzn. czy strona sama odnowi JWT przy kolejnych wczytaniach
> z tego urządzenia?

Jeśli **tak** → auto-rezerwacja może działać bezobsługowo i dopinamy to do `padel_watch`.
Jeśli **nie** → wyrzucamy ten katalog i zostaje wklejanie tokenu przed oknem polowania.

## Dlaczego przeglądarka, a nie kod

Serwerowe odtwarzanie logowania SSO **nie działa**: Decathlon traktuje je jako logowanie
z nowego urządzenia i wysyła kod weryfikacyjny na e-mail. To celowa kontrola
bezpieczeństwa i nie należy jej obchodzić.

Prawdziwa przeglądarka to co innego: **Ty logujesz się ręcznie i sam wpisujesz kod z maila** —
dokładnie tak, jak przy każdym nowym urządzeniu. Nic nie jest obchodzone.

## Jak przetestować

1. Zainstaluj dodatek → **Uruchom**
2. Otwórz zakładkę **panelu** (ikona w menu bocznym HA) — zobaczysz Chromium przez noVNC
3. **Zaloguj się** na `go.decathlon.pl`. Przyjdzie kod na maila — **wpisz go**
4. Obserwuj **Dziennik**:

```
✓ JWT odczytany, ważny do 2026-07-17 15:32:55 (jeszcze ~14 min). Długość: 812 zn.
```

5. **To jest właściwy test:** zostaw dodatek na 30–60 min. Token żyje ~15 min, więc
   przy kolejnych odczytach zobaczysz, czy strona odnawia go sama:

| Co widzisz po godzinie | Wniosek |
|---|---|
| `✓ JWT odczytany, ważny do …` z **przesuwającą się** datą | 🎉 sesja się utrzymuje — droga otwarta |
| `✗ strona przekierowała na logowanie` | sesja nie przeżyła — PoC do kosza |
| `✗ brak go-sdk-jwt` (a byłeś zalogowany) | coś innego — podeślij log |

## Opcje

| Opcja | Znaczenie | Domyślnie |
|---|---|---|
| `start_url` | strona wczytywana przy odczycie | `https://go.decathlon.pl` |
| `read_interval` | co ile sekund odczytywać token (60–3600) | `600` |

## Co robi, a czego nie

- ✅ Trzyma profil Chromium w `/data` → logowanie przeżywa restart dodatku
- ✅ Czyta `localStorage['go-sdk-jwt']` przez CDP i raportuje ważność
- ❌ **Niczego nie rezerwuje** — to tylko diagnostyka
- ❌ Nie jest jeszcze spięty z `padel_watch`

## Uwagi

- Obraz waży ~760 MB (Chromium). Na mini PC bez znaczenia; na Raspberry Pi odradzam.
- VNC nasłuchuje tylko lokalnie (`-localhost`); na zewnątrz wychodzi wyłącznie noVNC
  przez Ingress, czyli za uwierzytelnieniem Home Assistanta.
- `boot: manual` — PoC nie wstaje sam po restarcie HA. To celowe.
