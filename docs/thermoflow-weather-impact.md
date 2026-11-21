# ThermoFlow Weather Impact

Na komputerze roboczym uruchom `python3 -m thermoflow_weather_impact verified-fuel-periods.json`. Do formatu Fuel ML dodaj listę `forecasts`; każdy rekord zawiera `start`, `end`, `issued_at`, `available_at`, `kind: "forecast"` i `forecast_mean_outdoor_c`. Daty wymagają strefy. Średnia prognozowana dotyczy dokładnie znanego z góry zakresu `start`–`end`. `available_at` to rzeczywisty czas pozyskania, a nie godzina modelu przypisana po fakcie. Nie generuj prognozy dla nieznanego w tamtym momencie horyzontu dosypki i nie zastępuj archiwum prognoz pogodą obserwowaną.

Raport pokazuje liczbę kwalifikujących się i sparowanych okresów oraz MAE, RMSE i bias na wspólnych pięciu okresach. Dodatni `holdout_mae_gain_kg_per_day` oznacza niższy błąd modelu z pogodą na tym bloku, nie dowód przyczynowości ani zgodę na zmianę modelu. Brak archiwum prognoz daje `collecting`. Narzędzie nie pobiera prognoz, nie zapisuje ich na Raspberry Pi i nie modyfikuje produkcji.
