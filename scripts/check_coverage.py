#!/usr/bin/env python3
"""Per-module coverage gate.

A single repository-wide percentage hides exactly the module you care about:
95% overall is comfortably reachable with `config_flow.py` — the largest file
here, and the one that drives every zone mutation — barely touched. So this
checks three things independently:

* the integration as a whole clears ``OVERALL_MINIMUM``;
* **every** production module clears ``MODULE_MINIMUM``;
* the config-flow modules are at 100%, because a config flow is pure branching
  on user input and a missed branch is a step a user can reach and nothing has
  ever executed.

Reads the JSON report ``pytest --cov-report=json`` writes. Run it as:

    pytest --cov --cov-report=json:coverage.json
    python scripts/check_coverage.py coverage.json

Exits non-zero with a per-module table on failure, so CI says which file is
short rather than only that something is.

**The ratchet.** ``scripts/coverage_floors.json`` records what each module
reaches today. While a module is below target, the floor is what is enforced:
coverage may go up, never down. That makes the gate useful from the first
commit instead of being switched off until the day everything clears 95%,
which is the day it never gets switched on. Passing ``--strict`` ignores the
floors and demands the real targets; the file is deleted, and the flag
becomes the default, once every module clears them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: The integration as a whole.
OVERALL_MINIMUM = 95.0

#: Every production module, individually.
MODULE_MINIMUM = 95.0

#: Modules held to 100%. A config flow has no runtime state to hide behind:
#: every branch is a step a user can reach.
FULL_COVERAGE_MODULES = frozenset({"config_flow.py"})

#: Files that carry no logic to cover. Anything else claiming an exemption
#: needs a reason written here, not a quiet omission.
EXEMPT_MODULES = frozenset(
    {
        # Re-exports and the package marker only.
        "__init__.py",
    }
)

PACKAGE = "custom_components/unifi_play"


def _module_name(path: str) -> str:
    """Return the path relative to the integration package."""
    normalised = path.replace("\\", "/")
    marker = f"{PACKAGE}/"
    index = normalised.find(marker)
    if index >= 0:
        return normalised[index + len(marker) :]
    return normalised


FLOORS_PATH = Path(__file__).resolve().parent / "coverage_floors.json"


def _module_target(module: str) -> float:
    if module in FULL_COVERAGE_MODULES:
        return 100.0
    return MODULE_MINIMUM


def _load_floors(strict: bool) -> dict[str, float]:
    if strict or not FLOORS_PATH.is_file():
        return {}
    data = json.loads(FLOORS_PATH.read_text(encoding="utf-8"))
    return {str(k): float(v) for k, v in data.get("modules", {}).items()}


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    strict = "--strict" in argv[1:]
    report_path = Path(args[0] if args else "coverage.json")
    if not report_path.is_file():
        print(f"coverage report not found: {report_path}", file=sys.stderr)
        print(
            "run: pytest --cov --cov-report=json:coverage.json",
            file=sys.stderr,
        )
        return 2

    report = json.loads(report_path.read_text(encoding="utf-8"))
    files = report.get("files", {})
    if not files:
        print("coverage report contains no files", file=sys.stderr)
        return 2

    floors = _load_floors(strict)
    overall_floor = OVERALL_MINIMUM
    if not strict and FLOORS_PATH.is_file():
        overall_floor = float(
            json.loads(FLOORS_PATH.read_text(encoding="utf-8")).get(
                "total", OVERALL_MINIMUM
            )
        )

    rows: list[tuple[str, float, float, float, list[int]]] = []
    for path, data in sorted(files.items()):
        module = _module_name(path)
        if module in EXEMPT_MODULES:
            continue
        percent = float(data["summary"]["percent_covered"])
        target = _module_target(module)
        required = min(target, floors.get(module, target))
        rows.append((module, percent, required, target, data.get("missing_lines", [])))

    overall = float(report["totals"]["percent_covered"])

    failures = [row for row in rows if row[1] + 1e-9 < row[2]]
    overall_short = overall + 1e-9 < min(OVERALL_MINIMUM, overall_floor)

    width = max((len(row[0]) for row in rows), default=10)
    header = f"{'module':<{width}}  {'covered':>8}  {'floor':>8}  {'target':>8}"
    print(header)
    print("-" * len(header))
    for module, percent, required, target, _missing in rows:
        flag = "" if percent + 1e-9 >= required else "  FAIL"
        gap = "" if percent + 1e-9 >= target else "  (below target)"
        print(
            f"{module:<{width}}  {percent:>7.2f}%  {required:>7.1f}%  "
            f"{target:>7.1f}%{flag or gap}"
        )
    print("-" * len(header))
    print(
        f"{'TOTAL':<{width}}  {overall:>7.2f}%  "
        f"{min(OVERALL_MINIMUM, overall_floor):>7.1f}%  {OVERALL_MINIMUM:>7.1f}%"
    )
    if not strict and floors:
        print(
            "\nRatcheted against scripts/coverage_floors.json. "
            "Coverage may rise; it may not fall."
        )

    if not failures and not overall_short:
        return 0

    print("", file=sys.stderr)
    if overall_short:
        print(
            f"integration coverage {overall:.2f}% is below "
            f"{min(OVERALL_MINIMUM, overall_floor):.1f}%",
            file=sys.stderr,
        )
    for module, percent, required, _tgt, missing in failures:
        print(
            f"{module}: {percent:.2f}% is below {required:.1f}% "
            f"(uncovered lines: {_format_lines(missing)})",
            file=sys.stderr,
        )
    print(
        "\nCoverage went down. Add tests for the lines listed above, or say "
        "in review why the floor should move.",
        file=sys.stderr,
    )
    return 1


def _format_lines(lines: list[int]) -> str:
    """Collapse a line list into ranges, so the message stays readable."""
    if not lines:
        return "none"
    ranges: list[str] = []
    start = previous = lines[0]
    for line in lines[1:]:
        if line == previous + 1:
            previous = line
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = line
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(ranges)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
