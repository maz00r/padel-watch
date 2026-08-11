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

## Jak czytać wynik

```
Miejsce pomiaru : lambda:eu-west-1
Adresy ELB      : 52.30.168.98, 54.76.133.149
DNS             : 40.3 ms
  TCP 52.30.168.98    : 0.3 ms (min 0.3, max 0.6)
  TCP 54.76.133.149   : 0.8 ms (min 0.7, max 0.8)
TLS             : 17.3 ms (TLSv1.3)
HTTP zimny      : 63.3 ms   (nowe połączenie)
HTTP CIEPŁY     : 65.0 ms   (min 33.1, max 187.9)   <- ta liczba decyduje
  kolejno       : [...] ms
Odpowiedź       : 25744 B na drucie (gzip)
CPU (bez sieci) : ... ms
```

To jest **prawdziwy wynik z 11.08 przy 512 MB**. Sieć zniknęła zgodnie z planem
(0,3 ms zamiast 61 ms), ale ciepłe pobranie zostało na 65 ms. Podejrzenie: rdzeń był
wygłodzony — patrz `CPU (bez sieci)` i uwaga o pamięci wyżej. Dlatego pomiar trzeba
powtórzyć przy 1769 MB, zanim wyciągnie się z niego wniosek.

Każda warstwa osobno, bo tylko tak widać, gdzie siedzi czas. `HTTP CIEPŁY` to dokładnie
koszt jednego zapytania sprintu i jednego strzału salwy.

**Reguła decyzyjna** — porównaj `HTTP CIEPŁY` z `pobranie` z Dziennika (~80 ms):

| wynik | wniosek |
|---|---|
| ≤ 20 ms | przeprowadzka warta zachodu; oszczędza ~120 ms na ścieżce rezerwacji |
| 20–50 ms | zysk realny, ale mniejszy — wtedy VPS w Londynie za kilkanaście zł może wystarczyć |
| > 50 ms | premisa upada, nie ruszamy się nigdzie i szukamy gdzie indziej |

Osobno warto zerknąć na `TCP` obu adresów ELB. Jeśli różnią się wyraźnie, to znaczy,
że DNS podaje je na zmianę i część naszej dziennej zmienności bierze się stąd,
a nie z obciążenia serwera.

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
