"""Read-only Fibaro HC3 API provider for ThermoFlow.

Only GET requests are implemented. No controller, climate-zone or device writes
are exposed by this module.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_HISTORY_WINDOW_SECONDS = 6 * 60 * 60
DEFAULT_SPLIT_THRESHOLD = 800
DEFAULT_MAX_RECORDS = 1000


class HC3Error(RuntimeError):
    """Sanitized upstream/configuration error; never includes credentials."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class HC3Config:
    base_url: str
    username: str
    password: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, environ=None) -> "HC3Config":
        env = os.environ if environ is None else environ
        credentials_path = str(env.get("FIBARO_HC3_CREDENTIALS_FILE", "")).strip()
        values = {}
        if credentials_path:
            try:
                with open(credentials_path, encoding="utf-8") as stream:
                    values = json.load(stream)
            except (OSError, ValueError, TypeError):
                raise HC3Error(503, "Nie można odczytać pliku poświadczeń Fibaro HC3") from None
            if not isinstance(values, dict):
                raise HC3Error(503, "Nieprawidłowy format pliku poświadczeń Fibaro HC3")

        base_url = str(env.get("FIBARO_HC3_URL") or values.get("url") or "").strip().rstrip("/")
        username = str(env.get("FIBARO_HC3_USERNAME") or values.get("username") or "").strip()
        password = str(env.get("FIBARO_HC3_PASSWORD") or values.get("password") or "")
        try:
            timeout = float(env.get("FIBARO_HC3_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            raise HC3Error(503, "Nieprawidłowy timeout Fibaro HC3") from None

        if not base_url or not username or not password:
            raise HC3Error(503, "Integracja Fibaro HC3 nie jest skonfigurowana")
        if not base_url.startswith(("http://", "https://")) or any(ch in base_url for ch in "\r\n"):
            raise HC3Error(503, "Nieprawidłowy adres Fibaro HC3")
        if not 1 <= timeout <= 60:
            raise HC3Error(503, "Timeout Fibaro HC3 musi mieścić się w zakresie 1–60 s")
        return cls(base_url=base_url, username=username, password=password, timeout_seconds=timeout)


class FibaroHC3Provider:
    """Small read-only client for the HC3 endpoints verified on the target hub."""

    def __init__(self, config: HC3Config, *, opener=None):
        self.config = config
        self._opener = opener or urlopen
        token = base64.b64encode(
            f"{config.username}:{config.password}".encode("utf-8")
        ).decode("ascii")
        self._authorization = f"Basic {token}"

    def _get_json(self, path: str, params=None):
        if not path.startswith("/api/") or "//" in path or ".." in path:
            raise HC3Error(400, "Niedozwolona ścieżka Fibaro HC3")
        url = self.config.base_url + path
        if params:
            url += "?" + urlencode(params)
        request = Request(
            url,
            headers={
                "Authorization": self._authorization,
                "Accept": "application/json",
                "User-Agent": "ThermoFlow-HC3-Provider/1.0",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read()
            return json.loads(raw.decode("utf-8"))
        except HTTPError as error:
            status = error.code if 400 <= error.code < 500 else 502
            raise HC3Error(status, f"Fibaro HC3 zwróciło HTTP {error.code}") from None
        except (TimeoutError, OSError) as error:
            if isinstance(error, TimeoutError):
                raise HC3Error(504, "Przekroczono czas oczekiwania na Fibaro HC3") from None
            raise HC3Error(503, "Nie można połączyć się z Fibaro HC3") from None
        except URLError:
            raise HC3Error(503, "Nie można połączyć się z Fibaro HC3") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HC3Error(502, "Fibaro HC3 zwróciło nieprawidłową odpowiedź JSON") from None

    @staticmethod
    def _positive_id(value) -> int:
        if isinstance(value, bool):
            raise HC3Error(400, "Identyfikator Fibaro HC3 musi być dodatnią liczbą całkowitą")
        try:
            result = int(value)
        except (TypeError, ValueError):
            raise HC3Error(400, "Identyfikator Fibaro HC3 musi być dodatnią liczbą całkowitą") from None
        if result <= 0 or str(result) != str(value).strip():
            raise HC3Error(400, "Identyfikator Fibaro HC3 musi być dodatnią liczbą całkowitą")
        return result

    def get_devices(self):
        """Return the HC3 device inventory."""
        result = self._get_json("/api/devices")
        if not isinstance(result, list):
            raise HC3Error(502, "Nieprawidłowa lista urządzeń Fibaro HC3")
        return result

    def get_device(self, device_id: int):
        """Return one device, including current FGT001 state properties."""
        return self._get_json(f"/api/devices/{self._positive_id(device_id)}")

    def get_climate_zones(self):
        """Return the climate-zone overview."""
        result = self._get_json("/api/panels/climate")
        if not isinstance(result, list):
            raise HC3Error(502, "Nieprawidłowa lista stref klimatycznych Fibaro HC3")
        return result

    def get_climate_zone(self, zone_id: int):
        """Return one climate zone and its schedule."""
        return self._get_json(f"/api/panels/climate/{self._positive_id(zone_id)}")

    def get_events_window(self, start: int, end: int, *, limit=DEFAULT_MAX_RECORDS):
        """Read one bounded event-history window using HC3 Unix timestamps."""
        start, end = self._validate_range(start, end)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= DEFAULT_MAX_RECORDS:
            raise HC3Error(400, "Limit historii Fibaro HC3 musi wynosić od 1 do 1000")
        result = self._get_json(
            "/api/events/history",
            {"from": start, "to": end, "numberOfRecords": limit},
        )
        if isinstance(result, list):
            events = result
        elif isinstance(result, dict) and isinstance(result.get("events"), list):
            events = result["events"]
        elif isinstance(result, dict) and isinstance(result.get("data"), list):
            events = result["data"]
        else:
            raise HC3Error(502, "Nieprawidłowy format historii zdarzeń Fibaro HC3")
        return events

    def get_events_range(
        self,
        start: int,
        end: int,
        *,
        window_seconds=DEFAULT_HISTORY_WINDOW_SECONDS,
        split_threshold=DEFAULT_SPLIT_THRESHOLD,
        limit=DEFAULT_MAX_RECORDS,
    ):
        """Read a complete range, splitting busy windows and deduplicating IDs.

        Fifteen-minute windows are the normal path. A response at or above 800
        records is split recursively before the HC3 1000-record cap is reached.
        Returned events are restricted to the requested timestamp range.
        """
        start, end = self._validate_range(start, end)
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, int) or window_seconds < 1:
            raise HC3Error(400, "Okno historii Fibaro HC3 musi mieć co najmniej 1 sekundę")
        if isinstance(split_threshold, bool) or not isinstance(split_threshold, int) or not 1 <= split_threshold <= limit:
            raise HC3Error(400, "Nieprawidłowy próg dzielenia historii Fibaro HC3")

        unique = {}
        cursor = start
        while cursor < end:
            window_end = min(cursor + window_seconds, end)
            self._collect_window(
                cursor, window_end, unique,
                split_threshold=split_threshold, limit=limit,
            )
            cursor = window_end

        return sorted(unique.values(), key=lambda item: (item["timestamp"], item["id"]))

    def _collect_window(self, start, end, collected, *, split_threshold, limit):
        events = self.get_events_window(start, end, limit=limit)
        if len(events) >= split_threshold:
            if end - start <= 1:
                raise HC3Error(502, "Nie można bezpiecznie podzielić przeciążonego okna historii Fibaro HC3")
            midpoint = start + (end - start) // 2
            self._collect_window(start, midpoint, collected, split_threshold=split_threshold, limit=limit)
            self._collect_window(midpoint, end, collected, split_threshold=split_threshold, limit=limit)
            return

        for event in events:
            if not isinstance(event, dict):
                continue
            event_id = event.get("id")
            timestamp = event.get("timestamp")
            if isinstance(event_id, bool) or not isinstance(event_id, int):
                continue
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
                continue
            if start <= timestamp <= end:
                collected[event_id] = event

    @staticmethod
    def _validate_range(start, end):
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, int) or not isinstance(end, int) or start >= end):
            raise HC3Error(400, "Zakres historii Fibaro HC3 wymaga poprawnych znaczników Unix start < end")
        return start, end
