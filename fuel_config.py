"""Immutable-at-import runtime configuration for the legacy ThermoFlow fuel engine.

Values and parsing intentionally preserve the original fuel.py behavior. This
module centralizes configuration only; it does not alter runtime policy.
"""
from __future__ import annotations

import os
from datetime import datetime

from fuel_schema import field_number, timezone as thermoflow_timezone

TZ = thermoflow_timezone()
API_TOKEN = os.getenv("FUEL_API_TOKEN", "")
TS_API = "https://api.thingspeak.com"


def _csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "").strip()
    return [item.strip() for item in raw.split(",")] if raw else []


# Kanał 1 (temperatury i moc) z tych samych zmiennych, których używa api.py.
TS_CHANNEL_TEMPS = (_csv_env("THINGSPEAK_CHANNEL_IDS") or [""])[0]
TS_READ_KEY_TEMPS = (_csv_env("THINGSPEAK_READ_KEYS") or [""])[0]
TS_CHANNEL_VALVE = (_csv_env("THINGSPEAK_CHANNEL_IDS") + ["", ""])[1]
TS_READ_KEY_VALVE = (_csv_env("THINGSPEAK_READ_KEYS") + ["", ""])[1]
TS_CHANNEL_FIRE = os.getenv("THINGSPEAK_FIRE_CHANNEL_ID", "").strip()
TS_READ_KEY_FIRE = os.getenv("THINGSPEAK_FIRE_READ_KEY", "").strip()
FIELD_COUNTER = field_number("fuel_percent", "valve_pumps_fuel")
FIELD_OUTDOOR = field_number("outdoor_c", "temperatures_power")
FIELD_POWER = field_number("boiler_power_kw", "temperatures_power")
# Pusty = wykrywany automatycznie z daty utworzenia kanału.
TS_DATA_START = os.getenv("FUEL_TEMPS_DATA_START", "").strip()

# Osobny kanał na dosypki.
TS_CHANNEL_FUEL = os.getenv("THINGSPEAK_FUEL_CHANNEL_ID", "").strip()
TS_READ_KEY_FUEL = os.getenv("THINGSPEAK_FUEL_READ_KEY", "").strip()
TS_WRITE_KEY_FUEL = os.getenv("THINGSPEAK_FUEL_WRITE_KEY", "").strip()

# Domyślna lokalizacja: Hajnówka, Podlaskie; env może ją nadpisać.
FUEL_LAT = os.getenv("FUEL_LAT", "").strip() or "52.74"
FUEL_LON = os.getenv("FUEL_LON", "").strip() or "23.58"
OM_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
OM_FORECAST = "https://api.open-meteo.com/v1/forecast"

# Temperatura bazowa stopniodni, gdy nie da się jeszcze dopasować modelu budynku.
HDD_BASE = float(os.getenv("HDD_BASE", "") or 14.0)


def _parse_hdd_base_fixed(raw: str):
    """FUEL_HDD_BASE_FIXED: opcjonalne ręczne wymuszenie temperatury bazowej.

    Puste ustawienie lub „off”/„auto”/„0” oznacza automatyczne dopasowanie z dosypek.
    """
    raw = (raw or "").strip().lower().replace(",", ".")
    if not raw or raw in ("off", "auto", "none", "false", "0"):
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 8.0 <= value <= 22.0 else None


HDD_BASE_FIXED = _parse_hdd_base_fixed(os.getenv("FUEL_HDD_BASE_FIXED", ""))
MODEL_BASES = [value / 2 for value in range(24, 34)]
MIN_MODEL_PERIODS = 4
OFFSET_MIN_DAYS = 3
OFFSET_CACHE_SECONDS = 3600
OFFSET_FREEZE_DAYS = 14
OFFSET_RECALC_SECONDS = 7 * 24 * 3600
FORECAST_CACHE_SECONDS = 3600
PELLET_KWH_PER_KG = float(os.getenv("PELLET_KWH_PER_KG", "4.8"))
BOILER_EFF = float(os.getenv("BOILER_EFF", "0.88"))
BAG_KG = float(os.getenv("PELLET_BAG_KG", "15"))
HOPPER_KG = float(os.getenv("PELLET_HOPPER_KG", "") or 228)
HOPPER_RESERVE_PERCENT = float(os.getenv("PELLET_HOPPER_RESERVE_PERCENT", "") or 15)
SUMMARY_CACHE_SECONDS = 60
SUMMARY_WAIT_SECONDS = 25

MAX_GAP_S = 300
MIN_DAILY_BURNER_COVERAGE = 0.90
MAX_COUNTER_GAP_S = 900
MAX_DAYS = 120
MIN_PERIOD_DAYS = 2.0
MAX_PERIODS = 50

CLIMATE_CACHE_SECONDS = 30 * 24 * 3600
CLIMATE_YEARS = 5
TELEMETRY_HISTORY_CACHE_SECONDS = 6 * 3600
TELEMETRY_PARTIAL_CACHE_SECONDS = 10 * 60
TELEMETRY_FETCH_CHUNK_DAYS = 3
WEATHER_VARS = "shortwave_radiation_sum,wind_speed_10m_max"
MIN_DAILY_DAYS = 30
DAILY_MODEL_MIN_GAIN = 0.03
MIN_COUNTER_DROP = 20
COUNTER_COLLECTING_MESSAGE = "kalibracja licznika po zmianie podajnika: zbieranie danych"
COUNTER_PROVISIONAL_KG_PER_PERCENT = 3.4
COUNTER_ESTIMATE_UNCERTAINTY = 0.10
MIN_SPAN_DROP = 10
REFILL_MERGE_SECONDS = 30 * 60
BURNER_FACTOR_AFTER_EPOCH = 0.94


def _parse_epoch(raw: str) -> datetime:
    """Chwila zmiany wydajności podajnika: od niej licznik ma inny współczynnik kg na 1 %."""
    default = datetime.fromisoformat("2026-11-13T18:27:00+01:00")
    try:
        parsed = datetime.fromisoformat(raw.strip()) if raw.strip() else default
    except ValueError:
        return default
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)


COUNTER_EPOCH = _parse_epoch(os.getenv("FUEL_COUNTER_EPOCH", ""))
