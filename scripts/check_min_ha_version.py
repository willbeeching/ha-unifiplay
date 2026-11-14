#!/usr/bin/env python3
"""Import smoke test, plus the minimum-version consistency check.

Two jobs, deliberately in one script so they cannot drift apart:

1. **Import the integration.** Every module, not just the package. A missing
   import or a syntax feature the interpreter does not have shows up here as
   a plain traceback rather than as a config entry that silently fails to set
   up in someone's Home Assistant.

2. **Check the declared floor against the release under test.** ``hacs.json``
   tells HACS the oldest Home Assistant this integration supports, and the
   floor is kept at the current stable release, which is the one release CI
   runs. If the two differ, the number is decorative: the floor nobody tests
   is the floor nobody supports. A Dependabot bump to
   ``requirements_test_latest.txt`` fails this until ``hacs.json`` moves
   with it.

Usage::

    python scripts/check_min_ha_version.py
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "custom_components.unifi_play"

#: Every module in the integration. Listed rather than globbed so that adding
#: a module without adding it here is a visible omission in review, and so a
#: stray file in the package directory cannot quietly become part of the
#: smoke test.
MODULES = (
    "",
    ".api",
    ".binary_sensor",
    ".button",
    ".config_flow",
    ".const",
    ".coordinator",
    ".diagnostics",
    ".discovery",
    ".entity",
    ".helpers",
    ".media_player",
    ".mqtt_client",
    ".number",
    ".select",
    ".sensor",
    ".services",
    ".switch",
    ".text",
    ".zone_writer",
)


def _import_all() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    for suffix in MODULES:
        name = f"{PACKAGE}{suffix}"
        importlib.import_module(name)
        print(f"  imported {name}")


def _declared_floor() -> str:
    hacs = json.loads((REPO_ROOT / "hacs.json").read_text(encoding="utf-8"))
    floor = hacs.get("homeassistant")
    if not floor:
        raise SystemExit("hacs.json does not declare a homeassistant floor")
    return str(floor)


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    import homeassistant.const as ha_const

    installed = ha_const.__version__
    print(f"Home Assistant {installed} on Python {sys.version.split()[0]}")

    print("Importing every integration module:")
    _import_all()

    floor = _declared_floor()
    if installed != floor:
        print(
            f"\nhacs.json declares a minimum of {floor}, but CI is running "
            f"{installed}.\n"
            "The floor is kept at the release CI tests. If this is a "
            "pytest-homeassistant-custom-component bump, set "
            f'"homeassistant": "{installed}" in hacs.json on the same branch.',
            file=sys.stderr,
        )
        return 1
    print(f"hacs.json floor {floor} matches the release under test")

    manifest = json.loads(
        (REPO_ROOT / "custom_components" / "unifi_play" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    print(
        f"manifest version {manifest['version']}, requirements {manifest['requirements']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
