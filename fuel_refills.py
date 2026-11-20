"""Pure refill-event normalization and close-event merging.

Network access and ThingSpeak credentials remain in the legacy service adapter.
"""
from __future__ import annotations


def normalize_refill_feeds(feeds: list[dict], timezone, parse_timestamp) -> list[dict]:
    """Normalize ThingSpeak refill and void events into chronological refill records."""
    voided = {int(feed["field3"]) for feed in feeds if feed.get("field3") not in (None, "")}
    out = []
    for feed in feeds:
        if feed.get("field1") in (None, ""):
            continue
        try:
            entry_id = int(feed["entry_id"])
        except (KeyError, TypeError, ValueError):
            # ThingSpeak returns numeric IDs, while CSV exports represent them
            # as strings. Normalize before comparing them with void references.
            continue
        if entry_id in voided:
            continue
        out.append({
            "id": entry_id,
            "ts": parse_timestamp(feed["created_at"]).astimezone(timezone).isoformat(),
            "kg": float(feed["field1"]),
            "note": feed.get("field2") or None,
        })
    out.sort(key=lambda row: parse_timestamp(row["ts"]))
    return out


def merge_close_refills(refills: list[dict], merge_seconds: int, parse_timestamp) -> list[dict]:
    """Merge refill entries closer than the configured interval, preserving source IDs."""
    merged: list[dict] = []
    for refill in refills:
        if merged and (parse_timestamp(refill["ts"]) - parse_timestamp(merged[-1]["ts"])).total_seconds() < merge_seconds:
            current = merged[-1]
            current["kg"] = round(current["kg"] + refill["kg"], 3)
            current["ts"], current["id"] = refill["ts"], refill["id"]
            current["ids"].append(refill["id"])
            notes = [note for note in (current.get("note"), refill.get("note")) if note]
            current["note"] = " + ".join(notes) if notes else None
        else:
            merged.append({**refill, "ids": [refill["id"]]})
    return merged
