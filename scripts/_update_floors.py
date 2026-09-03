"""Raise scripts/coverage_floors.json to what the current run achieved.

Only ever raises: a floor that could go down is not a ratchet. Run after a
PR lifts coverage, and commit the result with it.
"""

from __future__ import annotations

import json
import math
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
FLOORS = REPO / "scripts" / "coverage_floors.json"


def _floor(percent: float) -> float:
    return math.floor(percent * 10) / 10


def main(report_path: str) -> int:
    report = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))
    data = json.loads(FLOORS.read_text(encoding="utf-8"))
    modules = dict(data["modules"])
    for path, file_data in report["files"].items():
        name = path.replace("\\", "/").split("custom_components/unifi_play/")[-1]
        if name == "__init__.py":
            continue
        achieved = _floor(float(file_data["summary"]["percent_covered"]))
        if achieved > modules.get(name, 0.0):
            print(f"{name}: {modules.get(name, 0.0)} -> {achieved}")
        modules[name] = max(modules.get(name, 0.0), achieved)
    total = _floor(float(report["totals"]["percent_covered"]))
    if total > data["total"]:
        print(f"TOTAL: {data['total']} -> {total}")
    data["total"] = max(data["total"], total)
    data["modules"] = dict(sorted(modules.items()))
    FLOORS.write_text(json.dumps(data, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "coverage.json"))
