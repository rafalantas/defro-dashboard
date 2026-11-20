"""Backward-compatible import name for the ThermoFlow 2.0 runtime.

Production code loads the engine through :mod:`fuel_engine`. Existing callers
and tests that import ``fuel`` receive the exact same module object so legacy
monkeypatching and private helper imports continue to work during migration.
"""
from __future__ import annotations

import importlib as _importlib
import sys as _sys

_runtime = _importlib.import_module("thermoflow2_runtime")
_sys.modules[__name__] = _runtime
