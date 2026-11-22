# ThermoFlow Unified Intelligence Runtime

`GET /api/thermoflow/v1/intelligence` zwraca `thermoflow-unified-intelligence/1` oraz `Cache-Control: no-store`. `modules` zawiera zawsze `burner_ml`, `fuel_ml`, `weather_impact`, `health_score`, `diagnostic_ai`, `anomaly_v3` i `causal_diagnostics`. To osobny endpoint tylko do odczytu. `/api/thermoflow/v1/shadow`, `/api/fuel/*`, poller zapisujący ThingSpeak i sterownik zachowują dotychczasowe kontrakty.

## Jak interpretować odpowiedź

- **Health Score i Diagnostic AI:** wynik z tego samego świeżego odczytu RAM, który zasila istniejące API shadow. `screens` jest podstawą odwołań dowodowych (`/screens/return` itd.). Po upływie ważności live score i dowody znikają. Raport może pokazać niepełne pokrycie bez liczbowej oceny.
- **Anomaly Detection 3.0 i Causal Diagnostics:** live, wielosygnałowa analiza zsynchronizowanych próbek z bufora RAM. Wykrywa wyłącznie powtarzalne wzorce po minimum trzech sparowanych startach pompy; pokazuje zmianę powrotu, zmianę otwarcia 4D, równoczesne przełączenia innych pomp, ograniczenia danych i poziom pewności. Wynik pozostaje korelacją czasową, nigdy dowodem przyczynowości. Brak przepływomierza i pełnego stanu odbiorników jest jawnie wskazany jako ograniczenie. Luka, duplikat czasu lub nieaktualna telemetria zamyka ocenę zamiast interpolować dane.
- **Burner ML, Fuel ML i Weather Impact:** `evaluated` oznacza metryki historycznego holdoutu, nie predykcję na żywo ani wdrożony model. Benchmark `weather_impact` ocenia wpływ archiwalnej prognozy pogody na błąd modelu na zakończonych okresach; nie jest prognozą na kolejne dni. `collecting` informuje o braku paczki lub kwalifikujących się próbek. Uszkodzony artefakt daje `unavailable`, a starszy niż siedem dni `stale`. API nie zwraca współczynników modelu, zrzutów wejściowych, ID dosypek ani kluczy.
- **Brak predykcji:** `prediction` jest `null`, a `live_predictions_enabled` to `false`. Nie ma zweryfikowanej ścieżki wejściowej online dla tych modeli, nawet jeśli historyczny benchmark zakończył się powodzeniem. Nic nie jest promowane automatycznie.

## Widok w dashboardzie

W zakładce **Diagnostyka → Inteligencja i diagnostyka** są cztery karty bieżącej oceny i trzy karty benchmarków. Bieżąca prognoza zużycia peletu z pogodą ma jedno miejsce prezentacji: **Paliwo i prognoza → Prognoza dobowa peletu · 7 dni**. W Diagnostyce pozostaje wyłącznie historyczny benchmark wpływu pogody; nie renderuje on ponownie prognozy kg/d. Historyczne metryki MAE i RMSE są porównywane z wariantami odniesienia w tej samej jednostce. Gdy raportu brakuje, karta pokazuje przyczynę zamiast wyniku. Health Score to udział ocenionych składowych w zakresie odniesienia, nie miara sprawności ani bezpieczeństwa kotła. Diagnostic AI pokazuje wyłącznie hipotezy i dowody dostępne w bieżącym raporcie.

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


## Anomaly Detection 3.0 i Causal Diagnostics

Shadow Runtime utrzymuje osobny, ograniczony bufor zsynchronizowanych próbek (maksymalnie 241 odczytów, około dwóch godzin przy typowym interwale 30 s). W odróżnieniu od ekranów porównujących jeden sygnał w jednym reżimie, bufor zdarzeń zachowuje historię przełączeń pomp. Zmiana stanu pompy nie kasuje więc sekwencji potrzebnej do sparowania startu z sąsiednimi odczytami temperatury powrotu i otwarcia zaworu 4D.

Anomaly 3.0 ocenia starty pomp podłogowej, CWU, zaworu 4D i cyrkulacyjnej. Dla każdego startu zestawia sąsiednie próbki przed/po zdarzeniu, jeżeli odstęp nie przekracza 75 s. Do wzorca spadku powrotu potrzeba co najmniej trzech sparowanych startów i co najmniej trzech spadków o 1°C lub więcej; mediana zmian również musi wynosić najwyżej -1°C. Zmiana otwarcia zaworu, mierzony czas do pierwszej zmiany jego położenia oraz 15-minutowa zmiana temperatury kotła i spalin są kontekstem dowodowym, a nie automatyczną rekomendacją ruchu zaworu. Trend kotła nie jest samodzielnym dowodem zbyt dużego zapotrzebowania budynku.

Ocena jest niedostępna lub zbierana, gdy historia jest nieciągła, nieaktualna, zduplikowana albo brakuje wiarygodnych stanów. Nie interpoluje luk. Równoczesna zmiana innej pompy obniża pewność wzorca. „Umiarkowana” pewność oznacza jedynie bardziej powtarzalny wzorzec bez wykrytej równoczesnej zmiany innej pompy — nie jest prawdopodobieństwem przyczynowości.

Causal Diagnostics przedstawia hipotezę, obserwacje, liczbę sparowanych zdarzeń, medianę zmiany powrotu, czynniki zakłócające i ograniczenia pomiaru. Nie mierzymy przepływu ani pełnego stanu wszystkich odbiorników, dlatego system nie może stwierdzić, że pompa spowodowała zmianę temperatury.

Analiza jest lokalna w RAM i tylko do odczytu. Nie tworzy nowych zapisów ThingSpeak, nie uruchamia treningu, nie zmienia paliwa ani nastaw. Ochrona powrotu pozostaje na poziomie 55°C ±1°C; próg 54°C pozostaje wyłącznie sygnałem do przeglądu.
