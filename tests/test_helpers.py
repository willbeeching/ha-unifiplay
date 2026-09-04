"""Resolution and payload hygiene shared by the actions and the config flow.

Two of these are the reason `helpers.py` exists rather than the logic living
in `services.py`: the config flow needs the same device resolution and the
same dev_info stripping, and the one time they diverged the flow wrote a zone
the actions would have refused.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_play.const import DOMAIN
from custom_components.unifi_play.helpers import (
    APP_DEV_INFO_KEYS,
    entry_covering_macs,
    mac_normalise,
    resolve_device,
    strip_firmware_keys,
    via_device_link,
)

from .const import AMP_MAC


async def test_entry_covering_macs_names_the_loaded_entry(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """The lookup is about overlapping hardware, not about a second entry."""
    assert entry_covering_macs(hass, [AMP_MAC]) == "UniFi Play (Direct)"
    assert entry_covering_macs(hass, ["aa:bb:cc:dd:ee:ff"]) == "UniFi Play (Direct)"
    assert entry_covering_macs(hass, ["DEADBEEF0000"]) is None
    assert (
        entry_covering_macs(hass, [AMP_MAC], exclude_entry_id=setup_direct.entry_id)
        is None
    )
    assert entry_covering_macs(hass, []) is None
    assert entry_covering_macs(hass, [""]) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("aa:bb:cc:dd:ee:ff", "AABBCCDDEEFF"),
        ("AABBCCDDEEFF", "AABBCCDDEEFF"),
        ("Aa:Bb:cC:dD:Ee:fF", "AABBCCDDEEFF"),
    ],
)
def test_mac_spellings_normalise_to_one(raw: str, expected: str) -> None:
    """Every source spells them differently.

    Discovery gives colons, the console gives colons, the zone payload gives
    bare hex, and a comparison between two of them silently never matches.
    """
    assert mac_normalise(raw) == expected


async def test_a_registry_device_from_another_integration(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """Reachable from an action call: the device selector is filtered by
    integration in the UI, and nothing filters a scripted call."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=setup_direct.entry_id,
        identifiers={("some_other_integration", "whatever")},
    )
    with pytest.raises(ServiceValidationError) as err:
        resolve_device(hass, device.id)
    assert err.value.translation_key == "not_a_play_device"


async def test_a_speaker_no_coordinator_has_heard_of(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """A registry row outlives the entry that made it.

    Removing an entry and adding a different one leaves the old devices in
    place until they are cleaned up, and an automation still points at them.
    """
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=setup_direct.entry_id,
        identifiers={(DOMAIN, "FFEEDDCCBBAA")},
    )
    with pytest.raises(ServiceValidationError) as err:
        resolve_device(hass, device.id)
    assert err.value.translation_key == "no_live_device"


async def test_an_unknown_device_id(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        resolve_device(hass, "no-such-device")
    assert err.value.translation_key == "unknown_device"


async def test_a_known_speaker_resolves_to_its_coordinator(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    registry = dr.async_get(hass)
    device = next(
        d
        for d in dr.async_entries_for_config_entry(registry, setup_direct.entry_id)
        if (DOMAIN, AMP_MAC) in d.identifiers
    )
    coordinator, dev_id, state = resolve_device(hass, device.id)
    assert coordinator.data[dev_id] is state
    assert state.mac == AMP_MAC


def test_firmware_owned_keys_are_dropped_before_a_write() -> None:
    """`host` above all: a zone written with it forms on every speaker and
    plays in one room only."""
    stripped = strip_firmware_keys(
        [
            {
                "type": "UPL-AMP",
                "mac": AMP_MAC,
                "name": "A",
                "ip": "1.2.3.4",
                "color": "black",
                "host": True,
            }
        ]
    )
    assert set(stripped[0]) == APP_DEV_INFO_KEYS
    assert "host" not in stripped[0]


def test_an_unrecognised_key_is_dropped_and_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dropping it is right - the app never sends it, so writing it back is
    a guess - but a firmware that adds a field is worth knowing about, and
    the log line is the only notice anybody gets.
    """
    with caplog.at_level(logging.DEBUG, logger="custom_components.unifi_play.helpers"):
        stripped = strip_firmware_keys(
            [
                {
                    "type": "UPL-AMP",
                    "mac": AMP_MAC,
                    "name": "A",
                    "ip": "1.2.3.4",
                    "color": "black",
                    "signal": -52,
                }
            ]
        )
    assert "signal" not in stripped[0]
    assert "signal" in caplog.text


def test_via_device_link_uses_whichever_form_this_ha_understands(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """2026.9 has via_device_id; the 2025.8 floor still uses via_device."""
    identifier = (DOMAIN, AMP_MAC)
    link = via_device_link(hass, setup_direct.entry_id, identifier)
    if "via_device_id" in link:
        host = next(
            device
            for device in dr.async_entries_for_config_entry(
                dr.async_get(hass), setup_direct.entry_id
            )
            if identifier in device.identifiers
        )
        assert link == {"via_device_id": host.id}
    else:
        assert link == {"via_device": identifier}


def test_via_device_link_uses_the_registry_id_when_lookup_exists(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """The 2026.9 helper is absent on the floor, so both lanes stub it.

    Leaving this branch to the latest lane alone dropped helpers.py
    below 95% on the minimum coverage gate.
    """
    identifier = (DOMAIN, AMP_MAC)
    host = next(
        device
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), setup_direct.entry_id
        )
        if identifier in device.identifiers
    )

    def _lookup(_hass: HomeAssistant, ident: tuple[str, str], **_kwargs: object) -> str:
        assert ident == identifier
        return host.id

    with patch(
        "custom_components.unifi_play.helpers.dr.async_get_device_id_by_identifier",
        _lookup,
        create=True,
    ):
        assert via_device_link(hass, setup_direct.entry_id, identifier) == {
            "via_device_id": host.id
        }


def test_via_device_link_is_empty_when_the_lookup_raises(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """The zone still exists; it just has no parent until the host is."""

    def _raise(*_args: object, **_kwargs: object) -> str:
        raise ValueError("unknown identifier")

    with patch(
        "custom_components.unifi_play.helpers.dr.async_get_device_id_by_identifier",
        _raise,
        create=True,
    ):
        assert (
            via_device_link(hass, setup_direct.entry_id, (DOMAIN, "DEADBEEF0000")) == {}
        )
