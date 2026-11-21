"""Evaluate frozen shadow outputs in chronological, non-overlapping windows."""
import argparse
import json
from collections import Counter
from math import ceil, sqrt
from pathlib import Path

INPUT_SCHEMA = "thermoflow-shadow-ledger/1"
REPORT_SCHEMA = "thermoflow-shadow-evaluation/1"
SIDES = ("baseline", "candidate")


def _time(value):
    return type(value) is int and 0 < value <= 253402300799


def _value(value, kind):
    return type(value) is bool if kind == "alert" else (
        type(value) in (int, float) and -1e12 <= value <= 1e12)


def _duration(value):
    return type(value) in (int, float) and 0 <= value <= 3600000


def _cost(values):
    values = sorted(values)
    if not values:
        return {"samples": 0, "mean_ms": None, "p95_ms": None}
    return {"samples": len(values), "mean_ms": sum(values) / len(values),
            "p95_ms": values[ceil(len(values) * 0.95) - 1]}


def _metrics(rows, kind):
    if not rows:
        return None
    if kind == "regression":
        errors = [prediction - actual for actual, prediction in rows]
        n = len(errors)
        return {"mae": sum(abs(err) for err in errors) / n,
                "rmse": sqrt(sum(err ** 2 for err in errors) / n),
                "bias": sum(errors) / n}
    negatives = sum(not actual for actual, _ in rows)
    positives = len(rows) - negatives
    false_positives = sum(not actual and prediction for actual, prediction in rows)
    false_negatives = sum(actual and not prediction for actual, prediction in rows)
    return {"positives": positives, "negatives": negatives, "false_positives": false_positives,
            "false_negatives": false_negatives,
            "true_positives": positives - false_negatives,
            "true_negatives": negatives - false_positives,
            "false_positive_rate": false_positives / negatives if negatives else None,
            "false_negative_rate": false_negatives / positives if positives else None}


def evaluate_shadow(ledger):
    if not isinstance(ledger, dict) or ledger.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"expected {INPUT_SCHEMA}")
    kind, cases = ledger.get("kind"), ledger.get("cases")
    start, end, as_of = (ledger.get(key) for key in ("start", "end", "as_of"))
    width = ledger.get("window_seconds")
    task = ledger.get("task")
    if (not isinstance(task, str) or not 1 <= len(task) <= 80 or kind not in ("alert", "regression")
            or not isinstance(cases, list) or not all(map(_time, (start, end, as_of)))
            or not start < end <= as_of or type(width) is not int
            or not 1 <= width or (end - start + width - 1) // width > 366):
        raise ValueError("invalid task, chronological range, windows, or cases")
    ids = Counter(case["id"] for case in cases if isinstance(case, dict)
                  and isinstance(case.get("id"), str) and case["id"])
    rejected = Counter()
    by_window = [[] for _ in range((end - start + width - 1) // width)]
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
            rejected["invalid_case"] += 1
            continue
        if ids[case["id"]] != 1:
            rejected["duplicate_case"] += 1
            continue
        decision, outcome_end = case.get("decision_at"), case.get("outcome_end")
        if not _time(decision) or not _time(outcome_end) or decision >= outcome_end:
            rejected["invalid_timeline"] += 1
            continue
        if not start <= decision < end:
            rejected["outside_window"] += 1
            continue
        by_window[(decision - start) // width].append(case)

    windows = []
    for index, group in enumerate(by_window):
        counts = Counter()
        scores = {side: [] for side in SIDES}
        costs = {side: [] for side in SIDES}
        for case in group:
            label = case.get("label")
            label_ok = (isinstance(label, dict) and label.get("verified") is True
                        and _time(label.get("available_at"))
                        and case["outcome_end"] <= label["available_at"] <= as_of
                        and _value(label.get("value"), kind))
            counts["verified_labels"] += label_ok
            predictions = {}
            for side in SIDES:
                prediction = case.get(side)
                if not isinstance(prediction, dict):
                    continue
                issued, cutoff = prediction.get("issued_at"), prediction.get("data_cutoff")
                if (not _time(issued) or not _time(cutoff)
                        or not cutoff <= issued <= case["decision_at"]
                        or not _value(prediction.get("value"), kind)):
                    counts[f"{side}_invalid"] += 1
                    continue
                counts[f"{side}_valid"] += 1
                predictions[side] = prediction["value"]
                duration = prediction.get("duration_ms")
                if _duration(duration):
                    costs[side].append(duration)
            if label_ok and len(predictions) == len(SIDES):
                counts["paired_labeled"] += 1
                for side in SIDES:
                    scores[side].append((label["value"], predictions[side]))

        # UTC half-open decision-time windows, including windows with no data.
        window_start = start + index * width
        windows.append({"start": window_start, "end": min(window_start + width, end),
                        "eligible_cases": len(group), "verified_labels": counts["verified_labels"],
                        "paired_labeled": counts["paired_labeled"],
                        "coverage": {side: {"valid": counts[f"{side}_valid"],
                                            "invalid": counts[f"{side}_invalid"],
                                            "ratio": counts[f"{side}_valid"] / len(group) if group else None}
                                     for side in SIDES},
                        "paired_metrics": {side: _metrics(scores[side], kind) for side in SIDES},
                        "recorded_compute": {side: _cost(costs[side]) for side in SIDES}})

    return {"schema": REPORT_SCHEMA, "task": task, "kind": kind, "as_of": as_of,
            "mode": "offline_shadow_only", "promotion": "disabled",
            "status": "evaluated" if any(w["paired_labeled"] for w in windows) else "collecting",
            "excluded_cases": dict(sorted(rejected.items())), "windows": windows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate_shadow(json.loads(args.ledger.read_text(encoding="utf-8"))),
                     indent=2, allow_nan=False))
