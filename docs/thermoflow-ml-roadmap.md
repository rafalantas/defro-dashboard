# ThermoFlow: cykle i ML w trybie obserwacyjnym

## Ciągłe zbieranie FIRE (#93)

Istniejące API nadal zapisuje stan i jasność do osobnego kanału ThingSpeak. Dodatkowy `fire-collector` jest opcjonalnym odbiorcą: używa jedynie `THINGSPEAK_FIRE_CHANNEL_ID` i `THINGSPEAK_FIRE_READ_KEY`, nie dostaje klucza zapisu ani tokenu eModul. Zapisuje lokalną historię w nazwanym wolumenie `fire-archive` i nie wystawia portu. Uruchomienie bez profilu nie zmienia dotychczasowego stosu.

Po publikacji obrazu zawierającego ten PR, w istniejącej konfiguracji stacka z tym samym środowiskiem:

```sh
docker compose --profile ml-collection up -d --no-deps fire-collector
docker compose logs --tail=20 fire-collector
docker compose exec -T fire-collector python -m thermoflow_fire_collector status
```

W Portainerze profil musi być aktywny dla wdrożenia stosu. Jeżeli używany sposób wdrożenia nie przekazuje profili Compose, usuń `profiles: ["ml-collection"]` wyłącznie z sekcji `fire-collector` w konfiguracji tego stosu. Nie kopiuj klucza do polecenia ani do pliku z wynikami. Nie kasuj wolumenu podczas redeployu.

Pierwsza synchronizacja sięga do 24 h wstecz, kolejne uruchamiają się co 5 minut i nadrabiają od ostatniego zatwierdzonego okna, z 15 minutami nakładania. Błąd odczytu nie przesuwa kursora; duplikaty tego samego wpisu ThingSpeak nie dublują rekordów, a różne wpisy o tym samym czasie zostają widoczne do odrzucenia przez segmenter. Pole `valid_fire_samples` mówi, ile jest próbek binarnych; świeży checkpoint sam w sobie nie potwierdza świeżego stanu płomienia. Kolektor nie może odtworzyć próbek niezgromadzonych wcześniej przez ThingSpeak.

Kiedy chcesz sprawdzić postęp, wyeksportuj lokalne archiwum bez sekretów:

```sh
(umask 077; docker compose exec -T fire-collector python -m thermoflow_fire_collector export > fire-snapshot.json)
python3 -m thermoflow_observed_cycles fire-snapshot.json > observed-cycles.json
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
