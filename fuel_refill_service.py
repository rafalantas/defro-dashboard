"""Refill command validation and orchestration.

HTTP authentication/body decoding stay in the API facade. ThingSpeak transport,
cache invalidation and raw refill retrieval are injected from the compatibility
layer so the command semantics remain identical.
"""
from __future__ import annotations

from datetime import datetime


def add_refill(data: dict, deps: dict) -> dict:
    try:
        kg = float(data["kg"]) if "kg" in data else float(data["bags"]) * deps["bag_kg"]
    except (KeyError, TypeError, ValueError):
        raise deps["fuel_error"](400, "Podaj 'kg' albo 'bags'") from None
    if not 0 < kg <= 1000:
        raise deps["fuel_error"](400, "kg poza zakresem")

    dt = None
    if data.get("ts"):
        try:
            dt = datetime.fromisoformat(str(data["ts"]))
        except ValueError:
            raise deps["fuel_error"](400, "Nieprawidłowy format ts (ISO 8601)") from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=deps["tz"])

    fields = {"field1": kg}
    if data.get("note"):
        fields["field2"] = str(data["note"])[:255]
    entry_id = deps["write"](fields, dt)
    return {
        "id": entry_id,
        "ts": (dt or deps["now"]()).isoformat(),
        "kg": kg,
    }


def void_refill(raw_id: str, deps: dict) -> dict:
    try:
        refill_id = int(raw_id)
    except ValueError:
        raise deps["fuel_error"](400, "Nieprawidłowe id") from None
    if refill_id not in {row["id"] for row in deps["refills_raw"]()}:
        raise deps["fuel_error"](404, "Nie ma takiej dosypki")
    return {
        "voided": refill_id,
        "void_entry": deps["write"]({"field3": refill_id}),
    }
