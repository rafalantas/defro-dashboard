# ThermoFlow — wizualizacja instalacji

Responsywna wizualizacja instalacji z obiegiem podłogowym 3D, grzejnikowym 4D, zasobnikiem CWU i animowanymi przepływami. Docker uruchamia frontend nginx oraz tylko-odczytowy backend TECH/eModul. Backend udostępnia stronie temperatury, stany urządzeń, zawór wbudowany, paliwo, tryb i informacje o wersji. Dane są odświeżane co 30 sekund i buforowane przez 20 sekund.

Zawór 3D pozostaje ręczny. Strona nie wysyła poleceń do sterownika. Gdy API nie udostępnia danego czujnika, panel pokazuje „Brak odczytu”.

## Wdrożenie przez Portainer i GitHub

1. W istniejącym repozytorium `rafalantas/defro-dashboard` zastąp pliki aplikacji zawartością tego folderu, w tym katalog `.github/workflows/`. Workflow buduje i publikuje dwa obrazy do GHCR.
2. Nie dodawaj do repozytorium prawdziwego tokenu ani pliku `.env`. `.gitignore` pomija `.env` i katalog `secrets/`.
3. W Portainerze dodaj GitHub Container Registry jako registry, jeśli obrazy są prywatne. Możesz też ustawić oba pakiety GHCR jako publiczne.
4. Wybierz **Stacks → Add stack → Git repository**. Podaj `https://github.com/rafalantas/defro-dashboard`, gałąź `main` i ścieżkę `docker-compose.yml`.
5. W zmiennych środowiskowych stacka dodaj `TECH_API_TOKEN` ze świeżym tokenem eModul. Poprzedni token został ujawniony w rozmowie — wygeneruj nowy przed wdrożeniem. Opcjonalnie dodaj `TECH_MODULE_ID`, jeśli konto ma więcej niż jeden sterownik.
6. Wdróż stack. Portainer pobierze obrazy z GHCR i uruchomi dwa kontenery. Dashboard będzie dostępny pod `http://ADRES-DOCKERA:5001`.
7. Żeby wdrożenie następowało automatycznie po pushu: włącz webhook dla stacka w Portainerze i zapisz otrzymany URL jako sekret repozytorium GitHub o nazwie `PORTAINER_WEBHOOK_URL` (**Settings → Secrets and variables → Actions**). Workflow po udanej publikacji obu obrazów wywoła ten webhook.

Token pozostaje zmienną stacka i nie trafia do kodu strony ani repozytorium.

## Lokalny Docker Compose

Skopiuj `.env.example` do `.env`, ustaw `TECH_API_TOKEN` i opcjonalnie `TECH_MODULE_ID`, a następnie uruchom lokalny wariant budujący obrazy:

```sh
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d
```

Otwórz [http://localhost:5001](http://localhost:5001). Bez tokenu strona działa w trybie demo. Status backendu: `/api/health`. Zatrzymanie: `docker compose down`.
