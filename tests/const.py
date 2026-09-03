"""Shared constants and payload loaders for the UniFi Play tests.

Payloads come from ``tests/fixtures/``; see the provenance notes there for
which model and firmware each was captured from, and which parts of its
meaning were verified on hardware rather than merely observed.
"""

from __future__ import annotations

import json
from copy import deepcopy
from functools import cache
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# One PowerAmp and one Audio Port. The pair matters: the two models have
# different inputs, so a single-model test set would never catch a merged
# source map (see const.py's module docstring).
AMP_ID = "11111111-1111-4111-8111-111111111111"
AMP_MAC = "AABBCCDDEEFF"
AMP_IP = "192.168.1.100"
AMP_NAME = "Living Room"

PORT_ID = "22222222-2222-4222-8222-222222222222"
PORT_MAC = "AABBCCDDEE11"
PORT_IP = "192.168.1.101"
PORT_NAME = "Kitchen"

# A third speaker, used by the zone tests that need a member to go offline
# while the rest stay up.
THIRD_ID = "33333333-3333-4333-8333-333333333333"
THIRD_MAC = "AABBCCDDEE22"
THIRD_IP = "192.168.1.102"
THIRD_NAME = "Study"

CONSOLE_HOST = "192.168.1.1"
API_KEY = "test-api-key-not-a-real-credential"

ZONE_ID = "9c7ba639-ecf4-4c70-bc91-adf043f3e9ae"
ZONE_NAME = "Downstairs"


@cache
def _load(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def fixture(name: str) -> Any:
    """Return a captured payload, deep-copied so a test cannot mutate it.

    The cache is on the parse, not the result: several tests edit a payload
    to describe a variant, and a shared object would leak that edit into
    every later test in the session.
    """
    return deepcopy(_load(name))


def device_dict(
    *,
    device_id: str = AMP_ID,
    mac: str = AMP_MAC,
    ip: str = AMP_IP,
    name: str = AMP_NAME,
    platform: str = "UPL-AMP",
    firmware: str = "1.0.38",
) -> dict[str, Any]:
    """A discovery-shaped device dict.

    Console and direct discovery both produce this shape, which is why
    ``UnifiPlayDeviceState`` takes either interchangeably.
    """
    return {
        "id": device_id,
        "name": name,
        "mac": mac,
        "platform": platform,
        "firmware": firmware,
        "ip": ip,
    }


def amp_device() -> dict[str, Any]:
    """The PowerAmp, as discovery reports it."""
    return device_dict()


def port_device() -> dict[str, Any]:
    """The Audio Port, as discovery reports it."""
    return device_dict(
        device_id=PORT_ID,
        mac=PORT_MAC,
        ip=PORT_IP,
        name=PORT_NAME,
        platform="UPL-PORT",
        firmware="1.1.10",
    )


def third_device() -> dict[str, Any]:
    """A second Audio Port."""
    return device_dict(
        device_id=THIRD_ID,
        mac=THIRD_MAC,
        ip=THIRD_IP,
        name=THIRD_NAME,
        platform="UPL-PORT",
        firmware="1.1.10",
    )


def zone_member(
    mac: str, name: str, ip: str, *, platform: str = "UPL-PORT", host: bool = False
) -> dict[str, Any]:
    """One ``dev_info`` entry as a device reports it.

    Note ``host``: the firmware writes that flag and the integration must
    never assert it on a write. It is present here because this is the
    *read* shape.
    """
    return {
        "type": platform,
        "mac": mac,
        "name": name,
        "ip": ip,
        "color": "black",
        "host": host,
    }


def groups_body(
    *,
    group_id: str = ZONE_ID,
    name: str = ZONE_NAME,
    members: list[dict[str, Any]] | None = None,
    group_index: int = 1,
    broadcasting_mode: str = "zone_only",
    wb_enable: bool = False,
    wb_device: str = "",
    wb_input: str = "",
    extra_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A ``groups`` event body containing one zone (plus any extras).

    Defaults to the captured two-speaker zone: the PowerAmp hosting, the
    Audio Port as a member.
    """
    if members is None:
        members = [
            zone_member(AMP_MAC, AMP_NAME, AMP_IP, platform="UPL-AMP", host=True),
            zone_member(PORT_MAC, PORT_NAME, PORT_IP),
        ]
    group = {
        "group_id": group_id,
        "name": name,
        "dev_info": members,
        "dev_count": len(members),
        "group_index": group_index,
        "broadcasting_mode": broadcasting_mode,
        "wb_enable": wb_enable,
        "wb_device": wb_device,
        "wb_input": wb_input,
    }
    return {"timestamp": 1786371652, "groups": [group, *(extra_groups or [])]}


def empty_groups_body() -> dict[str, Any]:
    """A device that reports no zones at all."""
    return {"timestamp": 1786371652, "groups": []}
