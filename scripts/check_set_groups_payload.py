#!/usr/bin/env python3
"""Verify a written zone never carries firmware-owned keys.

Why this exists
---------------
v1.3.0-v1.3.5 created zones that every device agreed on - correct membership,
correct dev_count - but that only ever sounded on the host. The member stayed
silent over AirPlay and over Spotify Connect alike, so it was not a streaming
problem: the zone never carried audio to the member at all.

The cause was one key. ``set_groups`` writes built ``dev_info`` entries with
``"host": true`` on the chosen device. ``host`` is the firmware's output, not
the writer's input - the device elects a host after the write and echoes the
flag back. Asserting it up front suppressed whatever the firmware does on
election, and the member never joined. Capturing the Play app's own
``set_groups`` write off a device's MQTT broker settled it: the app does not
send the key at all. ``"host": false`` is not the fix either.

The same capture showed the per-group ``timestamp`` we sent is never echoed
back, so it is write-only noise.

Neither failure is loud. The zone forms, the entities look right, and the only
symptom is silence in one room - which is exactly the class of bug the other
guards in this directory exist for. So: assert statically that the payload
builder for a *written* zone emits neither key.

Not covered here
----------------
``gs_to_dict`` deliberately DOES echo ``host``. It re-serialises zones the
devices reported, for the sibling entries the write path resends untouched
alongside the zone being edited; stripping their elected host would force a
re-election on an unrelated - and possibly playing - zone every time the user
edits a different one.
"""

from __future__ import annotations

import ast
import pathlib
import sys

FORBIDDEN = {"host", "timestamp"}
TARGET = "group_payload"
PKG = (
    pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "unifi_play"
)


def _dict_keys(node: ast.AST) -> set[str]:
    """Literal string keys of every dict literal under node."""
    keys: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict):
            keys |= {
                k.value
                for k in sub.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
        elif isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant):
            if isinstance(sub.slice.value, str):
                keys.add(sub.slice.value)
    return keys


def main() -> int:
    helpers = PKG / "helpers.py"
    try:
        tree = ast.parse(helpers.read_text(), filename=str(helpers))
    except SyntaxError as err:
        print(f"needs a newer Python to parse the package ({err})")
        return 0

    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == TARGET
        ),
        None,
    )
    if fn is None:
        print(f"error: {TARGET}() not found in {helpers.name}")
        return 1

    offending = sorted(_dict_keys(fn) & FORBIDDEN)
    if offending:
        print(f"error: {TARGET}() emits firmware-owned key(s): {offending}")
        print("       'host' is elected by the device; writing it silences members.")
        print("       a per-group 'timestamp' is never echoed back.")
        print(f"       see {helpers.name}:dev_info_entry and docs/api.md")
        return 1

    print(f"ok: {TARGET}() emits no firmware-owned keys")
    return 0


if __name__ == "__main__":
    sys.exit(main())
