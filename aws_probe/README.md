# Pomiar: ile naprawdę kupuje przeprowadzka do Irlandii

`go.decathlon.pl` to load balancer w AWS **eu-west-1**:

```
go.decathlon.pl → sporteo-01-2125210702.eu-west-1.elb.amazonaws.com
```

Rezerwacja potrzebuje **dwóch rund** do tego serwera, więc opóźnienie liczy się podwójnie.
Ten pomiar odpowiada na jedno pytanie: **ile z naszych ~80 ms na pobranie i ~70 ms na
strzał to sieć, którą przeprowadzka usunie** — zanim wydamy na nią czas i pieniądze.

To nie jest część dodatku. Home Assistant ignoruje ten katalog (nie ma `config.yaml`).

## Po co, skoro już to policzyliśmy

Bo w naszych liczbach jest sprzeczność. Dodatek raportuje rundę do serwera **61 ms**,
a w tej samej sekundzie uwierzytelniony POST na **podtrzymanym** połączeniu zajmuje
**48 ms**. Zapytanie na ciepłym gnieździe nie może być szybsze niż jedna runda —
któraś z tych liczb kłamie.

Najbardziej prawdopodobne wyjaśnienie: `log_rtt` mierzy raz, przy starcie procesu,
czyli często wiele godzin przed 11:00, a trasa bywa wtedy inna. Jeśli tak, to realne
RTT o 11:00 jest niższe niż 61 ms — a wtedy **zysk z przeprowadzki jest mniejszy,
niż szacowałem**. Trzeba to wiedzieć przed decyzją, nie po.

## Z czym porównujemy

Strony „dom" nie trzeba osobno mierzyć — masz ją w Dzienniku każdego dnia:

| liczba z Dziennika | co to jest |
|---|---|
| `(pobranie 80 ms)` | pobranie po podtrzymanym połączeniu = **HTTP CIEPŁY** |
| `📡 Runda do go.decathlon.pl: 61 ms` | TCP (mierzone przy starcie, patrz wyżej) |
| `✓ Auto-rejestracja … [70 ms]` | jeden strzał = jedna runda + czas serwera |

Pomiar w Lambdzie wypisuje te same wielkości. Porównujesz `HTTP CIEPŁY` z `pobranie`.

## Uruchomienie w Lambdzie (konsola, ~10 minut)

**Region musi być `eu-west-1` (Irlandia).** To jedyna rzecz, której nie da się później
poprawić bez zaczynania od nowa — sprawdź w prawym górnym rogu konsoli, zanim klikniesz
cokolwiek. Pomiar w innym regionie nie odpowie na żadne z naszych pytań.

1. Konsola AWS → **Lambda** → **Create function**
2. **Author from scratch**, nazwa np. `padel-probe`, runtime **Python 3.13**, architektura **arm64** (tańsza, tu bez różnicy)
3. **Create function**
4. W edytorze kodu otwórz `lambda_function.py`, skasuj zawartość i wklej **całą** treść [`probe.py`](probe.py)
5. **Deploy**
6. Zakładka **Configuration → General configuration → Edit**: **Timeout 30 s**, **Memory 1769 MB**

   > **Pamięć w Lambdzie to w praktyce suwak od PROCESORA**, nie od RAM-u. Pełny rdzeń
   > dostajesz dopiero przy **1769 MB**. Przy 512 MB masz jego ułamek, a wtedy
   > deszyfrowanie TLS i przeczytanie 25 KB odpowiedzi kosztuje dziesiątki milisekund
   > i w pomiarze wygląda jak wolny serwer Decathlonu. Sprawdzone: przy 512 MB samo
   > uzgodnienie TLS zajęło **17 ms mimo rundy 0,3 ms** — to był czysty procesor.
   > Zużycie RAM-u to i tak tylko ~53 MB, więc nie podnosisz pamięci dla pamięci.
7. **Test** → nazwa dowolna, treść zdarzenia zostaw `{}` → **Test**

Wynik zobaczysz w oknie wykonania i w CloudWatch Logs.

> Pomiar wysyła **wyłącznie GET-y bez uwierzytelnienia**. Celowo nie przyjmuje tokenu:
> zapisany event testowy w konsoli przechowywałby żywą sesję Decathlona, a tego
> nie chcemy. Nic nie rezerwuje i nie dotyka konta.

## Zanim odejdziesz od konsoli — cztery bezpieczniki

Przy nowym koncie to ważniejsze niż sam pomiar:

1. **Budget alert na 1 USD** — Billing and Cost Management → Budgets → Create budget → Zero spend / Monthly cost. Robi się raz i chroni przed każdą późniejszą pomyłką.
2. **Retencja logów 3 dni** — CloudWatch → Log groups → `/aws/lambda/padel-probe` → Actions → Edit retention. Domyślnie logi zostają **na zawsze** i to jedyna pozycja, która potrafi po cichu rosnąć.
3. **Reserved concurrency = 1** (Configuration → Concurrency) — funkcja nigdy nie odpali się równolegle.
4. **Nie dodawaj Function URL** do tej funkcji. Pomiar odpalasz przyciskiem Test; adres publiczny jest tu niepotrzebny.

## Wynik pomiaru (11.08.2026) — ROZSTRZYGNIĘTY

Przy 512 MB, czyli ułamku rdzenia, wynik był zafałszowany. Przy **1769 MB**:

| warstwa | dom | eu-west-1 (1769 MB) |
|---|---|---|
| TCP (jedna runda) | „61 ms" | **0,5 ms** |
| TLS | — | 3,2 ms *(przy 512 MB: 17,3 ms — czysty procesor)* |
| HTTP ciepły | ~80 ms | **38,8 ms** (31–48) |

Ciepłe pomiary kolejno: `[32.5, 43.3, 34.3, 44.0, 31.2, 47.5]` — bez trendu malejącego,
więc to zmienność serwera, nie rozgrzewka.

### Sprzeczność 61 ms vs 48 ms — wyjaśniona

Pomiar daje **czas serwera bez udziału sieci: ~38 ms** na ciężką odpowiedź. Podstawmy
to do obserwacji z domu:

| obserwacja z Dziennika | model `RTT + serwer` | pasuje? |
|---|---|---|
| ciężkie pobranie **80 ms** | 42 + 38 | ✓ |
| `users.getMe` **48 ms** (lekka odpowiedź) | 42 + 6 | ✓ |

Obie liczby wychodzą przy **RTT ≈ 42 ms**, a żadna nie wychodzi przy 61 ms. Czyli
**to `log_rtt` w dodatku podaje wartość zawyżoną** — mierzy raz, przy starcie procesu,
często wiele godzin przed polowaniem. 42 ms to zresztą wartość znacznie bardziej
wiarygodna dla trasy Warszawa–Dublin (~1800 km).

### Ile realnie kupuje przeprowadzka

Licząc od „termin pojawia się na serwerze" do „nasze żądanie rezerwacji tam dociera":

| | dom | eu-west-1 |
|---|---|---|
| wykrycie (sprint, 3 wątki bez przerw) | ~65 ms | ~32 ms |
| dolot strzału | ~42 ms | ~0,3 ms |
| **razem** | **~107 ms** | **~32 ms** |

**Około 75 ms, czyli trzykrotnie szybciej do celu.** Przy konkurencie zabierającym
wieczorne godziny w ~200 ms to jest różnica, która może przeważyć.

Czego przeprowadzka NIE zmieni: ~38 ms, które serwer Decathlonu potrzebuje na
wygenerowanie ciężkiej odpowiedzi, oraz zmienności 31–48 ms.

## Czego ten pomiar NIE rozstrzygnie

Anomalii „pierwszy zapis po publikacji kosztuje ~300 ms" (8.08: 294, 9.08: 319,
10.08: 232, przy kolejnych 57–84 ms). Ona dotyczy **pierwszego zapisu do świeżo
opublikowanego grafiku**, więc odtworzyć ją można wyłącznie o 11:00:53 na żywej
publikacji. Jeśli jest po stronie Decathlonu, przeprowadzka jej nie usunie —
a to jest połowa tego, co dziś tracimy. Odpowiedź przyniesie dopiero właściwa
integracja, nie ten pomiar.

## Lokalnie (opcjonalnie)

Ten sam plik działa jako zwykły skrypt, jeśli chcesz porównać z jakiejkolwiek innej
maszyny — ale liczby z laptopa po WiFi nie są porównywalne z serwerem po kablu:

```bash
python3 aws_probe/probe.py
```
