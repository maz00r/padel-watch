# Zdalny strzał z Irlandii — instrukcja krok po kroku

Sprint i salwa wykonują się w AWS eu-west-1, tuż obok serwera Decathlona.
**W Home Assistancie zostaje wszystko inne**: logowanie, przeglądarka, token, panel
rezerwacji, kalendarz, powiadomienia i stan.

Zmierzone 11.08.2026 (`aws_probe/`, Lambda 1769 MB):

| | dom | eu-west-1 |
|---|---|---|
| runda do serwera | ~42 ms | **0,5 ms** |
| ciężkie pobranie | ~80 ms | **39 ms** |
| **publikacja → nasze żądanie dociera** | **~107 ms** | **~32 ms** |

Trzykrotnie szybciej. Konkurent zabiera wieczorne godziny w ~200 ms, więc te ~75 ms
to spory kawałek jego przewagi.

---

## Zanim zaczniesz

Potrzebujesz konta AWS i ~20 minut. Jeśli jeszcze nie ustawiłeś zabezpieczeń kosztowych
przy okazji [`aws_probe/`](../aws_probe/README.md), zrób to **teraz**, przed resztą:
**Billing → Budgets → Create budget → Zero spend budget**.

Koszt docelowy: ok. 30 wywołań miesięcznie po ~10 sekund przy 1769 MB to ~530 GB-s
z **400 000 darmowych**. Nawet bez darmowego limitu wychodzi kilka groszy miesięcznie.

---

## 1. Pobierz paczkę

Gotowa paczka leży w repozytorium — nie musisz nic budować:

**[⬇ padel-remote.zip](https://github.com/maz00r/padel-watch/raw/main/aws_remote/padel-remote.zip)**

W środku są dwa pliki: `handler.py` i `check_padel.py` — **ten sam silnik, którego używa
dodatek**. To celowe: dwie osobne implementacje filtrów albo limitu rezerwacji
rozjechałyby się przy pierwszej zmianie, a skutkiem byłaby rezerwacja terminu, którego
nie chcesz, albo o jeden za dużo.

Paczka jest przebudowywana przy każdej zmianie silnika, a **CI pilnuje, żeby nie
rozjechała się ze źródłami** — porównuje jej zawartość z `handler.py` i `check_padel.py`
przy każdym PR. Gdyby się rozjechała, wgrałbyś stary silnik i dowiedziałbyś się o tym
dopiero po przegranym terminie.

> Jeśli wolisz zbudować samodzielnie: `./aws_remote/build.sh`.

**Po każdej aktualizacji dodatku** wróć tu, pobierz paczkę ponownie i wgraj
(punkt 3) — Lambda nie aktualizuje się sama.

## 2. Utwórz funkcję

Konsola AWS, **region `eu-west-1` (Ireland)** — sprawdź prawy górny róg.

1. **Lambda → Create function → Author from scratch**
2. Function name: `padel-remote`
3. Runtime: **Python 3.13**, Architecture: **arm64**
4. **Create function**

## 3. Wgraj kod

1. Zakładka **Code** → **Upload from** → **.zip file** → wskaż `padel-remote.zip` → **Save**
2. **Runtime settings → Edit → Handler: `handler.lambda_handler`** → **Save**

   > Domyślnie jest tam `lambda_function.lambda_handler`. Bez tej zmiany dostaniesz
   > `Unable to import module 'lambda_function'`.

## 4. Ustawienia funkcji

**Configuration → General configuration → Edit:**

| ustawienie | wartość | dlaczego |
|---|---|---|
| Memory | **1769 MB** | pamięć w Lambdzie to suwak od **procesora**; pełny rdzeń zaczyna się tutaj. Przy 512 MB samo TLS zajmowało 17 ms zamiast 3 ms. RAM-u zużywa się i tak ~55 MB |
| Timeout | **30 s** | okno sprintu to kilka sekund, reszta to zapas |

**Configuration → Concurrency → Reserve concurrency: `1`**

Funkcja nigdy nie odpali się równolegle. Gdyby ktoś poznał adres, nie rozkręci
kosztów ani równoległych rezerwacji.

## 5. Sekret

Wymyśl długi, losowy ciąg — np.:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Configuration → Environment variables → Edit → Add:**

| klucz | wartość |
|---|---|
| `PADEL_SECRET` | wygenerowany ciąg |

Bez tej zmiennej funkcja **odrzuca wszystkie** żądania (a nie: wpuszcza wszystkie).

## 6. Adres wywołania

**Configuration → Function URL → Create function URL:**

- Auth type: **NONE**
- **Configure cross-origin resource sharing (CORS): zostaw WYŁĄCZONE**

Skopiuj adres — wygląda jak
`https://abc123....lambda-url.eu-west-1.on.aws/`.

> **Dlaczego NONE, a nie IAM.** Autoryzacja IAM wymagałaby trzymania kluczy AWS
> w konfiguracji dodatku, a te — w razie wycieku kopii zapasowej Home Assistanta —
> dają dostęp do konta. Sekret w nagłówku otwiera wyłącznie tę jedną funkcję.
> Adres zawiera losowy 32-znakowy identyfikator, żądania bez poprawnego sekretu
> dostają 403 i nic więcej, a `Reserve concurrency: 1` ogranicza szkodę do zera.
> **Traktuj ten adres jak hasło** — nie wklejaj go publicznie.

## 7. Skonfiguruj dodatek

Home Assistant → **Padel (Decathlon) → Konfiguracja**:

| opcja | wartość |
|---|---|
| `remote_url` | adres Function URL z punktu 6 |
| `remote_secret` | ten sam ciąg co `PADEL_SECRET` |

**Zapisz** → **Uruchom ponownie**. W Dzienniku pojawi się:

```
☁ Zdalny strzał włączony: sprint i salwa lecą z abc123....lambda-url.eu-west-1.on.aws.
  Gdy nie odpowie, poluję lokalnie.
```

Jeśli zamiast tego zobaczysz ostrzeżenie o braku `remote_secret` — dodatek **celowo
wyłączył** zdalny strzał, żeby nie wysyłać Twojego tokenu pod adres bez żadnej bramki.

---

## Jak to wygląda w działaniu

O 11:00:51 zamiast lokalnego sprintu leci jedno żądanie do Irlandii. Dziennik dodatku
pokazuje przepisany dziennik zdalny (linie z `☁`):

```
[11:00:51.9] 🏁 Sprint START — 3 wątków bez przerw do 11:00:56
[11:00:54.6]    ☁ Sprint: nowe terminy po 2244 ms — 3 pasujących do filtra
[11:00:54.6]    ☁ ⇉ Salwa: 3 prób równolegle (wt 18.08 20:00, 19:00, 15:00)
[11:00:54.7]    ☁ ✓ Auto-rejestracja: wt 18.08 20:00 — accepted [24 ms]
[11:00:54.7] ☁ Irlandia: sprint 2244 ms, całość 2290 ms
[11:00:54.8] 📋 Grafik na wt 18.08: 4 wolne z 4
```

Rezerwacja, powiadomienie, kalendarz i stan działają dokładnie jak dotąd.

## Bezpieczeństwo

- **Token nigdzie w AWS nie jest zapisywany.** Przychodzi w treści żądania, żyje
  w pamięci przez jedno wywołanie i znika. Nie ma go w zmiennych środowiskowych,
  w Secrets Managerze ani w zapisanym zdarzeniu testowym. W dzienniku funkcji
  pojawia się wyłącznie jego **długość**.
- **Nie zapisuj zdarzenia testowego z prawdziwym tokenem** w konsoli Lambdy —
  konsola przechowuje je trwale. Do testu użyj samego dodatku.
- Ustaw **retencję logów**: CloudWatch → Log groups → `/aws/lambda/padel-remote` →
  Actions → Edit retention → **3 dni**. Domyślnie logi zostają na zawsze.

## Gdy Irlandia zawiedzie

| sytuacja | co się dzieje |
|---|---|
| zły sekret, zły adres, funkcja wyłączona | odpowiedź wraca w milisekundach → **dodatek poluje lokalnie**, okno sprintu jeszcze trwa |
| **brak odpowiedzi (timeout)** | okna już nie ma, a funkcja **mogła zdążyć zarezerwować**. Dodatek NIE strzela powtórnie i wypisuje ostrzeżenie — **sprawdź panel Padel po polowaniu** |
| Irlandia nic nie znalazła | normalny wynik, dodatek jedzie dalej jak zwykle |

Ryzyko podwójnej rezerwacji jest samo z siebie ograniczone: zajęty termin znika
z listy wolnych, więc kolejny bieg nie ma czego rezerwować. Realne ryzyko to
**nieanulowana nadwyżka ponad `auto_register_max`** — stąd to ostrzeżenie.

## Czego to nie naprawi

- **~38 ms**, które serwer Decathlona potrzebuje na wygenerowanie ciężkiej odpowiedzi.
- Zmienności 31–48 ms po jego stronie.
- Anomalii „pierwszy zapis po publikacji ~300 ms" (294 / 319 / 232 ms przy kolejnych
  57–84 ms). Nie wiemy, czy przeżyje przeprowadzkę — pierwszy log z Irlandii to pokaże.

## Wyłączenie

Wyczyść `remote_url` w konfiguracji dodatku i uruchom ponownie. Wszystko wraca
do polowania lokalnego. Funkcję w AWS możesz zostawić — nieużywana nic nie kosztuje.

## Partie publikacji

Jedno wywołanie obserwuje do końca okna sprintu i rejestruje **każdą partię**, jaka się
pojawi — nie kończy się na pierwszym trafieniu.

Powód jest zmierzony. 01.09 publikacja przyszła dwiema partiami w odstępie ~450 ms.
Stara wersja rejestrowała pierwszą i wracała do domu; zanim dodatek przetworzył wynik
i zawołał Irlandię ponownie, mijało ~620 ms bez jednego spojrzenia. W tym oknie dwie
najlepsze godziny pojawiły się i zniknęły — bez jednego strzału z naszej strony.

Skutki dla działania funkcji:

- wywołanie trwa zwykle **pełne okno sprintu** (`sprint_seconds`), a nie do pierwszego
  trafienia; wyjątkiem jest wyczerpanie limitu rezerwacji, które kończy je od razu,
- `timings.batches` w odpowiedzi mówi, ile partii złapano,
- `max_per_run` obowiązuje **całe wywołanie**, nie pojedynczą partię.
