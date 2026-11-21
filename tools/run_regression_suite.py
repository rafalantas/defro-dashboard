"""Run every test file in isolation, including standalone assert-based regressions."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
STANDALONE = {
    "test_counter_epoch.py", "test_daily_lag.py", "test_frontend_assets.py",
    "test_hdd_base.py", "test_history_analysis.py", "test_overview.py",
}


def main():
    files = sorted((ROOT / "tests").glob("test_*.py"))
    failed = []
    for test in files:
        if test.name in STANDALONE:
            arguments = [str(test)]
        else:
            arguments = ["-m", "unittest", "discover", "-s", "tests", "-p", test.name]
        result = subprocess.run([sys.executable, *arguments], cwd=ROOT, check=False,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        state = "FAIL" if result.returncode else "PASS"
        print(f"{state} {test.name}", flush=True)
        if result.returncode:
            failed.append(test.name)
    print(f"Regression files: {len(files) - len(failed)}/{len(files)} passed.")
    if failed:
        print("Failed: " + ", ".join(failed))
    return int(not files or bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
