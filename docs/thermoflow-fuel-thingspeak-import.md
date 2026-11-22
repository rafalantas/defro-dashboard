# Fuel ML: import istniejących dosypek z ThingSpeak

Narzędzie jest uruchamiane ręcznie. W kontenerze API zawierającym tę zmianę korzysta z obecnych zmiennych Portainera. Pobiera z istniejącego kanału wpisy dosypek, stosuje anulowania i ten sam próg scalania bliskich wpisów co produkcyjny moduł paliwa. Odczytuje procent sterownika w oknie ±10 minut wokół dosypki dla kontekstu. Nie używa kluczy zapisu i nie modyfikuje danych, cache API, kalibracji, konfiguracji ani harmonogramu pollera.

## Najpierw sprawdź pokrycie

```sh
docker exec defro-dashboard-api python -m thermoflow_fuel_snapshot --days 120 --status
```

`--status` wypisuje liczniki, stan benchmarku i powody odrzucenia. Polecenie czyta maksymalnie 80 osobnych okien licznika plus kanał dosypek. Przy zbyt dużym zakresie zawęź `--days` lub dodaj `--without-counter`, które pomija kontekst procentów i nie zmienia kwalifikacji okresów. Pobranie całego dostępnego rejestru dosypek (maksymalnie 8000 rekordów; pełny limit to błąd) pozwala uwzględnić anulowania zapisane później niż anulowana dosypka.

## Wybierz poziom pewności etykiet

- Domyślnie brak potwierdzenia oznacza `unverified`. Procent sterownika mierzy jego licznik podajnika, nie rzeczywisty stan zasobnika. Skok do 100% nie potwierdza pełnej dosypki i nie ustala dokładnego czasu fizycznej operacji.
- Jeśli zasobnik rzeczywiście zawsze był uzupełniany do tego samego poziomu, włącz `--assume-equal-fill`. Narzędzie wykorzysta istniejące kilogramy bez ręcznego przepisywania. Wynik ma etykietę `assumed_equal_fill_level`: to szacunek przy założeniu, nie pomiar. Nie używaj tego trybu, jeśli historia obejmuje częściowe lub niezarejestrowane dosypki. W razie wątpliwości wybierz tryb zweryfikowany.
- Tryb zweryfikowany przyjmuje listę identyfikatorów wpisów w `--metadata`. Po scaleniu bliskich wpisów należy potwierdzić wszystkie identyfikatory w grupie. Potwierdzenie oznacza, że po całej operacji uzyskano wspólny poziom odniesienia i między potwierdzonymi końcami nie ma niezarejestrowanej zmiany zapasu. Nie przepisuje się mas. Niepotwierdzony wpis pozostaje granicą i nie jest przeskakiwany.

```sh
docker exec defro-dashboard-api python -m thermoflow_fuel_snapshot --days 120 --assume-equal-fill --status
docker exec defro-dashboard-api python -m thermoflow_fuel_snapshot --days 120 --assume-equal-fill --evaluate
```

## Co porównuje benchmark

Jeśli nie ma rzeczywiście zachowanych prognoz, używa odtwarzanej mediany wcześniejszych zamkniętych okresów (`causal_replay_median`). Uwzględnia tylko tę samą epokę i nie używa masy z końca badanego okresu. To porównanie retrospektywne według czasu dosypki, a **nie** odtworzenie produkcyjnej prognozy ani dowód, że wpis (zwłaszcza wpis wsteczny) był już dostępny w rejestrze w tym dniu. Nie miesza porównania retrospektywnego z prognozami zachowanymi w chwili wydania. Benchmark stosuje aktualną `FUEL_COUNTER_EPOCH`; odrzuca okres przecinający zmianę i ocenia najnowszą epokę.

Format `--metadata` (przykład wyłącznie formatu):

```json
{
  "schema": "thermoflow-fuel-import-metadata/1",
  "verified_refill_ids": [14, 15, 17],
  "archived_baselines": [{
    "start": "2026-11-21T09:00:00Z",
    "baseline_issued_at": "2026-11-21T08:50:00Z",
    "baseline_kg_per_day": 11.4
  }]
}
```

Usuń klucz `archived_baselines`, aby użyć mediany retrospektywnej. Gdy klucz występuje, nie uzupełniamy braków medianą.

### Archiwum pogody z chwili decyzji

Ten sam plik może zawierać `archived_forecasts`. Są one dołączane do zrzutu, dzięki czemu `thermoflow_weather_impact` może ocenić dokładnie te prognozy, które były dostępne przed początkiem okresu. Każdy wpis musi wskazywać dokładne granice okresu dosypka–dosypka, mieć `kind: "forecast"` oraz strefowe znaczniki `issued_at` i `available_at` spełniające warunek `issued_at <= available_at <= start`. Wartość `forecast_mean_outdoor_c` musi być skończoną temperaturą w zakresie -90..65°C. Duplikaty, obserwacje, późne prognozy, złe okna i błędne wartości są odrzucane.

Przykład formatu (wartości demonstracyjne, nie dane z instalacji):

```json
{
  "schema": "thermoflow-fuel-import-metadata/1",
  "verified_refill_ids": [14, 15, 17],
  "archived_baselines": [{
    "start": "2026-11-21T09:00:00Z",
    "baseline_issued_at": "2026-11-21T08:50:00Z",
    "baseline_kg_per_day": 11.4
  }],
  "archived_forecasts": [{
    "start": "2026-11-21T09:00:00Z",
    "end": "2026-11-28T09:00:00Z",
    "issued_at": "2026-11-21T08:40:00Z",
    "available_at": "2026-11-21T08:42:00Z",
    "kind": "forecast",
    "forecast_mean_outdoor_c": 2.5
  }]
}
```

`archived_forecasts` jest opcjonalne. Bez niego zrzut zawiera pustą listę prognoz i Weather Impact pozostaje `collecting`; nie pobieramy pogody wstecz ani nie zastępujemy prognozy obserwacją.

Po zbudowaniu zrzutu uruchom:

```sh
docker exec -i defro-dashboard-api python -m thermoflow_fuel_snapshot --metadata /dev/stdin --evaluate < fuel-metadata.json > verified-fuel-periods.json
python3 -m thermoflow_weather_impact verified-fuel-periods.json
```

Wpisy `archived_forecasts` muszą być ręcznie zarchiwizowanymi prognozami z rzeczywistymi czasami wydania i dostępności. Nie wpisuj dzisiejszych wartości ani czasu importu jako historycznych znaczników. `start` identyfikuje początek okresu (końcowy czas wpisu po scaleniu); prognoza musi być zachowana w tej chwili lub wcześniej. Nie wyliczaj jej dzisiejszym modelem wstecz. Jeśli metadata jest potrzebna, uruchom narzędzie w środowisku z dostępem do tego pliku, np. `docker exec -i defro-dashboard-api python -m thermoflow_fuel_snapshot --metadata /dev/stdin --status < fuel-metadata.json`. Plik zostaje na komputerze roboczym; wewnątrz kontenera nie powstaje kopia.

Bez dodatkowych opcji narzędzie wypisuje zrzut kompatybilny z `python -m thermoflow_fuel_ml`. Zrzut zawiera czasy operacji i identyfikatory wpisów, ale nie notatki i nie klucze. Przechowuj go poza repozytorium. `collecting` nie oznacza awarii odczytu, tylko brak wystarczającej liczby kwalifikujących się okresów. Testy syntetyczne nie stanowią pomiaru jakości na instalacji.

Osobne benchmarki, które wymagają prawdziwej prognozy i zweryfikowanych etykiet (np. porównanie wpływu archiwalnej prognozy pogody), nie przyjmują domyślnie trybu szacunkowego ani retrospektywnej mediany.
