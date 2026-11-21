# ThermoFlow Shadow Evaluation

Uruchom na komputerze roboczym:

```sh
python3 -m thermoflow_shadow_evaluation shadow-ledger.json > shadow-report.json
```

Jeden plik dotyczy jednego zadania i jednej jednostki (np. `burner-duration-minutes`, `fuel-kg-per-day` lub `verified-process-alert-per-episode`). Przykład kontraktu, nie pomiar:

```json
{
  "schema": "thermoflow-shadow-ledger/1",
  "task": "fuel-kg-per-day",
  "kind": "regression",
  "start": 1800000000,
  "end": 1800604800,
  "as_of": 1800700000,
  "window_seconds": 86400,
  "cases": [{
    "id": "period-001",
    "decision_at": 1800000000,
    "outcome_end": 1800086400,
    "label": {"verified": true, "available_at": 1800086460, "value": 10},
    "baseline": {"issued_at": 1799999995, "data_cutoff": 1799999980, "value": 11, "duration_ms": 5},
    "candidate": {"issued_at": 1800000000, "data_cutoff": 1799999980, "value": 9, "duration_ms": 8}
  }]
}
```

- Czasy to sekundy UTC. Okna są rozłączne, przypisanie po `decision_at`. `outcome_end` jest końcem okresu oceny; etykieta może być zweryfikowana po tej chwili, najpóźniej w `as_of`. `data_cutoff` obejmuje dane i etykiety użyte do uczenia oraz dane użyte do samej prognozy. Rzeczywiste czasy i pochodzenie muszą pochodzić z archiwum: walidacja pól nie jest dowodem przeciw wyciekowi z przeliczeń po fakcie. Brak prognozy lub etykiety reprezentuj przez `null`, nie `0` ani `false`.
- Dla `alert` używaj booleanów. Niezależnie weryfikuj epizody dodatnie i ujemne: nieoznaczona nieprawidłowość nie jest ujemną etykietą. Dla regresji używaj liczby w jednej jednostce. Raport zawiera pokrycie każdego wariantu, wynik na wspólnych weryfikowanych przypadkach i puste okna. Fałszywe alarmy mają mianownik z weryfikowanych przypadków ujemnych; brak mianownika daje `null`.
- `duration_ms` jest opcjonalnym pomiarem czasu obliczeń wariantu, wykonanym porównywalnym zegarem i w porównywalnym środowisku. Średnia i p95 dotyczą tylko rekordów z podanym pomiarem, a nie czasu odpowiedzi dashboardu. Brak pomiaru daje `null`.
- Raport nie uczy ani nie promuje modelu. Ten PR dostarcza evaluator i kontrakt, nie tworzy archiwum wyników ML ani etykiet. Nie wymaga SQLite, dodatkowego pollera ani zapisu na Raspberry Pi. Trzymaj wejścia poza repozytorium. Testy syntetyczne weryfikują mechanikę, nie jakość na instalacji.
