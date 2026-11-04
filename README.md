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
| `/api/status` | Bieżące dane sterownika używane przez dashboard |
| `/api/statistics?period=day\|week\|month\|year` | Historyczne serie eModul i ThingSpeak do wykresów oraz CSV |
| `/api/modules` | Nazwy, identyfikatory i wersje sterowników na koncie |
| `/api/diagnostics` | Odczyty wszystkich kafelków, stref i pozycji menu MU/MI |

Backend pobiera dane eModul w tle co około 30 sekund; odpowiedź jest buforowana przez 20 sekund. Nowe próbki wysyłane są do trzech kanałów ThingSpeak co 40 sekund (minimum 35 s), bez zapisywania historii na dysku Raspberry Pi. Kanały muszą być prywatne lub publiczne z odpowiednimi kluczami odczytu; każde pole należy włączyć i nazwać zgodnie z listą poniżej. Historia ThingSpeak jest dołączana do wykresów i CSV. ThingSpeak zwraca starsze dane w agregowanych przedziałach: 10 minut dla miesiąca i 4 godziny dla roku; dlatego szczegóły przełączeń pomp najlepiej analizować dla krótszych zakresów. CSV używa separatora `;`, polskich przecinków dziesiętnych oraz czasu lokalnego `Europe/Warsaw` i odpowiadającego mu czasu UTC ISO 8601. Próbki sprzed konfiguracji kanałów nie zostaną uzupełnione.
Endpoint diagnostyczny pobiera pełniejszy zestaw danych bezpośrednio z eModul na żądanie. Zwraca m.in. typ i widoczność kafelka, jego przetłumaczoną nazwę oraz parametry i widgety. Przy diagnozowaniu brakujących czujników otwórz `http://ADRES-DOCKERA:5001/api/diagnostics`.

## Konfiguracja

| Zmienna | Wymagana | Opis |
| --- | --- | --- |
| `TECH_API_TOKEN` | Tak dla danych live | Token Bearer eModul. Bez niego dashboard działa w trybie demo. |
| `TECH_MODULE_ID` | Nie | `udid` sterownika wybranego z `/api/modules`. Puste pole wybiera pierwszy sterownik. |
| `CLOUDFLARE_TUNNEL_TOKEN` | Nie | Token Cloudflare Tunnel; używany tylko przy aktywnym profilu `cloudflare`. |
| `COMPOSE_PROFILES` | Nie | Ustaw `cloudflare`, aby uruchomić opcjonalny kontener tunelu. |
| `THINGSPEAK_CHANNEL_IDS` | Tak dla historii | Trzy identyfikatory kanałów, oddzielone przecinkami, w kolejności: temperatury, zawór/pompy, nastawy/kocioł. |
| `THINGSPEAK_WRITE_KEYS` | Tak dla historii | Trzy klucze Write API, w tej samej kolejności co kanały. Sekret Portainera. |
| `THINGSPEAK_READ_KEYS` | Nie | Trzy klucze Read API w tej samej kolejności. Dla kanałów publicznych można zostawić puste. Przy pustym środkowym kluczu zachowaj przecinki, np. `klucz1,,klucz3`. |
| `THINGSPEAK_WRITE_INTERVAL_SECONDS` | Nie | Odstęp zapisu; domyślnie 40 sekund, minimum 35. |
| `PORTAINER_WEBHOOK_URL` | Nie | Sekret GitHub Actions do wywołania webhooka po publikacji obrazów; wymaga osiągalnego adresu z runnera. |

### Pola kanałów ThingSpeak

W każdym z trzech kanałów włącz osiem pól i nadaj im poniższe nazwy. Zmienne `THINGSPEAK_CHANNEL_IDS`, `THINGSPEAK_WRITE_KEYS` oraz `THINGSPEAK_READ_KEYS` muszą zachować tę samą kolejność kanałów.

| Kanał | Pola 1–8 |
| --- | --- |
| 1 · Temperatury | Temperatura CO (°C), Temperatura CWU (°C), Temperatura spalin (°C), Temperatura zewnętrzna (°C), Temperatura pokoju (°C), Temperatura podłogówki C1 (°C), Temperatura powrotu (°C), Moc chwilowa pieca (kW) |
| 2 · Zawór i pompy | Temperatura zaworu 4D (°C), Zadana zaworu 4D (°C), Otwarcie zaworu 4D (%), Powrót zaworu 4D (°C), Pompa zaworu 4D (0/1), Pompa podłogowa (0/1), Pompa CWU (0/1), Pompa cyrkulacyjna (0/1) |
| 3 · Nastawy i kocioł | Zadana CO (°C), Zadana CWU (°C), Zapas paliwa (%), Pozostały czas paliwa (h), Sygnał ognia, Wentylator (%), Temperatura podajnika (°C), Chwilowe zużycie paliwa (L/h) |

W Portainerze ustaw trzy ID i klucze zapisu jako wartości oddzielone przecinkami, bez spacji. Dla prywatnych kanałów wklej także trzy klucze odczytu. Klucze API nie powinny trafiać do repozytorium ani do rozmowy. W ThingSpeak ustaw interwał aktualizacji kanału na co najmniej 40 sekund.

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
│                                 # oraz opcjonalny Cloudflare Tunnel
├── docker-compose.local.yml      # lokalne budowanie obrazów
├── .env.example                  # szablon lokalnej konfiguracji
├── .dockerignore
└── .gitignore
```

Repozytorium zawiera 12 plików źródłowych i konfiguracyjnych. Każdy jest używany przez aplikację, lokalne uruchomienie, CI albo ochronę sekretów; katalogu nie zaśmiecają kopie wynikowych obrazów ani wygenerowane pliki.
