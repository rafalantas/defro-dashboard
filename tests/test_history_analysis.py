"""Regression tests for configured 4D minimum opening in history analysis."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api


def sample_history():
    return {
        "thermoflow-boiler-power": [],
        "thermoflow-valve4d-opening": [
            [0, 80], [60, 50], [120, 32], [180, 30], [240, 35], [300, 80],
        ],
        "thermoflow-valve4d-current": [[300, 40]],
        "thermoflow-valve4d-effective-setpoint": [[0, 40]],
        "thermoflow-valve4d-return": [[0, 60], [300, 56]],
        "thermoflow-pump-floor": [[0, 1]],
        "thermoflow-pump-valve4d": [[0, 1]],
    }


def test_history_analysis_uses_configured_minimum_opening():
    result = api._history_analysis(sample_history(), minimum_opening=30)

    assert result["protection_cycles"]
    assert result["protection_cycles"][0]["min_opening"] == 30


def test_history_analysis_keeps_legacy_fallback_when_config_missing():
    result = api._history_analysis(sample_history())

    assert result["protection_cycles"]
    assert result["protection_cycles"][0]["min_opening"] == 32


def test_history_analysis_rejects_out_of_range_minimum_and_uses_fallback():
    result = api._history_analysis(sample_history(), minimum_opening=130)

    assert result["protection_cycles"]
    assert result["protection_cycles"][0]["min_opening"] == 32

if __name__ == "__main__":
    test_history_analysis_uses_configured_minimum_opening()
    test_history_analysis_keeps_legacy_fallback_when_config_missing()
    test_history_analysis_rejects_out_of_range_minimum_and_uses_fallback()
    print("History analysis regression: configured minimum opening and fallback: OK")
