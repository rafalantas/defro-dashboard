# ThermoFlow: obserwacyjna ocena pracy

`thermoflow_anomaly_v2.screen_series` przyjmuje próbki `(czas UTC w sekundach, wartość)` z tej samej fazy pracy oraz jawne `as_of` i `minimum_deviation` w jednostkach czujnika. Nie używaj mieszaniny różnych faz np. rozpalania i wygaszania jako jednego poziomu odniesienia.

`data_state` opisuje wiarygodność danych, `process_state` wynik oceny. Brak, niejednoznaczność, zestarzenie lub krótka ciągła historia dają `unknown`. Nie łączymy rozdzielonych fragmentów ani nie wykorzystujemy próbek późniejszych niż `as_of`. Przydatna historia pozwala pokazać różnicę wobec wcześniejszej mediany i próg uwzględniający rozrzut MAD oraz minimalną istotną różnicę. `review_deviation` to kandydat do przeglądu, nie potwierdzona usterka. Dotychczasowy scorer i sterownik pozostają bez zmian.

## Health Score

`build_health_score` korzysta z jawnej listy wymaganych składowych i tej samej chwili `as_of` co screening. Pokazuje dla każdej czas, wiek, stan i wartość składowej. Wynik ogólny to odsetek składowych mieszczących się w poziomie odniesienia, a nie prawdopodobieństwo awarii ani certyfikat bezpiecznej pracy. Jeżeli choć jedna oczekiwana składowa nie ma oceny, ogólny wynik pozostaje pusty, a odpowiedź pokazuje pokrycie.

## Diagnostic AI

`diagnostic_hypotheses` zgłasza tylko zgodne czasowo odchylenia, odwołując się do odpowiedniego rekordu w `screens`. Rozróżnia „brak sygnału w ocenionych składowych” od „nie wiadomo”. Zmiana warunków pracy i zmiana pomiaru pozostają alternatywnymi hipotezami. To deterministyczne reguły na danych, bez LLM, ustalania przyczyny, automatycznych komend ani zmian nastaw.

## API i Dashboard

`GET /api/thermoflow/v1/shadow` (`thermoflow-shadow/1`, `Cache-Control: no-store`) czyta gotowy wynik pollera. Nie pobiera danych, nie trenuje modelu i nie udostępnia surowej historii. Możliwe stany to `collecting`, `telemetry_unavailable`, `screened` oraz `stale` (90 s od ostatniej oceny; bez wyniku i hipotez). Widok „Ocena obserwacyjna” pokazuje czas oceny i stan; przy wejściu do zakładki lub kliknięciu przycisku pobiera nowy widok.

Bufor RAM mieści maksymalnie 49 próbek. Zmiana odczytanego FIRE, stanu pomp 4D/podłogówki/CWU lub luka powyżej 75 s zaczynają nowy segment. Są to przybliżenia porównywalnej fazy, nie gwarancja identycznych warunków. Próg obserwacyjny (spaliny 8°C, powrót 3°C) wymaga walidacji i nie jest nastawą kotła. Po restarcie ocena jest budowana na nowo; żadna historia w ThingSpeak nie jest modyfikowana. Wyniki benchmarków ML pozostają offline.
