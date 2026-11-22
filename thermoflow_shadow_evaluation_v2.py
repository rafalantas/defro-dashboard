"""Aggregate, paired, chronological evaluation for frozen ThermoFlow shadow ledgers."""
from math import sqrt
from thermoflow_shadow_evaluation import evaluate_shadow

SCHEMA = "thermoflow-shadow-evaluation-v2/1"


def _number(value):
    return type(value) in (int, float) and abs(value) <= 1e12


def _metrics(rows, kind):
    if not rows:
        return None
    if kind == "regression":
        errors = [prediction - actual for actual, prediction in rows]
        return {"samples": len(errors), "mae": sum(abs(x) for x in errors) / len(errors),
                "rmse": sqrt(sum(x*x for x in errors) / len(errors)),
                "bias": sum(errors) / len(errors)}
    negatives = sum(not actual for actual, _ in rows)
    positives = len(rows) - negatives
    fp = sum(not actual and prediction for actual, prediction in rows)
    fn = sum(actual and not prediction for actual, prediction in rows)
    return {"samples": len(rows), "positives": positives, "negatives": negatives,
            "false_positives": fp, "false_negatives": fn,
            "false_positive_rate": fp / negatives if negatives else None,
            "false_negative_rate": fn / positives if positives else None}


def evaluate_shadow_v2(ledger):
    """Add global paired metrics, coverage and chronological stability."""
    base = evaluate_shadow(ledger)
    kind, start, end = ledger["kind"], ledger["start"], ledger["end"]
    paired = {"baseline": [], "candidate": []}
    eligible = verified = 0
    invalid = {"baseline": 0, "candidate": 0}
    for case in ledger["cases"]:
        if not isinstance(case, dict) or type(case.get("decision_at")) is not int:
            continue
        if not start <= case["decision_at"] < end:
            continue
        eligible += 1
        label = case.get("label")
        label_ok = (isinstance(label, dict) and label.get("verified") is True
                    and type(label.get("available_at")) is int
                    and type(case.get("outcome_end")) is int
                    and case["outcome_end"] <= label["available_at"] <= ledger["as_of"]
                    and (type(label.get("value")) is bool if kind == "alert"
                         else _number(label.get("value"))))
        if not label_ok:
            continue
        verified += 1
        values = {}
        for side in paired:
            prediction = case.get(side)
            if not isinstance(prediction, dict):
                invalid[side] += 1
                continue
            issued, cutoff, value = prediction.get("issued_at"), prediction.get("data_cutoff"), prediction.get("value")
            valid_value = type(value) is bool if kind == "alert" else _number(value)
            if (type(issued) is not int or type(cutoff) is not int
                    or not cutoff <= issued <= case["decision_at"] or not valid_value):
                invalid[side] += 1
                continue
            values[side] = value
        if len(values) == 2:
            for side in paired:
                paired[side].append((label["value"], values[side]))
    windows = base["windows"]
    stability = []
    for side in paired:
        populated = [w["paired_metrics"][side] for w in windows if w["paired_metrics"][side] is not None]
        first, last = (populated[0], populated[-1]) if populated else (None, None)
        if kind == "regression" and first and last:
            stability.append({"variant": side, "first_mae": first["mae"],
                              "last_mae": last["mae"], "mae_change": last["mae"] - first["mae"]})
        else:
            stability.append({"variant": side, "first": first, "last": last})
    count = len(paired["candidate"])
    return {
        "schema": SCHEMA, "task": ledger["task"], "kind": kind, "as_of": ledger["as_of"],
        "mode": "offline_shadow_only", "promotion": "disabled",
        "coverage": {"eligible_cases": eligible, "verified_labels": verified,
                     "paired_cases": count,
                     "label_coverage": verified / eligible if eligible else None,
                     "paired_coverage": count / eligible if eligible else None,
                     "invalid_predictions": invalid},
        "paired_metrics": {side: _metrics(rows, kind) for side, rows in paired.items()},
        "chronological_stability": stability, "windows": windows,
        "status": "evaluated" if count else "collecting",
        "limitations": ["Ledger validation does not prove physical label provenance.",
                       "Synthetic fixtures validate mechanics, not real-installation model quality.",
                       "No training, automatic promotion, or runtime control is performed."]}


if __name__ == "__main__":
    import argparse, json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate_shadow_v2(json.loads(args.ledger.read_text(encoding="utf-8"))),
                     indent=2, allow_nan=False))
