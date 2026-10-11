# DEFRO Dashboard

[![Build and publish images](https://github.com/rafalantas/defro-dashboard/actions/workflows/docker.yml/badge.svg?branch=main)](https://github.com/rafalantas/defro-dashboard/actions/workflows/docker.yml)

Nowoczesny panel do podglądu instalacji grzewczej z kotłem DEFRO i sterownikiem TECH/eModul. Pokazuje animowany schemat przepływu wody, obieg podłogowy z zaworem 3D, obieg grzejnikowy z zaworem 4D, zasobnik CWU oraz dostępne odczyty i stany sterownika.

Integracja z eModul działa tylko do odczytu. Dashboard nie steruje kotłem, pompami ani zaworami. Jeśli sterownik nie udostępnia danego odczytu, panel pokaże brak danych.

## Funkcje

- Animowany, responsywny schemat instalacji i przepływów.
- Temperatury oraz stany dostępne z kafelków eModul.
- Stan pomp, wentylatora, podajnika, zaworu, zasobnika CWU i sterownika — zależnie od danych udostępnionych dla danego urządzenia.
- Tryb demonstracyjny bez tokenu eModul.
- Odczyt nazw sterowników i ich identyfikatorów przez `/api/modules`.
- Dwa obrazy Docker publikowane automatycznie w GitHub Container Registry (GHCR).

## Architektura

| Kontener | Rola | Dostęp |
| --- | --- | --- |
| `defro-dashboard` | Nginx i interfejs WWW | Port `5001` na hoście |
| `defro-dashboard-api` | Backend Python, odczyt eModul | Tylko wewnątrz sieci Docker |

Przeglądarka łączy się z API przez Nginx. Token eModul pozostaje w środowisku kontenera API i nie jest wysyłany do przeglądarki.

## Wdrożenie w Portainerze

Stack używa gotowych obrazów z GHCR. Workflow GitHub Actions publikuje je po każdym pushu do `main`:

- `ghcr.io/rafalantas/defro-dashboard:latest`
- `ghcr.io/rafalantas/defro-dashboard-api:latest`

1. W Portainerze wybierz **Stacks → Add stack → Git repository**.
2. Podaj repozytorium `https://github.com/rafalantas/defro-dashboard`, gałąź `main` i ścieżkę Compose `docker-compose.yml`.
3. Dodaj zmienną stacka `TECH_API_TOKEN` z aktualnym tokenem eModul.
4. Wdróż stack. Panel będzie dostępny pod `http://ADRES-DOCKERA:5001`.

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
| `/api/status` | Bieżące dane sterownika używane przez dashboard |
| `/api/modules` | Nazwy, identyfikatory i wersje sterowników na koncie |
| `/api/diagnostics` | Odczyty wszystkich kafelków, stref i pozycji menu MU/MI |

Backend pobiera dane eModul co około 30 sekund; odpowiedź jest buforowana przez 20 sekund.
Endpoint diagnostyczny pobiera pełniejszy zestaw danych bezpośrednio z eModul na żądanie. Zwraca m.in. typ i widoczność kafelka, jego przetłumaczoną nazwę oraz parametry i widgety. Przy diagnozowaniu brakujących czujników otwórz `http://ADRES-DOCKERA:5001/api/diagnostics`.

## Konfiguracja

| Zmienna | Wymagana | Opis |
| --- | --- | --- |
| `TECH_API_TOKEN` | Tak dla danych live | Token Bearer eModul. Bez niego dashboard działa w trybie demo. |
| `TECH_MODULE_ID` | Nie | `udid` sterownika wybranego z `/api/modules`. Puste pole wybiera pierwszy sterownik. |
| `PORTAINER_WEBHOOK_URL` | Nie | Sekret GitHub Actions do wywołania webhooka po publikacji obrazów; wymaga osiągalnego adresu z runnera. |

## Struktura repozytorium

```text
.
├── README.md                     # opis, wdrożenie i konfiguracja
├── .github/workflows/docker.yml  # budowanie i publikowanie obrazów
├── api.py                        # tylko-odczytowy backend eModul
├── index.html                    # dashboard i animowany schemat
├── nginx.conf                    # serwowanie strony i proxy do API
├── Dockerfile                    # obraz frontendu
├── Dockerfile.api                # obraz backendu
├── docker-compose.yml            # konfiguracja Portainera / GHCR
├── docker-compose.local.yml      # lokalne budowanie obrazów
├── .env.example                  # szablon lokalnej konfiguracji
├── .dockerignore
└── .gitignore
```

Repozytorium zawiera 12 plików źródłowych i konfiguracyjnych. Każdy jest używany przez aplikację, lokalne uruchomienie, CI albo ochronę sekretów; katalogu nie zaśmiecają kopie wynikowych obrazów ani wygenerowane pliki.
