#!/usr/bin/env python3
"""Refuse a quality-scale tracker that has drifted from this repository's pin.

``custom_components/unifi_play/quality_scale.yaml`` is a self-assessment.
Nothing in hassfest grades a custom integration, so a rule marked ``done``
with no evidence, an exemption with no reason, or a file that quietly
omits a rule we already know about is worse than no file: it reads as a
completed checklist.

Regular CI compares the tracker to ``OFFICIAL_RULES`` below, which is a
snapshot of Home Assistant's rule set (hassfest ``ALL_RULES`` and the
developer docs, retrieved 2026-09-05). The list is pinned so a PR check
does not depend on GitHub or developers.home-assistant.io being up, and
so it cannot flap when those sites change. A new official rule does
**not** fail that build on its own: both the tracker and this pin would
have to be updated first.

``--check-upstream`` fetches
https://raw.githubusercontent.com/home-assistant/core/dev/script/hassfest/quality_scale.py
and fails when the pin no longer matches hassfest. That is a scheduled
job, not pull-request CI. When it fails, add the rule here and give it
a status in the tracker.

It also fails when:

* a status is not ``done``, ``exempt`` or ``todo``;
* an exemption has no reason;
* a ``done`` rule has no supporting comment (the test or the code that
  makes it true);
* ``brands`` is ``done`` but the local brand assets are missing, the
  wrong size, or absent from the release archive.

The manifest must not claim a ``quality_scale`` tier. This repository is
a custom integration; the tracker is the assessment, not a hassfest grade.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
TRACKER = REPO / "custom_components" / "unifi_play" / "quality_scale.yaml"
MANIFEST = REPO / "custom_components" / "unifi_play" / "manifest.json"
BRAND_DIR = REPO / "custom_components" / "unifi_play" / "brand"
PACKAGE = Path("custom_components/unifi_play")

ALLOWED_STATUSES = frozenset({"done", "exempt", "todo"})

#: Official Quality Scale rules as of hassfest / developer docs 2026-09-05.
#: Compared to the tracker on every CI run. Compared to live hassfest only
#: when ``--check-upstream`` is passed.
OFFICIAL_RULES: dict[str, str] = {
    # Bronze
    "action-setup": "bronze",
    "appropriate-polling": "bronze",
    "brands": "bronze",
    "common-modules": "bronze",
    "config-flow-test-coverage": "bronze",
    "config-flow": "bronze",
    "dependency-transparency": "bronze",
    "docs-actions": "bronze",
    "docs-triggers": "bronze",
    "docs-conditions": "bronze",
    "docs-high-level-description": "bronze",
    "docs-installation-instructions": "bronze",
    "docs-removal-instructions": "bronze",
    "entity-event-setup": "bronze",
    "entity-unique-id": "bronze",
    "has-entity-name": "bronze",
    "runtime-data": "bronze",
    "test-before-configure": "bronze",
    "test-before-setup": "bronze",
    "unique-config-entry": "bronze",
    # Silver
    "action-exceptions": "silver",
    "config-entry-unloading": "silver",
    "docs-configuration-parameters": "silver",
    "docs-installation-parameters": "silver",
    "entity-unavailable": "silver",
    "integration-owner": "silver",
    "log-when-unavailable": "silver",
    "parallel-updates": "silver",
    "reauthentication-flow": "silver",
    "test-coverage": "silver",
    # Gold
    "devices": "gold",
    "diagnostics": "gold",
    "discovery-update-info": "gold",
    "discovery": "gold",
    "docs-data-update": "gold",
    "docs-examples": "gold",
    "docs-known-limitations": "gold",
    "docs-supported-devices": "gold",
    "docs-supported-functions": "gold",
    "docs-troubleshooting": "gold",
    "docs-use-cases": "gold",
    "dynamic-devices": "gold",
    "entity-category": "gold",
    "entity-device-class": "gold",
    "entity-disabled-by-default": "gold",
    "entity-translations": "gold",
    "exception-translations": "gold",
    "icon-translations": "gold",
    "reconfiguration-flow": "gold",
    "repair-issues": "gold",
    "stale-devices": "gold",
    # Platinum
    "async-dependency": "platinum",
    "inject-websession": "platinum",
    "strict-typing": "platinum",
}

ALLOWED_BRAND_FILES = frozenset(
    {
        "icon.png",
        "logo.png",
        "icon@2x.png",
        "logo@2x.png",
        "dark_icon.png",
        "dark_logo.png",
        "dark_icon@2x.png",
        "dark_logo@2x.png",
    }
)

ICON_SIZES = {"icon.png": (256, 256), "icon@2x.png": (512, 512)}
DARK_ICON_SIZES = {
    "dark_icon.png": (256, 256),
    "dark_icon@2x.png": (512, 512),
}

# https://github.com/home-assistant/brands#logo-image-requirements
# Shortest side: 128-256 (1x), 256-512 (hDPI). The maximum is preferred.
LOGO_SHORTEST = {False: (128, 256), True: (256, 512)}

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

HASSFEST_QUALITY_SCALE = (
    "https://raw.githubusercontent.com/home-assistant/core/dev/"
    "script/hassfest/quality_scale.py"
)
UPSTREAM_RULE = re.compile(
    r'Rule\("([a-z0-9-]+)",\s*ScaledQualityScaleTiers\.'
    r"(BRONZE|SILVER|GOLD|PLATINUM)"
)


def _entry(value: object) -> tuple[str, str]:
    if isinstance(value, str):
        return value, ""
    if isinstance(value, dict):
        return str(value.get("status", "")), str(value.get("comment") or "").strip()
    return "", ""


def png_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG IHDR. No imaging library required."""
    data = path.read_bytes()
    if data[:8] != PNG_SIGNATURE:
        raise ValueError(f"{path.name} is not a PNG")
    if len(data) < 24:
        raise ValueError(f"{path.name} is truncated")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def tracked_package_files() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", str(PACKAGE)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {name for name in out.split("\0") if name}


def _read_png(path: Path, failures: list[str]) -> tuple[int, int] | None:
    try:
        return png_size(path)
    except ValueError as err:
        failures.append(str(err))
        return None


def check_logo_asset(path: Path, failures: list[str]) -> tuple[int, int] | None:
    """A supplied logo must meet the brands shortest-side ranges."""
    size = _read_png(path, failures)
    if size is None:
        return None
    hdpi = "@2x" in path.name
    low, high = LOGO_SHORTEST[hdpi]
    shortest = min(size)
    if not low <= shortest <= high:
        kind = "hDPI" if hdpi else "normal"
        failures.append(
            f"{path.name} is {size[0]}x{size[1]} (shortest side {shortest}); "
            f"Home Assistant {kind} logos need the shortest side "
            f"{low}-{high}px"
        )
    return size


def check_logo_pair(base: str, failures: list[str]) -> None:
    """1x and @2x of the same logo, when both exist, must be exact doubles."""
    one = BRAND_DIR / f"{base}.png"
    two = BRAND_DIR / f"{base}@2x.png"
    if not one.is_file() and two.is_file():
        failures.append(f"{two.name} is present without {one.name}")
        check_logo_asset(two, failures)
        return
    if not one.is_file():
        return
    size_one = check_logo_asset(one, failures)
    if not two.is_file():
        return
    size_two = check_logo_asset(two, failures)
    if size_one is None or size_two is None:
        return
    expected = (size_one[0] * 2, size_one[1] * 2)
    if size_two != expected:
        failures.append(
            f"{two.name} is {size_two[0]}x{size_two[1]}, "
            f"expected {expected[0]}x{expected[1]}"
        )


def check_brands(failures: list[str]) -> None:
    """The local brand files Home Assistant 2026.3 serves from brand/."""
    if not BRAND_DIR.is_dir():
        failures.append(
            "brands is done but custom_components/unifi_play/brand/ is missing"
        )
        return
    present = {path.name for path in BRAND_DIR.iterdir() if path.is_file()}
    unexpected = present - ALLOWED_BRAND_FILES
    if unexpected:
        failures.append(
            "brand/ contains files Home Assistant will not serve: "
            + ", ".join(sorted(unexpected))
        )
    icon = BRAND_DIR / "icon.png"
    if not icon.is_file():
        failures.append(f"brands is done but {icon.relative_to(REPO)} is missing")
    for name, expected in {**ICON_SIZES, **DARK_ICON_SIZES}.items():
        path = BRAND_DIR / name
        if not path.is_file():
            continue
        size = _read_png(path, failures)
        if size is not None and size != expected:
            failures.append(
                f"{path.name} is {size[0]}x{size[1]}, Home Assistant icons are "
                f"{expected[0]}x{expected[1]}"
            )
    logo = BRAND_DIR / "logo.png"
    if not logo.is_file():
        failures.append(f"brands is done but {logo.relative_to(REPO)} is missing")
    check_logo_pair("logo", failures)
    check_logo_pair("dark_logo", failures)
    tracked = tracked_package_files()
    required_in_archive = [
        str(PACKAGE / "brand" / name)
        for name in sorted(ALLOWED_BRAND_FILES)
        if (BRAND_DIR / name).is_file()
    ]
    missing = [name for name in required_in_archive if name not in tracked]
    if missing:
        failures.append(
            "brand assets are not in git ls-files, so the release archive "
            f"would omit them: {', '.join(missing)}"
        )


def parse_upstream_rules(source: str) -> dict[str, str]:
    """Rule names and tiers from hassfest's ALL_RULES list."""
    found = {
        match.group(1): match.group(2).lower()
        for match in UPSTREAM_RULE.finditer(source)
    }
    if len(found) < 40:
        raise ValueError(
            "upstream hassfest quality_scale.py did not contain a plausible "
            f"ALL_RULES list ({len(found)} names parsed)"
        )
    return found


def fetch_upstream_rules(url: str) -> dict[str, str]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ha-unifiplay-quality-scale-check"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            source = response.read().decode("utf-8")
    except urllib.error.URLError as err:
        raise ValueError(f"could not fetch {url}: {err}") from err
    return parse_upstream_rules(source)


def check_upstream(failures: list[str], url: str) -> None:
    """The pin, not the tracker, against live hassfest."""
    try:
        upstream = fetch_upstream_rules(url)
    except ValueError as err:
        failures.append(str(err))
        return
    missing = [name for name in upstream if name not in OFFICIAL_RULES]
    extra = [name for name in OFFICIAL_RULES if name not in upstream]
    if missing:
        failures.append(
            "OFFICIAL_RULES is missing rules hassfest now publishes: "
            + ", ".join(missing)
            + ". Add them here and give each a status in quality_scale.yaml."
        )
    if extra:
        failures.append(
            "OFFICIAL_RULES has rules hassfest no longer publishes: " + ", ".join(extra)
        )
    tier = [
        f"{name}: pinned {OFFICIAL_RULES[name]}, hassfest {upstream[name]}"
        for name in OFFICIAL_RULES
        if name in upstream and OFFICIAL_RULES[name] != upstream[name]
    ]
    if tier:
        failures.append(
            "OFFICIAL_RULES tiers no longer match hassfest: " + "; ".join(tier)
        )


def check_tracker(failures: list[str]) -> dict[str, object] | None:
    if not TRACKER.is_file():
        print(f"quality scale tracker not found: {TRACKER}", file=sys.stderr)
        return None
    document = yaml.safe_load(TRACKER.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("rules"), dict):
        print(f"{TRACKER} has no rules: mapping", file=sys.stderr)
        return None
    rules = document["rules"]

    missing = [name for name in OFFICIAL_RULES if name not in rules]
    unknown = [name for name in rules if name not in OFFICIAL_RULES]
    if missing:
        failures.append(
            "official rules missing from the tracker: " + ", ".join(missing)
        )
    if unknown:
        failures.append(
            "tracker has rules the pinned official list does not: " + ", ".join(unknown)
        )

    for name, raw in rules.items():
        status, comment = _entry(raw)
        if status not in ALLOWED_STATUSES:
            failures.append(f"{name}: status {status!r} is not done, exempt, or todo")
            continue
        if status == "exempt" and not comment:
            failures.append(f"{name}: exempt with no reason")
        if status == "done" and not comment:
            failures.append(
                f"{name}: done with no supporting comment or test reference"
            )
        if name == "brands" and status == "done":
            check_brands(failures)

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if "quality_scale" in manifest:
        failures.append(
            "manifest.json claims quality_scale="
            f"{manifest['quality_scale']!r}. This is a custom integration; "
            "the tracker is the assessment. Do not put a tier in the manifest."
        )
    return rules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--check-upstream",
        action="store_true",
        help=(
            "fetch hassfest ALL_RULES and fail if OFFICIAL_RULES differs. "
            "Scheduled CI only; pull-request CI stays offline."
        ),
    )
    parser.add_argument(
        "--upstream-url",
        default=HASSFEST_QUALITY_SCALE,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    failures: list[str] = []
    rules = check_tracker(failures)
    if rules is None:
        return 2
    if args.check_upstream:
        check_upstream(failures, args.upstream_url)

    if not failures:
        print(
            f"{len(rules)} rules, "
            f"{sum(1 for raw in rules.values() if _entry(raw)[0] == 'done')} done, "
            f"{sum(1 for raw in rules.values() if _entry(raw)[0] == 'exempt')} exempt, "
            f"{sum(1 for raw in rules.values() if _entry(raw)[0] == 'todo')} todo"
        )
        return 0

    print("quality scale tracker is not honest:", file=sys.stderr)
    for item in failures:
        print(f"  {item}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
