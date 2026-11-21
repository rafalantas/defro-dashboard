# ThermoFlow: obserwacyjna ocena pracy

`thermoflow_anomaly_v2.screen_series` przyjmuje próbki `(czas UTC w sekundach, wartość)` z tej samej fazy pracy oraz jawne `as_of` i `minimum_deviation` w jednostkach czujnika. Nie używaj mieszaniny różnych faz np. rozpalania i wygaszania jako jednego poziomu odniesienia.

`data_state` opisuje wiarygodność danych, `process_state` wynik oceny. Brak, niejednoznaczność, zestarzenie lub krótka ciągła historia dają `unknown`. Nie łączymy rozdzielonych fragmentów ani nie wykorzystujemy próbek późniejszych niż `as_of`. Przydatna historia pozwala pokazać różnicę wobec wcześniejszej mediany i próg uwzględniający rozrzut MAD oraz minimalną istotną różnicę. `review_deviation` to kandydat do przeglądu, nie potwierdzona usterka. Dotychczasowy scorer i sterownik pozostają bez zmian.

## Health Score

`build_health_score` korzysta z jawnej listy wymaganych składowych i tej samej chwili `as_of` co screening. Pokazuje dla każdej czas, wiek, stan i wartość składowej. Wynik ogólny to odsetek składowych mieszczących się w poziomie odniesienia, a nie prawdopodobieństwo awarii ani certyfikat bezpiecznej pracy. Jeżeli choć jedna oczekiwana składowa nie ma oceny, ogólny wynik pozostaje pusty, a odpowiedź pokazuje pokrycie.

## Diagnostic AI

`diagnostic_hypotheses` zgłasza tylko zgodne czasowo odchylenia, odwołując się do odpowiedniego rekordu w `screens`. Rozróżnia „brak sygnału w ocenionych składowych” od „nie wiadomo”. Zmiana warunków pracy i zmiana pomiaru pozostają alternatywnymi hipotezami. To deterministyczne reguły na danych, bez LLM, ustalania przyczyny, automatycznych komend ani zmian nastaw.
