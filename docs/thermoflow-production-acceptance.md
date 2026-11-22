# ThermoFlow — odbiór produkcyjny i pozostałe bramki

Ten dokument rozdziela stan kodu od gotowości danych i odbioru na działającej instalacji. Zielone CI potwierdza testy automatyczne, nie jakość modelu na rzeczywistym kotle.

## Co jest już w kodzie

- Wersjonowane API Unified Intelligence tylko do odczytu.
- Health Score i Diagnostic AI oparte na świeżych odczytach, z jawnym pokryciem i ograniczeniami.
- Anomaly Detection 3.0 i Causal Diagnostics: analiza powtarzalnych, zsynchronizowanych zdarzeń pomp, temperatury powrotu i zaworu 4D; korelacja nie jest dowodem przyczynowości.
- Burner ML i Fuel ML: oddzielne benchmarki chronologiczne oraz bramki eksportu artefaktów.
- Shadow inference Burner/Fuel z odrębnym statusem live; brak artefaktu lub świeżego wejścia nie może generować liczbowej prognozy.
- Weather-aware forecast istniejącego planera paliwa. Jedyna bieżąca prognoza kg/d jest w zakładce Paliwo; Weather Impact w Diagnostyce jest testem historycznym.
- Walidacja frontend, Python, runtime API i obrazów Docker dla amd64/arm64 w CI.

## Co nadal blokuje pełny odbiór ML

### 1. Fuel ML — wiarygodne etykiety

Wymagane są fizycznie potwierdzone okresy dosypka–dosypka z tym samym poziomem odniesienia, bez nieujętych dosypek/ubytków, oraz identyfikatory wpisów dosypek. Sam reset procentów sterownika nie potwierdza masy. Nie używać `--assume-equal-fill` jako etykiet produkcyjnych.

### 2. Fuel ML — prognoza bazowa z chwili decyzji

Dla każdego okresu potrzebna jest zachowana prognoza bazowa wydana nie później niż początek okresu. Dzisiejsze przeliczenie starego okresu nie jest archiwalną prognozą i nie może zastąpić tej etykiety.

### 3. Weather Impact — archiwalne prognozy pogody

Benchmark wymaga prognoz faktycznie dostępnych w chwili decyzji dla tych samych zweryfikowanych okresów paliwowych. Późniejsze obserwacje lub reanaliza pogody nie mogą udawać prognozy as-issued.

### 4. Burner ML — odbiór na rzeczywistym kanale FIRE

Potrzebny jest ciągły, surowy zrzut binarnego stanu FIRE z kompletnymi cyklami, bez luk przerywających segment. Jasność płomienia, spaliny ani moc palnika nie zastępują etykiety FIRE. Artefakt wymaga co najmniej 20 próbek treningowych i 5 najnowszych próbek holdout oraz pokonania obu baseline'ów.

### 5. Shadow review na instalacji

Po przygotowaniu artefaktów należy sprawdzić na rzeczywistym runtime:
- czy statusy `collecting`, `stale`, `unavailable` i `predicted` są prawidłowe;
- czy prognoza znika po utracie świeżości danych;
- czy Burner ML pojawia się dopiero po zaobserwowanej granicy OFF → FIRE; start procesu w trakcie FIRE ani luka telemetrii nie mogą tworzyć pozornego pełnego cyklu;
- czy prognoza Burner ML znika po przekroczeniu sześciogodzinnego limitu cyklu oraz po utracie świeżości telemetrii;
- czy Fuel ML nie zastępuje istniejącego planera, stanu zasobnika ani ewidencji paliwa;
- czy porównanie predykcji z późniejszą etykietą odbywa się poza ścieżką sterowania.

Nie promować modelu tylko dlatego, że przeszedł holdout. Najpierw ręczny przegląd jakości danych, błędu, biasu, pokrycia i stabilności w kolejnych okresach.

## Odbiór wdrożenia

- [ ] PR-y scalone do `main` i obrazy GHCR zbudowane z właściwego commita.
- [ ] Portainer pobrał nowe obrazy; kontenery API i WWW działają po odtworzeniu.
- [ ] `/api/health` oraz `/api/thermoflow/v1/intelligence` zwracają poprawną odpowiedź.
- [ ] W panelu nie ma błędów JavaScript ani HTTP 5xx; widok sprawdzony na desktopie i telefonie.
- [ ] Audyt ThingSpeak uruchomiony ręcznie i zakończony; jego wynik nie jest mylony z automatycznym filtrowaniem danych.
- [ ] Przegląd świeżości źródeł, braków i luk telemetrycznych zakończony.
- [ ] Predykcje shadow pozostają informacyjne; nie wykonują zapisów do ThingSpeak ani poleceń do sterownika.

## Niezmienniki bezpieczeństwa

- Ochrona temperatury powrotu pozostaje **55°C ±1°C**. Nie zmieniać tej nastawy w ramach ML ani strojenia UI.
- AI/ML, diagnostyka i prognozy są tylko do odczytu i nie sterują kotłem, pompami, zaworami, podajnikiem, zapłonem ani STB.
- Brak lub luka danych oznacza brak oceny, a nie wynik pozytywny/negatywny.
- Korelacja czasowa nie jest dowodem przyczynowości.
