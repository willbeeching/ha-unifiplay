#!/usr/bin/env python3
"""Refuse a quality-scale tracker that has drifted from Home Assistant.

``custom_components/unifi_play/quality_scale.yaml`` is a self-assessment.
Nothing in hassfest grades a custom integration, so a rule marked ``done``
with no evidence, an exemption with no reason, or a file that quietly
omits a rule Home Assistant added last month is worse than no file: it
reads as a completed checklist.

This checks the tracker against the official rule set published at
https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/
(retrieved 2026-09-05). The list is pinned here rather than fetched at
runtime so CI does not depend on developers.home-assistant.io being up,
and so a new official rule fails the build until someone records a
status for it.

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

import json
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
TRACKER = REPO / "custom_components" / "unifi_play" / "quality_scale.yaml"
MANIFEST = REPO / "custom_components" / "unifi_play" / "manifest.json"
BRAND_DIR = REPO / "custom_components" / "unifi_play" / "brand"
PACKAGE = Path("custom_components/unifi_play")

ALLOWED_STATUSES = frozenset({"done", "exempt", "todo"})

#: Official Quality Scale rules as of the 2026-09-05 developer docs.
#: A rule appearing here but not in the tracker, or the other way around,
#: is the file falling behind Home Assistant.
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

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


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
    for required, expected in (("icon.png", (256, 256)), ("logo.png", None)):
        path = BRAND_DIR / required
        if not path.is_file():
            failures.append(f"brands is done but {path.relative_to(REPO)} is missing")
            continue
        try:
            size = png_size(path)
        except ValueError as err:
            failures.append(str(err))
            continue
        if expected is not None and size != expected:
            failures.append(
                f"{path.name} is {size[0]}x{size[1]}, Home Assistant icons are "
                f"{expected[0]}x{expected[1]}"
            )
    icon_2x = BRAND_DIR / "icon@2x.png"
    if icon_2x.is_file():
        try:
            size = png_size(icon_2x)
        except ValueError as err:
            failures.append(str(err))
        else:
            if size != (512, 512):
                failures.append(f"icon@2x.png is {size[0]}x{size[1]}, expected 512x512")
    logo = BRAND_DIR / "logo.png"
    logo_2x = BRAND_DIR / "logo@2x.png"
    if logo.is_file() and logo_2x.is_file():
        try:
            base = png_size(logo)
            double = png_size(logo_2x)
        except ValueError as err:
            failures.append(str(err))
        else:
            if double != (base[0] * 2, base[1] * 2):
                failures.append(
                    f"logo@2x.png is {double[0]}x{double[1]}, "
                    f"expected {base[0] * 2}x{base[1] * 2}"
                )
    tracked = tracked_package_files()
    required_in_archive = [
        str(PACKAGE / "brand" / name)
        for name in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png")
        if (BRAND_DIR / name).is_file()
    ]
    missing = [name for name in required_in_archive if name not in tracked]
    if missing:
        failures.append(
            "brand assets are not in git ls-files, so the release archive "
            f"would omit them: {', '.join(missing)}"
        )


def main() -> int:
    failures: list[str] = []
    if not TRACKER.is_file():
        print(f"quality scale tracker not found: {TRACKER}", file=sys.stderr)
        return 2
    document = yaml.safe_load(TRACKER.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("rules"), dict):
        print(f"{TRACKER} has no rules: mapping", file=sys.stderr)
        return 2
    rules = document["rules"]

    missing = [name for name in OFFICIAL_RULES if name not in rules]
    unknown = [name for name in rules if name not in OFFICIAL_RULES]
    if missing:
        failures.append(
            "official rules missing from the tracker: " + ", ".join(missing)
        )
    if unknown:
        failures.append(
            "tracker has rules Home Assistant does not publish: " + ", ".join(unknown)
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
