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

There is no ratchet any more. While the suite was being built out this
enforced a per-module floor recorded in ``scripts/coverage_floors.json``,
which could rise but never fall; every module now clears the real targets, so
the floors file is gone and the targets are what runs. Restoring a ratchet
would mean a module could be allowed to sit below 95% again, which is the
thing it existed to stop.
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


def _module_target(module: str) -> float:
    if module in FULL_COVERAGE_MODULES:
        return 100.0
    return MODULE_MINIMUM


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
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

    rows: list[tuple[str, float, float, list[int]]] = []
    for path, data in sorted(files.items()):
        module = _module_name(path)
        if module in EXEMPT_MODULES:
            continue
        percent = float(data["summary"]["percent_covered"])
        rows.append(
            (
                module,
                percent,
                _module_target(module),
                data.get("missing_lines", []),
            )
        )

    overall = float(report["totals"]["percent_covered"])

    failures = [row for row in rows if row[1] + 1e-9 < row[2]]
    overall_short = overall + 1e-9 < OVERALL_MINIMUM

    width = max((len(row[0]) for row in rows), default=10)
    header = f"{'module':<{width}}  {'covered':>8}  {'target':>8}"
    print(header)
    print("-" * len(header))
    for module, percent, target, _missing in rows:
        flag = "" if percent + 1e-9 >= target else "  FAIL"
        print(f"{module:<{width}}  {percent:>7.2f}%  {target:>7.1f}%{flag}")
    print("-" * len(header))
    print(f"{'TOTAL':<{width}}  {overall:>7.2f}%  {OVERALL_MINIMUM:>7.1f}%")

    if not failures and not overall_short:
        return 0

    print("", file=sys.stderr)
    if overall_short:
        print(
            f"integration coverage {overall:.2f}% is below " f"{OVERALL_MINIMUM:.1f}%",
            file=sys.stderr,
        )
    for module, percent, target, missing in failures:
        print(
            f"{module}: {percent:.2f}% is below {target:.1f}% "
            f"(uncovered lines: {_format_lines(missing)})",
            file=sys.stderr,
        )
    print(
        "\nAdd tests for the lines listed above. Lowering the target is a "
        "change to argue for in review, not a way past this.",
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
