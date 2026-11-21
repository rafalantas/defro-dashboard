# ThermoFlow: obserwacyjna ocena pracy

`thermoflow_anomaly_v2.screen_series` przyjmuje próbki `(czas UTC w sekundach, wartość)` z tej samej fazy pracy oraz jawne `as_of` i `minimum_deviation` w jednostkach czujnika. Nie używaj mieszaniny różnych faz np. rozpalania i wygaszania jako jednego poziomu odniesienia.

`data_state` opisuje wiarygodność danych, `process_state` wynik oceny. Brak, niejednoznaczność, zestarzenie lub krótka ciągła historia dają `unknown`. Nie łączymy rozdzielonych fragmentów ani nie wykorzystujemy próbek późniejszych niż `as_of`. Przydatna historia pozwala pokazać różnicę wobec wcześniejszej mediany i próg uwzględniający rozrzut MAD oraz minimalną istotną różnicę. `review_deviation` to kandydat do przeglądu, nie potwierdzona usterka. Dotychczasowy scorer i sterownik pozostają bez zmian.
