# ThermoFlow: cykle i ML w trybie obserwacyjnym

## Ciągłe zbieranie i kontrola FIRE (#93)

Dedykowany ThingSpeak pozostaje jedynym trwałym archiwum. Zapis wykonuje istniejący poller API, niezależnie od otwartej karty dashboardu. Nie uruchamiamy dodatkowego kontenera, SQLite, wolumenu ani zapisu na SD. Wymiana kontenera nie kasuje historii z ThingSpeak, ale podczas przerwy w pracy pollera nie powstają nowe próbki.

W obrazie zawierającym #93 dostępne jest polecenie odczytu. Korzysta z `THINGSPEAK_FIRE_CHANNEL_ID` i `THINGSPEAK_FIRE_READ_KEY` już ustawionych w kontenerze przez Portainera. Bez argumentów odczytuje ostatnią dobę. `--status` pokazuje liczbę stanów `0`, `1`, braków i zakres próbek, niczego nie zapisując:

```sh
docker exec defro-dashboard-api python -m thermoflow_fire_snapshot --status
```

Gdy zbierze się więcej historii, administrator może świadomie zapisać eksport na wybranym komputerze. Polecenie czyta maksymalnie 14 dni na wywołanie i wypisuje tylko czasy i stany, bez klucza i ID kanału. Plik w poniższym przykładzie powstaje wyłącznie przez przekierowanie powłoki:

```sh
(umask 077; docker exec defro-dashboard-api python -m thermoflow_fire_snapshot --days 7 > fire-snapshot.json)
```

## Import cykli z pomiarów FIRE (#93)

Narzędzie działa lokalnie. Czyta jeden zrzut JSON, wypisuje raport na standardowe wyjście i nie pobiera próbek z ThingSpeak. W wejściu podaj rzeczywiste, nieprzerzedzone próbki FIRE w UTC, opcjonalnie z temperaturą spalin i mocą. Same spaliny, jasność lub moc nie są etykietą rozpalenia. Nie używaj skompresowanego wykresu ani odpowiedzi, która nie zawiera surowej serii FIRE.

Format wejściowy (przykład wyłącznie formatu, niewystarczający do uczenia):

```json
{
  "schema": "thermoflow-raw-fire/1",
  "sampling": "raw",
  "points": {
    "thermoflow-fire": [[1800000000, 0], [1800000060, 1]],
    "thermoflow-flue": [[1800000060, 80]],
    "thermoflow-boiler-power": [[1800000060, 12]]
  }
}
```

```sh
python3 -m thermoflow_observed_cycles /ścieżka/snapshot.json > observed-cycles.json
```

Raport zawiera skrót pliku wejściowego, odrzucone próbki, kompletne i ucięte cykle oraz chronologiczny dataset. Start i koniec wymagają powtórzonego stanu FIRE. Luka dłuższa niż 300 sekund, konflikt timestampów albo nieprawidłowy stan FIRE o znanym czasie przerywają obserwację. Cecha czasu postoju łączy tylko kompletne cykle w tym samym ciągłym segmencie. Pierwszy cykl segmentu nie ma obserwowanego poprzednika. Podział 20+5 oznacza co najmniej 20 rekordów treningowych i 5 najnowszych rekordów odłożonych; jeśli danych jest mniej, stan to `collecting`.

Przechowuj zrzut i raport poza repozytorium: timestampy mogą ujawniać rytm pracy instalacji. Dane testowe w tym PR są syntetyczne. Import rzeczywistego zrzutu i ocena jego pokrycia pozostają osobnym krokiem odbioru.

## Kolejność dalszych zmian

Numery oznaczają planowaną kolejność. Wszelkie modele pozostają obserwacyjne, bez wpływu na sterownik, kalibrację, istniejące szacunki lub ustawienia.

| PR | Zakres i warunek odbioru |
|---|---|
| #94 Burner ML | Wykorzystać ciągłe segmenty FIRE z #93. Na odłożonym bloku porównać czas cyklu z medianą treningową i długością poprzedniego cyklu; brak danych nie daje predykcji. |
| #95 Fuel ML | Rzeczywiste etykiety z rozliczonych okresów dosypek, podział po czasie i epoce kalibracji. Porównać z aktualnym modelem paliwa, nie zastępować go. |
| #96 Weather Impact | Zestawić pogodę dostępną w chwili predykcji z bazą bez dodatkowych cech; nie wkładać odczytanej później pogody do prognozy. |
| #97 Anomaly Detection 2.0 | Rozróżnić awarie danych i odchylenia pracy; ocenę oznaczyć jako `unknown`, jeśli pokrycie nie wystarcza. |
| #98 Health Score | Pokazywać składowe, wiek danych oraz uzasadnienie wyniku; brak danych nie jest ani dobrą, ani złą oceną pracy. |
| #99 Diagnostic AI | Generować wyłącznie hipotezy poparte obserwacjami. Nie przedstawiać korelacji jako przyczyny i nie wysyłać komend do urządzeń. |
| #100 API i Dashboard | Udostępnić wersjonowany wynik tylko do odczytu, z jasnym statusem zbierania danych. Bez treningu na żądanie HTTP. |
| #101 Shadow Evaluation | Porównać baseline i kandydatów w kolejnych oknach czasu; raportować pokrycie, błąd, fałszywe alarmy i koszt obliczeń. Żadnej automatycznej promocji. |
