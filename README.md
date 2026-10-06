# Defro ST-9730 Dashboard

Dashboard do monitorowania i sterowania piecem pelletowym Defro ST-9730 przez API eModul.eu.

## Funkcje

- Podgląd temperatur CO, C.W.U., spalin, zewnętrznej
- Zmiana trybu pracy (ogrzewanie, priorytet kotła, pompy równoległe, tryb letni)
- Poziom pelletu z szacowanym czasem do zera
- Dane zaworu mieszającego
- Auto-odświeżanie co 30 sekund

## Uruchomienie na Raspberry Pi

### 1. Sklonuj repo

```bash
git clone https://github.com/TWOJ_USER/defro-dashboard.git
cd defro-dashboard
```

### 2. Utwórz plik .env

```bash
cp .env.example .env
nano .env
```

Uzupełnij token JWT z eModul.eu (zakładka Ustawienia → API w aplikacji lub z DevTools).

### 3. Uruchom

```bash
docker compose up -d
```

Dashboard dostępny na `http://IP_RASPBERRY:5001`

## Aktualizacja

Po push na `main` GitHub Actions automatycznie buduje nowy obraz. Żeby zaciągnąć aktualizację na Pi:

```bash
docker compose pull && docker compose up -d
```

## Odświeżanie tokenu

Token JWT wygasa. Żeby odnowić — zaloguj się na emodul.eu, otwórz DevTools → Network, odśwież stronę i skopiuj token z nagłówka `Authorization` dowolnego requestu. Wklej do `.env` i zrestartuj kontener:

```bash
docker compose restart
```

## Struktura

```
defro-dashboard/
├── app/
│   └── main.py          # Flask proxy (pobiera dane z emodul.eu, cache 30s)
├── templates/
│   └── index.html       # Dashboard UI
├── Dockerfile
├── docker-compose.yml
└── .env.example
```
