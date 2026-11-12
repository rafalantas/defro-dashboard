# defro-dashboard: instrukcje dla Claude Code

Dashboard kotła pelletowego Defro Gamma 40 kW (sterownik ST-9730, eModul) z zaworem 4D,
historią w ThingSpeak i śledzeniem zużycia peletu. Działa w Dockerze na Raspberry Pi,
budowany przez GitHub Actions do GHCR i wdrażany stackiem w Portainerze.

## Architektura

- `index.html`: cały frontend (HTML, CSS, JS w jednym pliku), serwowany przez nginx.
- `nginx.conf`: statyczne pliki i proxy `/api/` do kontenera API.
- `api.py`: backend na `http.server` i `urllib` (tylko biblioteka standardowa). Czyta eModul
  (tylko odczyt), co ok. 30 s zapisuje próbki do ThingSpeak (`_poll_forever`, nie usuwać),
  udostępnia `/api/status`, `/api/diagnostics`, `/api/statistics`, `/api/history`.
- `fuel.py`: zużycie peletu, wywoływany z `api.py` przez `fuel.handle()` dla `/api/fuel/*`.
- `tools/import_refills.py`: jednorazowy import historii dosypek.

## Zasady, których pilnujemy

- **Tylko biblioteka standardowa Pythona.** Bez `requests`, Flaska i innych zależności.
- **Brak zapisu na dysk Raspberry Pi.** Dane trwałe są w ThingSpeak, cache tylko w pamięci.
  Pierwsze wywołanie po restarcie może być wolniejsze, to akceptujemy.
- **Sekrety nigdy w repo.** Tokeny i klucze ThingSpeak tylko jako zmienne stacka w Portainerze,
  w `docker-compose.yml` wyłącznie `${ZMIENNA:-}`. Przed każdym commitem sprawdź diff pod kątem kluczy.
- **eModul tylko do odczytu.** Żadnych zapisów do menu sterownika z kodu.
- **Teksty dla użytkownika po polsku**, liczby w formacie polskim (przecinek dziesiętny).
- **Open-Meteo to uzupełnienie.** Błąd Open-Meteo nie może zepsuć endpointu. Wartości z prognozy
  dla dziś i kolejnych dób nie są zamrażane w cache (odświeżanie co ok. godzinę).
- `/api/history` w formacie `compact` musi zostać bezstratny i mieścić pełną dobę w jednej
  odpowiedzi (z analizą na końcu), bo jest używany do analizy danych poza dashboardem.

## ThingSpeak

- Kanał 1 (temperatury i moc), pola: 1 CO, 2 CWU, 3 spaliny, 4 zewnętrzna, 5 pokój,
  6 podłogówka C1, 7 zawór 4D, 8 moc chwilowa.
- Kanał 2 (zawór, pompy, paliwo), pola: 1 zadana 4D po korekcie, 2 otwarcie 4D, 3 powrót,
  4 pompa 4D, 5 pompa podłogówki, 6 pompa CWU, 7 cyrkulacja, 8 licznik zapasu paliwa (%).
- Kanał „Pelet”: 1 kg, 2 notatka, 3 id anulowanego wpisu (ThingSpeak nie usuwa wierszy).
- Darmowe konto: 1 zapis na 15 s na kanał.

## Wiedza o instalacji (ważna przy zmianach w obliczeniach)

- **Moc chwilowa z eModul jest zaniżona** ok. 1,6-1,7 raza. Do zużycia używamy licznika zapasu
  (pole 8 kanału 2, czas pracy podajnika) skalibrowanego w kg na 1 % do dosypek; moc palnika
  to rezerwa.
- Reset licznika do 100 % nie musi pokrywać się z dosypką: liczymy spadki, resety pomijamy.
- Dosypki są „do pełna”, więc ilość dosypana = zużycie od poprzedniej dosypki. Krótkie okresy
  (< 2 dni) są zaszumione i nie wchodzą do modelu.
- Model domu z dosypek: ok. 5 kg/dobę stałej części (CWU, straty) + ok. 3,4 kg na stopień
  poniżej ok. 15 °C średniej dobowej. Zasobnik 228 kg, zalecane minimum 15 %.
- Panel pokojowy (hol, koniec instalacji) steruje korektą zadanej zaworu dla całego domu.
  Obniżenia −0,5 °C w nocy i w środku dnia. Zaniki komunikacji RS dają pojedyncze zera/skoki zadanej.
- Ochrona powrotu przymyka zawór 4D do minimum (30 %); krótkie postoje pompy zaworu to nie ochrona.

## Testy przed każdym commitem

```bash
python3 -m py_compile api.py fuel.py tools/import_refills.py
python3 - <<'EOF'
import re; s = open('index.html', encoding='utf-8').read()
open('/tmp/all.js', 'w').write('\n;\n'.join(re.findall(r'<script[^>]*>(.*?)</script>', s, re.S)))
EOF
node --check /tmp/all.js
# lokalny start i smoke test
FUEL_API_TOKEN=test python3 api.py &
for p in status diagnostics "fuel/summary" "fuel/refills" "fuel/daily" "fuel/stats?days=2" "history?hours=1"; do
  curl -s -o /dev/null -w "$p %{http_code}\n" "http://127.0.0.1:8000/api/$p"; done
```
Przy zmianach w `fuel.py` albo analizie historii dopisz test z podmienionym `_request_json`
(atrapa ThingSpeak i Open-Meteo), zamiast polegać na prawdziwych usługach.

## Sposób pracy

- Zmiany przychodzą jako zlecenia z analizy danych (z czatu). Każde zlecenie realizuj na aktualnym
  `main`, jedną logiczną zmianą na commit, z opisem po polsku.
- Nie nadpisuj plików całymi paczkami: wprowadzaj zmiany w istniejącym kodzie albo nakładaj patch
  przez `git apply --check`, a potem `git apply`.
- Po zmianie zaktualizuj README (endpointy, zmienne środowiskowe), jeśli dotyczy.
