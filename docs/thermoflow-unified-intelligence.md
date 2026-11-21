# ThermoFlow Unified Intelligence Runtime

`GET /api/thermoflow/v1/intelligence` zwraca `thermoflow-unified-intelligence/1` oraz `Cache-Control: no-store`. `modules` zawiera zawsze `burner_ml`, `fuel_ml`, `weather_impact`, `health_score` i `diagnostic_ai`. To osobny endpoint tylko do odczytu. `/api/thermoflow/v1/shadow`, `/api/fuel/*`, poller zapisujący ThingSpeak i sterownik zachowują dotychczasowe kontrakty.

## Jak interpretować odpowiedź

- **Health Score i Diagnostic AI:** wynik z tego samego świeżego odczytu RAM, który zasila istniejące API shadow. `screens` jest podstawą odwołań dowodowych (`/screens/return` itd.). Po upływie ważności live score i dowody znikają. Raport może pokazać niepełne pokrycie bez liczbowej oceny.
- **Burner ML, Fuel ML i Weather Impact:** `evaluated` oznacza metryki historycznego holdoutu, nie predykcję na żywo ani wdrożony model. `collecting` informuje o braku paczki lub kwalifikujących się próbek. Uszkodzony artefakt daje `unavailable`, a starszy niż siedem dni `stale`. API nie zwraca współczynników modelu, zrzutów wejściowych, ID dosypek ani kluczy.
- **Brak predykcji:** `prediction` jest `null`, a `live_predictions_enabled` to `false`. Nie ma zweryfikowanej ścieżki wejściowej online dla tych modeli, nawet jeśli historyczny benchmark zakończył się powodzeniem. Nic nie jest promowane automatycznie.

## Widok w dashboardzie

W zakładce **Diagnostyka → ThermoFlow Unified Intelligence** są dwie karty bieżącej oceny i trzy karty benchmarków. Historyczne metryki MAE i RMSE są porównywane z wariantami odniesienia w tej samej jednostce. Gdy raportu brakuje, karta pokazuje przyczynę zamiast wyniku. Health Score to udział ocenionych składowych w zakresie odniesienia, nie miara sprawności ani bezpieczeństwa kotła. Diagnostic AI pokazuje wyłącznie hipotezy i dowody dostępne w bieżącym raporcie.

Panel odczytuje nowy endpoint przy otwarciu Diagnostyki i po kliknięciu **Odśwież wyniki**. Nie odpytuje backendu cyklicznie i ukrywa wyniki po upływie ich ważności. Dotychczasowa karta „Ocena obserwacyjna” i historia pracy kotła nadal działają niezależnie.

## Opcjonalna paczka benchmarków

Bez konfiguracji działa część live; moduły offline pozostają w stanie `collecting`. Administrator może na komputerze roboczym uruchomić istniejące ewaluatory i przygotować paczkę zagregowanych metryk. Każde wejście jest opcjonalne, ale potrzebne jest przynajmniej jedno:

```sh
python3 -m thermoflow_intelligence_bundle \
  --burner fire-snapshot.json \
  --fuel verified-fuel-periods.json \
  --weather verified-fuel-periods-with-forecasts.json \
  > intelligence.json
```

`--burner` przyjmuje surowy zrzut FIRE. `--fuel` i `--weather` przyjmują zweryfikowane okresy z faktycznie zachowaną prognozą bazową. `--weather` dodatkowo wymaga archiwalnych prognoz pogody na te same okresy. Wyniku z `--assume-equal-fill` ani odtworzonej mediany z #102 nie można opublikować tu jako zweryfikowanego benchmarku. Używaj wyłącznie zaufanych danych z instalacji; aplikacja waliduje format i pochodzenie zadeklarowane w kontrakcie, ale nie poświadcza fizycznego pochodzenia pliku.

Po świadomym włączeniu opcjonalnego odczytu `THERMOFLOW_INTELLIGENCE_BUNDLE` wskazuje wewnątrz kontenera plik (np. `/benchmarks/intelligence.json`). Użyj istniejącego katalogu udostępnionego tylko do odczytu, jeśli jest potrzebny; nie jest wymagany do uruchomienia API. Aktualizuj plik atomową podmianą w zamontowanym katalogu. Ustawiaj zmienną tylko dla zaufanego pliku administratora. Maksymalny rozmiar to 64 KiB, odczyt i walidacja odbywają się w oddzielnym wątku co 60 s, a HTTP czyta wyłącznie RAM. Bez tej zmiennej wątek nie powstaje. Runtime niczego nie zapisuje i nie wymaga SQLite, nowego kanału, kolektora ani zmiany nastaw.

## Weryfikacja

```sh
python3 tools/run_regression_suite.py
```

Runner izoluje pliki, w tym stare skrypty zmieniające środowisko podczas importu. Workflow walidacyjny buduje oba obrazy dla `linux/amd64` i `linux/arm64` bez publikacji i testuje nowy endpoint w uruchomionym kontenerze bez kluczy.
