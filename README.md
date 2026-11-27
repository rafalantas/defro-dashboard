# DEFRO Dashboard

[![Build and publish images](https://github.com/rafalantas/defro-dashboard/actions/workflows/docker.yml/badge.svg?branch=main)](https://github.com/rafalantas/defro-dashboard/actions/workflows/docker.yml)

Nowoczesny panel do podglądu instalacji grzewczej z kotłem DEFRO i sterownikiem TECH/eModul. Pokazuje animowany schemat przepływu wody, obieg podłogowy z zaworem 3D, obieg grzejnikowy z zaworem 4D, zasobnik CWU oraz dostępne odczyty i stany sterownika.

Integracja z eModul działa tylko do odczytu. Dashboard nie steruje kotłem, pompami ani zaworami. Jeśli sterownik nie udostępnia danego odczytu, panel pokaże brak danych.

## Funkcje

- Animowany, responsywny schemat instalacji i przepływów.
- Temperatury oraz stany dostępne z kafelków eModul.
- Wykres historii temperatur i pozostałych serii eModul dla dnia, tygodnia, miesiąca i roku, z eksportem zaznaczonych serii do CSV.
- Stan pomp, wentylatora, podajnika, zaworu, zasobnika CWU i sterownika — zależnie od danych udostępnionych dla danego urządzenia.
- Tryb demonstracyjny bez tokenu eModul.
- Odczyt nazw sterowników i ich identyfikatorów przez `/api/modules`.
- Dwa obrazy Docker publikowane automatycznie w GitHub Container Registry (GHCR).

## Architektura

| Kontener | Rola | Dostęp |
| --- | --- | --- |
| `defro-dashboard` | Nginx i interfejs WWW | Port `5001` na hoście |
| `defro-dashboard-api` | Backend Python, odczyt eModul | Tylko wewnątrz sieci Docker |
| `thermoflow-fibaro-collector` | Historia HC3 w trybie wyłącznie GET | Tylko wewnątrz sieci Docker |
| `thermoflow-thingspeak-collector` | Backfill i przyrostowy zapis historii ThingSpeak do wspólnego SQLite | Tylko wewnątrz sieci Docker |
| `defro-dashboard-cloudflared` | Opcjonalny tunel Cloudflare | Profil Compose `cloudflare`, bez publikowania portu |

Przeglądarka łączy się z API przez Nginx. Token eModul pozostaje w środowisku kontenera API i nie jest wysyłany do przeglądarki. Tunel kieruje do Nginx; ścieżki `/api/...` dashboardu są dostępne przez ten sam adres, ale kontener API nie dostaje osobnego publicznego portu.

## Wdrożenie w Portainerze

Stack używa gotowych obrazów z GHCR. Workflow GitHub Actions publikuje je po każdym pushu do `main`:

- `ghcr.io/rafalantas/defro-dashboard:latest`
- `ghcr.io/rafalantas/defro-dashboard-api:latest`

1. W Portainerze wybierz **Stacks → Add stack → Git repository**.
2. Podaj repozytorium `https://github.com/rafalantas/defro-dashboard`, gałąź `main` i ścieżkę Compose `docker-compose.yml`.
3. Dodaj zmienną stacka `TECH_API_TOKEN` z aktualnym tokenem eModul.
4. Wdróż stack. Panel będzie dostępny pod `http://ADRES-DOCKERA:5001`.

### Dostęp przez Cloudflare Tunnel

Tunel jest opcjonalny i nie wymaga przekierowania portów na routerze. W Cloudflare utwórz tunel **Cloudflared**, skopiuj jego token, a następnie w Portainerze dodaj do zmiennych stacka:

- `CLOUDFLARE_TUNNEL_TOKEN` — token tunelu z Cloudflare.
- `COMPOSE_PROFILES` — wartość `cloudflare`, aby uruchomić kontener tunelu.

W konfiguracji tunelu dodaj **Public Hostname** dla swojej domeny i ustaw usługę na `http://thermoflow:80`. To wewnętrzny adres serwisu dashboardu w sieci Compose. Ustaw też w Cloudflare Access regułę logowania (np. dozwolone adresy e-mail), żeby dashboard i jego ścieżki API nie były publicznie dostępne dla każdego. Po zapisaniu zmian zaktualizuj stack w Portainerze; kontener `defro-dashboard-cloudflared` połączy się z Cloudflare. Port `5001` pozostaje dostępny lokalnie tak jak wcześniej.

Nie kieruj tunelu na API, Docker Engine ani Portainera. Ten stack udostępnia przez tunel tylko interfejs dashboardu wraz z jego wymaganym proxy API.

Jeśli obrazy GHCR są prywatne, dodaj GHCR w Portainerze jako registry z prawem pobierania pakietów. Nie umieszczaj tokenu eModul w repozytorium, pliku Compose ani w kodzie strony.

### Wybór sterownika

Domyślnie backend wybiera pierwszy sterownik z konta eModul. Aby wskazać konkretny, otwórz `http://ADRES-DOCKERA:5001/api/modules` i skopiuj jego `udid` do zmiennej stacka `TECH_MODULE_ID`. Zapisz stack ponownie, aby odtworzyć kontener API z wybranym sterownikiem.

### Aktualizacje obrazów

Po pushu GitHub Actions buduje i publikuje obrazy. Jeśli Portainer nie może być osiągnięty z internetu, workflow nie może sam wywołać jego webhooka. W takim układzie ręcznie wybierz stack i użyj **Pull and redeploy** / **Update the stack** z pobraniem najnowszych obrazów.

Automatyczne wdrażanie bez publicznego dostępu do Portainera można skonfigurować na dwa sposoby:

- **Portainer Business Edition:** GitOps z pollingiem repozytorium i ponownym pobieraniem obrazów.
- **Portainer Community Edition:** self-hosted GitHub Actions runner w tej samej sieci co Portainer, który uruchomi webhook lokalnie. Repozytorium jest publiczne, dlatego runner powinien wykonywać wyłącznie zaufany proces wdrożeniowy.

Obecny workflow obsługuje opcjonalny sekret `PORTAINER_WEBHOOK_URL`. Używaj go tylko wtedy, gdy GitHub Actions rzeczywiście może połączyć się z adresem webhooka. Samo ustawienie sekretu nie zapewni dostępu do prywatnej sieci.

## Uruchomienie lokalne

Wymagane: Docker z Compose.

1. Skopiuj `.env.example` do `.env`.
2. Ustaw `TECH_API_TOKEN`; opcjonalnie ustaw `TECH_MODULE_ID`.
3. Zbuduj i uruchom usługi:

```sh
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d
```

Panel: `http://localhost:5001`.

Zatrzymanie usług:

```sh
docker compose -f docker-compose.yml -f docker-compose.local.yml down
```

## API

| Ścieżka | Zastosowanie |
| --- | --- |
| `/api/health` | Status konfiguracji i połączenia |
| `/api/thermoflow/data-readiness?days=30` | Read-only audyt wspólnego SQLite: zakresy i źródła telemetrii, historia ThingSpeak, kolektory, integralność i gotowość ML; zakres 1–90 dni. |
| `/api/status` | Bieżące dane sterownika używane przez dashboard |
| `/api/statistics?period=day\|week\|month\|year` | Historia ThingSpeak do wykresów i CSV |
| `/api/history?hours=12&step=120&series=thermoflow-valve4d` | Historia ThingSpeak z filtrem serii, oknem 1–24 h, opcjonalnym próbkowaniem i gotową analizą cykli palnika oraz ochrony powrotu |
| `/api/modules` | Nazwy, identyfikatory i wersje sterowników na koncie |
| `/api/diagnostics` | Odczyty diagnostyczne eModul: kafelki, kompletna struktura stref, dostępność menu MU/MI/MS/MP oraz podsumowanie serii statystyk |
| `/api/fuel/stats?days=30` | Zużycie peletu: okresy między dosypkami, stopniodni, kg na stopniodzień, szacunek dzienny z mocy palnika |
| `/api/fuel/summary` | Podsumowanie do karty „Zużycie peletu”: ostatnie 7 dni, kg na stopniodzień, czas od dosypki i szacowany stan zasobnika |
| `/api/fuel/telemetry-audit?days=7` | Audyt jakości 16 serii ThingSpeak (próbki, zakres czasu, odstępy, luki) oraz agregaty dobowe temperatur, mocy, zaworu i czasu pracy pomp; zakres 1–14 dni, tylko odczyt. |
| `/api/fuel/daily?days=60` | Zużycie w pełnych dobach z licznika podajnika obok temperatury, promieniowania słonecznego i wiatru (Open-Meteo), odchyłka od modelu domu oraz stan modelu dobowego |
| `/api/fuel/refills` | Lista dosypek z kanału ThingSpeak „Pelet” |
| `POST /api/fuel/refill` | Dodanie dosypki `{"kg": 30}` lub `{"bags": 2}`, opcjonalnie `ts` i `note`; wymaga nagłówka `X-Api-Token` |
| `DELETE /api/fuel/refill/<id>` | Anulowanie błędnej dosypki (ThingSpeak nie usuwa pojedynczych wpisów); wymaga `X-Api-Token` |

Osobny kontener `thermoflow-thingspeak-collector` pobiera surowe feedy GET z dwóch głównych kanałów ThingSpeak oraz skonfigurowanych kanałów ognia i dosypek. Pierwszy backfill obejmuje domyślnie 14 dni (konfigurowalne 1–30), w dwudniowych porcjach; odpowiedzi osiągające limit 8000 rekordów są automatycznie dzielone na mniejsze przedziały. Kolektor zachowuje oryginalne znaczniki czasu i identyfikatory wpisów, zapisuje surowe feedy z oznaczeniem kanału do `telemetry_samples`, a dosypki dodatkowo do istniejącego `refill_events`. Checkpointy i nakładka 120 sekund pozwalają bezpiecznie wznowić pracę po restarcie; ponowny odczyt nie nadpisuje istniejących wpisów. Kolektor używa wyłącznie kluczy Read API, nie kluczy zapisu. Dane Fibaro pozostają osobnym, niezmienianym archiwum zdarzeń w tej samej bazie SQLite.

Backend pobiera dane eModul w tle co około 30 sekund; odpowiedź jest buforowana przez 20 sekund. Nowe próbki wysyłane są do dwóch kanałów ThingSpeak co 40 sekund (minimum 35 s), bez zapisywania historii na dysku Raspberry Pi. Wykres i CSV korzystają wyłącznie z ThingSpeak. Kanały muszą być prywatne lub publiczne z odpowiednimi kluczami odczytu; każde pole należy włączyć i nazwać zgodnie z listą poniżej. ThingSpeak zwraca starsze dane w agregowanych przedziałach: 10 minut dla miesiąca i 4 godziny dla roku; dlatego szczegóły przełączeń pomp najlepiej analizować dla krótszych zakresów. CSV używa separatora `;`, polskich przecinków dziesiętnych oraz czasu lokalnego `Europe/Warsaw` i odpowiadającego mu czasu UTC ISO 8601. Kolektor historii uzupełnia SQLite o wpisy, które faktycznie istnieją w skonfigurowanych kanałach i mieszczą się w zakresie backfillu; nie odtworzy pomiarów sprzed utworzenia kanału.
Endpoint diagnostyczny pobiera dane bezpośrednio z eModul na żądanie. Oprócz kafelków i ich surowych parametrów zwraca całą strukturę `zones` (w tym `system`, `globalSchedules` i `controllerParameters`), wykryte pozycje menu MU/MI/MS/MP z informacją, które menu konto może odczytać, oraz zwarte podsumowanie serii statystyk. Pełne próbki zapisane w ThingSpeak są dostępne przez `/api/history?hours=12`. `hours` przyjmuje 1–24; alternatywnie można podać dokładne `start` i `end` w ISO 8601 (zakres maksymalnie 24 h). Parametr `series` filtruje serie, a `step=120` łączy je w przedziały 120-sekundowe: temperatury i moc są uśredniane, zaś nastawy, otwarcie, pompy i paliwo zachowują ostatnią wartość z przedziału. `step` może wynosić 60–3600 sekund. Domyślny format `compact` jest bezstratny: serie pomiarowe zawierają punkt `[unix_timestamp_seconds, value]` tylko przy zmianie wartości (wartość obowiązuje do następnego punktu) oraz ostatnią próbkę, a pompy są zwracane jako przedziały pracy `intervals: [[start, koniec], ...]` z łącznym czasem `on_minutes`. Każda seria ma `summary` (min, max, średnia, pierwsza i ostatnia wartość). `format=full` zwraca wszystkie próbki jak wcześniej. Zakresy dłuższe niż 12 h bez `step` są automatycznie łączone w przedziały 120 s (`step_automatic: true`), żeby cała doba mieściła się w jednej odpowiedzi. Blok `analysis` (wyłączany `analysis=0`) zawiera przebiegi palnika (start, koniec, minuty, maksymalna moc) oraz cykle ochrony powrotu: czas czekania na minimalnym otwarciu, czas otwierania do 80%, zimną przerwę liczoną do chwili, gdy temperatura zaworu 4D wraca do zadanej minus 5 K, minimalny powrót i stan pompy podłogówki, a także średnie zimne przerwy z włączoną i wyłączoną podłogówką. Alias `thermoflow-valve4d` wybiera temperaturę zaworu, otwarcie, powrót i zadaną po korekcie; działają też aliasy `valve-opening`, `return` i `setpoint`. Kolejność serii: zawór 4D, temperatura zewnętrzna i pokoju, kocioł, podłogówka i CWU, na końcu pompy i paliwo; zerowe odczyty temperatury pokoju są pomijane, a duplikaty łączone po serii i czasie. Menu serwisowe lub producenta może być niedostępne bez kodu albo na koncie użytkownika; diagnostyka pokaże wtedy błąd dostępności zamiast zakładać, że dane istnieją. Przy diagnozowaniu brakujących czujników otwórz `http://ADRES-DOCKERA:5001/api/diagnostics`.

## Konfiguracja

| Zmienna | Wymagana | Opis |
| --- | --- | --- |
| `TECH_API_TOKEN` | Tak dla danych live | Token Bearer eModul. Bez niego dashboard działa w trybie demo. |
| `TECH_MODULE_ID` | Nie | `udid` sterownika wybranego z `/api/modules`. Puste pole wybiera pierwszy sterownik. |
| `CLOUDFLARE_TUNNEL_TOKEN` | Nie | Token Cloudflare Tunnel; używany tylko przy aktywnym profilu `cloudflare`. |
| `COMPOSE_PROFILES` | Nie | Ustaw `cloudflare`, aby uruchomić opcjonalny kontener tunelu. |
| `THINGSPEAK_CHANNEL_IDS` | Tak dla historii | Dwa identyfikatory kanałów, oddzielone przecinkiem, w kolejności: temperatury i moc, zawór/pompy i paliwo. |
| `THINGSPEAK_WRITE_KEYS` | Tak dla historii | Dwa klucze Write API w tej samej kolejności co kanały. Sekret Portainera. |
| `THINGSPEAK_READ_KEYS` | Nie | Dwa klucze Read API w tej samej kolejności. Dla kanałów publicznych można zostawić puste. |
| `THERMOFLOW_THINGSPEAK_HISTORY_DAYS` | Nie | Zakres pierwszego backfillu; domyślnie 14 dni, dopuszczalne 1–30. |
| `THERMOFLOW_THINGSPEAK_INITIAL_START` | Nie | Opcjonalny początek backfillu jako UTC Unix timestamp; ma zastosowanie przy pierwszym uruchomieniu lub dodaniu nowego kanału. |
| `THERMOFLOW_THINGSPEAK_INTERVAL_SECONDS` | Nie | Odstęp cyklu kolektora; domyślnie 300 sekund. |
| `THERMOFLOW_THINGSPEAK_REQUEST_INTERVAL_SECONDS` | Nie | Minimalny odstęp między żądaniami GET; domyślnie 15 sekund. |
| `THINGSPEAK_FIRE_WRITE_KEY` | Wymagany dla zapisu czujnika ognia | Klucz Write API osobnego kanału czujnika ognia. |
| `THINGSPEAK_FIRE_READ_KEY` | Nie | Klucz Read API kanału czujnika ognia, jeśli kanał jest prywatny. |
| `THINGSPEAK_WRITE_INTERVAL_SECONDS` | Nie | Odstęp zapisu; domyślnie 40 sekund, minimum 35. |
| `THINGSPEAK_FIRE_CHANNEL_ID` | Nie | Osobny kanał ThingSpeak na stan i jasność czujnika ognia; nie zmienia dwóch dotychczasowych kanałów. |
| `THINGSPEAK_FUEL_CHANNEL_ID` | Nie | Osobny kanał ThingSpeak na dosypki peletu. Bez niego endpointy `/api/fuel/*` zwracają pustą listę dosypek. |
| `THINGSPEAK_FUEL_WRITE_KEY` | Dla dosypek | Klucz Write API kanału „Pelet”. Sekret Portainera. |
| `THINGSPEAK_FUEL_READ_KEY` | Nie | Klucz Read API kanału „Pelet”, jeśli kanał jest prywatny. |
| `FUEL_API_TOKEN` | Dla dosypek | Token wymagany w nagłówku `X-Api-Token` przy dodawaniu i anulowaniu dosypek, np. wynik `openssl rand -hex 24`. |
| `FUEL_LAT`, `FUEL_LON` | Zalecane | Współrzędne domu. Stopniodni dla dni sprzed kanału temperatur są liczone z archiwum Open-Meteo. |
| `FUEL_COUNTER_EPOCH` | Nie | Chwila zmiany wydajności podajnika (ISO 8601, bez strefy to czas lokalny). Domyślnie `2026-11-13T18:27:00+01:00`. Współczynnik kg na 1 % licznika jest kalibrowany osobno dla okresów przed i po niej. |
| `PELLET_HOPPER_KG` | Nie | Pojemność zasobnika w kg do karty w dashboardzie. Domyślnie 228 kg (Gamma 40 wg instrukcji, gęstość nasypowa 0,6 kg/dm³). |
| `HDD_BASE` | Nie | Temperatura bazowa stopniodni, dopóki nie ma 4 okresów do dopasowania modelu domu; domyślnie 14 °C. |
| `FUEL_HDD_BASE_FIXED` | Nie | Stała temperatura bazowa stopniodni; domyślnie 15,5 °C. Wszystkie wskaźniki kg na stopniodzień (karta, `/api/fuel/summary`, `/api/fuel/stats`, okresy) liczą się przy tej bazie, a model dopasowuje przy niej tylko stałą część i kg na stopień. Dopasowanie bazy z zakresu 12,0-16,5 °C jest liczone dalej, ale tylko informacyjnie (`model.best_fit_balance_temp`). Wartość `off` albo `auto` wyłącza stałą bazę i wraca do dopasowywanej (wtedy `HDD_BASE` obowiązuje do czasu dopasowania modelu). |
| `PELLET_HOPPER_RESERVE_PERCENT` | Nie | Rezerwa, do której karta liczy czas; domyślnie 15% (zalecane minimum z instrukcji). |
| `PELLET_BAG_KG` | Nie | Masa worka przy dodawaniu dosypki przez `bags`; domyślnie 15 kg. |
| `PORTAINER_WEBHOOK_URL` | Nie | Sekret GitHub Actions do wywołania webhooka po publikacji obrazów; wymaga osiągalnego adresu z runnera. |

### Pola kanałów ThingSpeak

W obu kanałach włącz pola i nadaj im poniższe nazwy. Zmienne `THINGSPEAK_CHANNEL_IDS`, `THINGSPEAK_WRITE_KEYS` oraz `THINGSPEAK_READ_KEYS` muszą zachować tę samą kolejność kanałów.

| Kanał | Pola 1–8 |
| --- | --- |
| 3566081 · Temperatury i moc | Temperatura CO (°C), Temperatura CWU (°C), Temperatura spalin (°C), Temperatura zewnętrzna (°C), Temperatura pokoju (°C), Temperatura podłogówki (C1), Temperatura zaworu 4D (°C), Moc chwilowa pieca (kW) |
| 3566084 · Zawór, pompy i paliwo | Zadana zaworu 4D po korekcie (°C), Otwarcie zaworu 4D (%), Temperatura powrotu (°C), Pompa zaworu 4D (0/1), Pompa podłogowa (0/1), Pompa CWU (0/1), Pompa cyrkulacyjna (0/1), Zapas paliwa (%) |

Dodatkowy, opcjonalny kanał czujnika ognia (ID z `THINGSPEAK_FIRE_CHANNEL_ID`, klucz z `THINGSPEAK_FIRE_WRITE_KEY`): Field 1 `fire_detected` (0 = brak płomienia, 1 = płomień), Field 2 `fire_brightness` (jasność z eModul). Zapis odbywa się w tym samym interwale co pozostała telemetria. Wartość `0` jasności jest zapisywana; brak odczytu pozostaje pustym polem. Kanał nie jest wymagany do działania obecnej telemetrii.

Zadana zaworu 4D po korekcie jest wyliczana jako wartość `setTemp` plus podpisana korekta `setTempCorrection`; korekta dodatnia podwyższa, a ujemna obniża temperaturę. W Portainerze ustaw dokładnie dwa ID oraz dwa klucze zapisu w tej samej kolejności, bez spacji. Dla prywatnych kanałów ustaw również dwa klucze odczytu. Nie dodawaj starego kanału 3566098. Klucze API nie powinny trafiać do repozytorium ani do rozmowy. W ThingSpeak ustaw interwał aktualizacji kanału na co najmniej 40 sekund.

### Zużycie peletu

Wpisy dosypek oddalone o mniej niż 30 minut są scalane w jedną dosypkę (suma kg, czas ostatniego wpisu), np. 6 kg i 84 kg dodane w odstępie kilkunastu sekund to jedna dosypka 90 kg; `/api/fuel/refills` zwraca scaloną listę z polem `ids` (wpisy składowe), a `DELETE /api/fuel/refill/<id>` anuluje pojedynczy wpis. Dosypki są zapisywane w osobnym kanale ThingSpeak „Pelet”, bez zapisu na dysk Raspberry Pi. Kanał ma trzy pola: Field 1 `kg`, Field 2 `notatka`, Field 3 `anulowany_wpis`. Darmowe konto ThingSpeak przyjmuje jeden zapis na 15 sekund na kanał; przy zbyt szybkim kolejnym wpisie API zwraca HTTP 429.

Założenie: przy każdej dosypce zasobnik jest uzupełniany do tego samego poziomu, więc ilość dosypana jest zużyciem od poprzedniej dosypki. Pierwsza dosypka jest punktem startowym. Ze wszystkich okresów między dosypkami dopasowywany jest model domu: kg na dobę = stała część (CWU, cyrkulacja i straty niezależne od pogody) + kg na każdy stopień poniżej temperatury granicznej. Temperatura graniczna jest wybierana z zakresu 12–16,5 °C tak, żeby model najlepiej pasował do danych (wymaga co najmniej 4 okresów; do tego czasu baza wynosi `HDD_BASE`, domyślnie 14 °C). Karta pokazuje kg na stopniodzień samego ogrzewania, czyli po odjęciu stałej części, więc wskaźnik można porównywać między miesiącami i sezonami. Dla dni sprzed kanału temperatur Open-Meteo jest korygowane o średnią różnicę względem czujnika sterownika, liczoną z pełnych dób, w których są oba źródła. Czas do rezerwy zasobnika jest liczony z prognozy pogody na 7 dni i modelu domu. Po zebraniu co najmniej 30 pełnych dób z licznika podajnika backend dopasowuje też model dobowy i sprawdza, czy suma promieniowania słonecznego i wiatr (iloczyn maksymalnej prędkości wiatru i stopniodni) poprawiają prognozę. Czynnik zostaje tylko wtedy, gdy zmniejsza błąd prognozy dla dób pominiętych przy dopasowaniu (walidacja LOO) o co najmniej 3% i ma fizycznie sensowny znak; wtedy prognoza zużycia i czas do rezerwy uwzględniają prognozę słońca i wiatru. Dla każdego okresu między dosypkami `/api/fuel/stats` liczy stopniodni dla temperatury granicznej budynku, kg na dobę, kg na stopniodzień oraz część grzewczą (`heating`). Temperatura zewnętrzna pochodzi z pierwszego kanału ThingSpeak, a dla dni sprzed jego utworzenia z archiwum Open-Meteo. Zużycie bieżące jest liczone przede wszystkim z licznika zapasu sterownika (czas pracy podajnika, pole 8 drugiego kanału), skalibrowanego w kg na 1 % do sumy dosypek z całego przedziału z danymi licznika; resety licznika do 100 % są pomijane, a moment resetu nie musi pokrywać się z dosypką. Rezerwą jest energia z serii mocy chwilowej, kalibrowana tak samo z całego przedziału, a potem model domu. Latem, przy znikomych stopniodniach, wskaźnik kg na stopniodzień nie ma sensu, a zużycie odpowiada głównie przygotowaniu CWU. Karta „Zużycie peletu” w dashboardzie odświeża się co 5 minut i pokazuje zużycie z ostatnich 7 dób, kg na stopniodzień z ostatnich dosypek, czas od ostatniej dosypki oraz szacowaną ilość w zasobniku z czasem do rezerwy 15% (zalecane minimum z instrukcji kotła; przy pustym zasobniku płomień gaśnie, a sterownik zgłasza nieudane rozpalenie, które trzeba skasować ręcznie); kafelek zmienia kolor, gdy do rezerwy zostaje mniej niż doba; karta jest ukryta, gdy kanał „Pelet” nie jest skonfigurowany. Agregaty dzienne są trzymane w pamięci. Po starcie kontenera cache dla karty „Zużycie peletu” jest rozgrzewany w tle (równolegle, kilka minut przy pełnej historii), a `/api/fuel/summary` odpowiada od razu: ostatnim wynikiem albo `{"configured": true, "warming": true}`, po czym dashboard ponawia zapytanie co 15 s, więc nginx nie zwraca 504. Kolejne odświeżenia (co 60 s) też liczą się w tle.

Po zmianie wydajności podajnika (`FUEL_COUNTER_EPOCH`) współczynnik kg na 1 % licznika jest liczony osobno: „stary” z okresów między dosypkami kończących się przed epoką, „nowy” z okresów zaczynających się po niej (okres obejmujący epokę nie kalibruje żadnego z nich). `counter_calibration` w `/api/fuel/summary`, `/api/fuel/stats` i `/api/fuel/daily` ma pola `old`, `new`, `collecting` i `message`, a pola najwyższego poziomu opisują współczynnik obowiązujący teraz. Korekta Open-Meteo do czujnika sterownika (średnia różnica z dób z obu źródeł) jest od 14 dób zamrożona i przeliczana raz w tygodniu, a nie przy każdym zapytaniu (`om_offset.frozen`); krótsza historia przelicza się co godzinę. Dzięki temu stopniodni i model domu nie przesuwają się od drobnych zmian prognoz Open-Meteo. Okres obejmujący epokę (od dosypki przed nią do pierwszej po niej) jest rozdzielany: część przed epoką liczona starym współczynnikiem, a reszta dosypanych kg przypisana części po epoce, z czego wychodzi oszacowanie nowego współczynnika (`counter_calibration.span`, niepewność ok. ±10 %, wymaga spadku licznika po epoce o co najmniej 10 pp). Dopóki nie ma pełnego okresu między dosypkami po epoce, licznik używa tego oszacowania, a gdy go jeszcze nie ma, wartości tymczasowej 3,4 kg na 1 % (±10 %). Karta oznacza to jako szacunek („kalibracja licznika po zmianie podajnika: zbieranie danych, tymczasowo 3,40 kg/% (szacunek, ±10%)” albo „szacunek z okresu obejmującego zmianę podajnika”; źródło „licznik podajnika (szacunek)”). `counter_calibration.new_effective` mówi, który współczynnik obowiązuje po epoce i skąd pochodzi. Moc palnika ma osobną kalibrację przed i po epoce (`calibration.old` i `calibration.new`); do pierwszego pełnego okresu po epoce obowiązuje oczekiwany współczynnik 0,94, także oznaczony jako szacunek. Doby w `/api/fuel/daily` używają współczynnika właściwego dla danej doby (`counter_regime`: `old`, `new`, `epoch`; `estimate: true` dla doby ze współczynnikiem tymczasowym), doba ze zmianą podajnika jest pomijana, a model dobowy nie uczy się na dobach-szacunkach. Model dobowy sprawdza też bezwładność budynku: kandydat `t_lag` = 0,6 × średnia temperatura doby + 0,4 × średnia temperatura poprzedniej doby, ze stopniodniami `hdd_lag` przy tej samej bazie co pozostałe wskaźniki (15,5 °C). Wiersze w `/api/fuel/daily` mają pola `t_lag` i `hdd_lag`. Kandydat (sam albo ze słońcem i wiatrem) zostaje tylko wtedy, gdy zmniejsza błąd prognozy dla dób, których model nie widział (LOO), o co najmniej 3 % i ma sensowny znak; wtedy `daily_model` ma `terms` z `hdd_lag`, `temperature_term: "t_lag"` i `lag_weights`, a prognoza zużycia na karcie liczy dzień z temperatury doby i wczorajszej. `daily_model.candidates` pokazuje błąd LOO wszystkich sprawdzonych kandydatów.

Historię dosypek można jednorazowo zaimportować skryptem `tools/import_refills.py` z pliku z wierszami `DD.MM.YYYY<TAB>kg` (wiersze w innym formacie są pomijane):

```bash
export THINGSPEAK_FUEL_CHANNEL_ID=...
export THINGSPEAK_FUEL_WRITE_KEY=...
python3 tools/import_refills.py dosypki.txt          # podgląd
python3 tools/import_refills.py dosypki.txt --send   # zapis do ThingSpeak
```

Import wykonaj przed pierwszym wpisem dodanym przez API. Nowe dosypki najwygodniej dodawać Skrótem w iOS: „Poproś o dane wejściowe” (liczba) → „Pobierz zawartość URL” z metodą POST na `https://TWOJA-DOMENA/api/fuel/refill`, nagłówkami `X-Api-Token` i `Content-Type: application/json` oraz treścią JSON `kg` = dane wejściowe → „Pokaż powiadomienie”.

## Struktura repozytorium

```text
.
├── README.md                     # opis, wdrożenie i konfiguracja
├── .github/workflows/docker.yml  # budowanie i publikowanie obrazów
├── api.py                        # tylko-odczytowy backend eModul
├── fuel.py                       # zgodna wstecz aliasowa nazwa runtime ThermoFlow 2.0
├── thermoflow2_runtime.py        # kompozycja providerów i usług paliwa
├── tools/import_refills.py       # jednorazowy import historii dosypek
├── index.html                    # dashboard i animowany schemat
├── favicon.svg                   # ikona dashboardu
├── nginx.conf                    # serwowanie strony i proxy do API
├── Dockerfile                    # obraz frontendu
├── Dockerfile.api                # obraz backendu
├── docker-compose.yml            # konfiguracja Portainera / GHCR
│                                 # oraz opcjonalny Cloudflare Tunnel
├── docker-compose.local.yml      # lokalne budowanie obrazów
├── .env.example                  # szablon lokalnej konfiguracji
├── .dockerignore
└── .gitignore
```

Repozytorium zawiera 15 plików źródłowych i konfiguracyjnych. Każdy jest używany przez aplikację, lokalne uruchomienie, CI albo ochronę sekretów; katalogu nie zaśmiecają kopie wynikowych obrazów ani wygenerowane pliki.


### Telemetria w eksperymentalnym modelu paliwa

Dobowe rekordy modelu AI są łączone z agregatami z dwóch kanałów ThingSpeak w strefie `Europe/Warsaw`. Dla ostatnich maksymalnie 14 dni do rekordu z licznikiem paliwa dołączane są średnie temperatur CO, CWU, podłogówki i powrotu, średnia moc kotła, średnie otwarcie zaworu 4D oraz czasy pracy pomp podłogowej, CWU i cyrkulacyjnej. Pompa zaworu 4D pozostaje wyłącznie w surowym audycie.

Cechy analogowe są przyjmowane tylko przy co najmniej 1000 próbek na dobę, a czasy pomp tylko przy co najmniej 20 godzinach znanego stanu. Brakujące lub niepełne dane nie są interpolowane. Model AI porównuje wariant pogodowy z wariantem rozszerzonym o telemetrię na wspólnym zbiorze kompletnych dób i chronologicznym holdoucie. Cechy telemetryczne wymagają co najmniej 90 kompletnych dób treningowych i 14 testowych (minimum 104 kompletne doby); do tego czasu API raportuje status `collecting`, liczbę kompletnych i niekompletnych dób oraz progi. Doby z brakującą albo fizycznie niepoprawną telemetrią nie są interpolowane. Daty są walidowane jako rzeczywiste daty ISO `YYYY-MM-DD`, a wszystkie rekordy dla zduplikowanego dnia są wykluczane, aby nie nadawać tej dobie podwójnej wagi. API raportuje powody odrzucenia i luki między dniami. Wariant telemetryczny jest zaakceptowany wyłącznie przy poprawie MAE o co najmniej 3% w obu protokołach.

Integracja `fuel_ai.py` działa w trybie shadow i nie zmienia produkcyjnych estymat paliwa. Obliczenia paliwa są obsługiwane przez runtime ThermoFlow 2.0, a prognoza dashboardu zachowuje dotychczasowy kontrakt. System pozostaje odczytowy i nie steruje kotłem. Publiczny audyt nadal ma limit 14 dni, natomiast wewnętrzny loader modelu pobiera historię do 120 dni i buforuje agregaty przez 6 godzin. Starsze rekordy paliwa bez dostępnej historii ThingSpeak pozostają bez cech instalacji.


### Diagnostyka cyklu rozpalenia (shadow)

Dedykowany kanał `THINGSPEAK_FIRE_CHANNEL_ID` zapisuje wyłącznie:
- Field 1: `fire_detected` (0/1),
- Field 2: jasność płomienia.

Temperatura CO pochodzi z istniejącego kanału telemetrycznego ThingSpeak. Aktualna zadana CO i histereza kotła są pobierane bezpośrednio z bieżącego snapshotu API eModul i nie są dublowane w ThingSpeak. Ponieważ zadana i histereza są stałymi nastawami instalacji, audyt stosuje ich bieżące wartości do historycznych próbek CO. Nie zapisujemy ich cyklicznie w ThingSpeak; odpowiedź audytu oznacza to jawnie jako założenie stałości nastaw.

Do odczytu kanału służy opcjonalna zmienna `THINGSPEAK_FIRE_READ_KEY` (pusty klucz dla kanału publicznego). Audyt wyznacza żądanie rozpalenia, gdy temperatura kotła przechodzi z wartości powyżej do wartości równej lub niższej niż `zadana - histereza`. Następnie koreluje je z pojawieniem się płomienia, aktywnością spalin i powrotem temperatury kotła do zadanej. Jest to wnioskowanie z próbek telemetrycznych, nie bezpośredni odczyt komendy sterownika. Diagnostyka jest wyłącznie read-only i nie zmienia sterowania ani nastaw.


### Odczyt telemetrii z lokalnej bazy (SQLite)

Kolektor ThingSpeak kopiuje każdy wpis kanałów telemetrii do `telemetry_samples`. Odczyty zakresu
(`feeds.json` z parametrami `start`, `end`, `results`) są obsługiwane z tej kopii (`thermoflow_local_feeds.py`),
w dokładnie tym samym kształcie co odpowiedź ThingSpeaka, więc modele i analizy nie wiedzą o zmianie. Dotyczy
silnika paliwa (podsumowanie, model dobowy, zużycie dobowe, licznik), `/api/history` oraz odczytu kanału FIRE.
Zimny start paliwa nie pobiera już kilkunastu MB JSON z internetu.

Odpowiedź lokalna jest używana tylko wtedy, gdy jest równoważna. W każdym innym przypadku zapytanie idzie do
ThingSpeaka jak dotąd, a pierwszy taki przypadek dla kanału trafia do logu (`Local feeds not used for channel …`):

- zapytanie z agregacją po stronie ThingSpeaka (`timescale`, `average`, …) albo bez `results`;
- okno zaczyna się przed pierwszą skopiowaną próbką kanału;
- kolektor nie dotarł jeszcze do końca okna (kopia może być spóźniona o więcej niż 15 minut);
- pasuje więcej wpisów niż `results` (nie zgadujemy reguły ucinania ThingSpeaka).

Nadal z ThingSpeaka (bez zmian): statystyki miesięczne i roczne `/api/statistics` (używają `timescale`),
dosypki z kanału „Pelet" (kolektor kopiuje tylko ostatnie dni, a dosypki sięgają kwietnia), Open-Meteo oraz
wszystkie zapisy. Bieżący stan kotła nadal pochodzi z eModul na żywo; kopia lokalna może być spóźniona do ok.
5 minut względem ThingSpeaka, co dla modeli nie ma znaczenia.

| Zmienna | Opis |
| --- | --- |
| `THERMOFLOW_READ_SOURCE` | `auto` (domyślnie): najpierw lokalna baza, ThingSpeak jako zapas. `thingspeak`: zawsze ThingSpeak (natychmiastowe wycofanie, bez zmiany obrazu). |
| `THERMOFLOW_LOCAL_FEEDS_START` | Opcjonalnie: data/czas ISO (UTC), od którego kopia lokalna jest kompletna. Pozwala obsłużyć okna sprzed pierwszej skopiowanej próbki, gdy ThingSpeak i tak nie ma tam danych. Bez niej takie okna idą do ThingSpeaka. |

Historia kolektora: `THERMOFLOW_THINGSPEAK_HISTORY_DAYS` przyjmuje najwyżej 30 dni; dłuższy start ustaw przez
`THERMOFLOW_THINGSPEAK_INITIAL_START` (Unix, sekundy).

### Zimno i dlaczego (reguły, tylko odczyt)

Endpoint `GET /api/fibaro/comfort?hours=24` (1–72 h) zasila sekcję „Zimno i dlaczego" w zakładce
Fibaro. Czyta wyłącznie lokalną bazę SQLite (nie łączy się z HC3, niczego nie zapisuje, nie wysyła
poleceń), tak jak `/api/fibaro/dashboard`, i nie wymaga tokenu. Odpowiedź jest buforowana w pamięci
przez 30 s.

Źródłem stanu są udane przebiegi kolektora Fibaro co ok. 5 minut: stan w danej chwili to ostatnia
zapisana migawka stref i głowic. Luka dłuższa niż 15 minut przerywa epizod i nie jest uzupełniana.
HC3 nie zapisuje zmian `heatRequired` ani `windowOpened` w historii zdarzeń, dlatego zapotrzebowanie i
okna są znane tylko z migawek (od początku ich zapisu).

- **Zimno** to strefa co najmniej 0,5 K poniżej aktywnej nastawy przez co najmniej 20 minut;
  krócej to „dogrzewa się". **Przegrzanie** to co najmniej 1,0 K ponad nastawę. Progi są stałymi
  w `thermoflow_fibaro_comfort.py`.
- **Przyczyna** to hipoteza z listą dowodów i poziomem pewności, nigdy dowód przyczynowości:
  podniesiona nastawa tuż przed epizodem, otwarte okno wykryte przez głowicę, brak zapotrzebowania
  mimo niedoboru, zapotrzebowanie zgłoszone a strefa nie dogrzewa się, spadek temperatury.
  Gdy dowodów brak, wynik to „brak danych wskazujących przyczynę".
- **Temperatura** to zmierzone `currentTemperature` strefy. **Nastawa** to `currentSetpointHeating`,
  a gdy HC3 go nie podaje (tak jest w tej instalacji), `currentTemperatureHeating`, czyli aktywna nastawa
  z harmonogramu (zmienia się np. z 20,5 na 19,5 °C w nocy). `handSetPointHeating` (ręczne nadpisanie)
  nie jest używane. Ta sama reguła (`zone_reading` w `thermoflow_fibaro_diagnostics.py`) obowiązuje w
  diagnostyce, kartach stref i tej sekcji. Źródło nastawy zwraca API w polu `setpoint_source`.
- Odczyty temperatury poza zakresem 5–40 °C (np. 0 °C z zaniku łączności) są pomijane.
- Wersja nie uwzględnia pracy kotła, zaworu 4D ani pogody, więc nie odróżnia braku mocy źródła
  ciepła od innych przyczyn. To kolejny etap.

### Fibaro HC3 Telemetry Intelligence (shadow)

Endpoint `GET /api/fibaro/telemetry` exposes a consolidated, read-only telemetry
contract for the mapped HC3 climate zones and physical FGT001 radiator heads.
It uses the same `FIBARO_HC3_API_TOKEN` Bearer authentication as the other
`/api/fibaro/*` endpoints.

The response includes:

- snapshot freshness and the completeness of the zone-to-head mapping;
- per-head `heatRequired`, `windowOpened`, battery percentage and HC3
  `dead` communication state, preserving missing values as `unknown`;
- P2 configuration compliance (expected P2 value: 322) and the Moderate
  regulator bit-6 state;
- per-zone current temperature, active heating setpoint, temperature gap and
  tri-state heat demand;
- bounded aggregate features under `ai_shadow.features`.

The compatible FGT001 head inventory is discovered dynamically from the
current HC3 climate-zone device assignments. Runtime code contains no fixed
head IDs or fixed expected head count. Mapping and P2 checks are reported as
explicit diagnostics; they do not trigger device writes.
Battery values at or below 20% are labelled low for maintenance visibility.
The endpoint is an on-demand snapshot, not a historical series: it reports
`ai_shadow.training_ready=false` and `history_available=false`. Missing or
malformed values remain unknown rather than being converted to false.

Fibaro features are shadow-only. They do not change `/api/fuel/*`, Fuel
Forecast, model training, model promotion or any boiler settings. The HC3
provider remains GET-only; there is no background polling, raw-history
persistence or device command. Return-temperature protection remains
55°C ±1°C.
