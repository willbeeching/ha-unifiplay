"""Entity naming, categorisation, default enablement and icon translations.

Two of these are static checks over the source and the translation files.
They are here rather than in a lint rule because what they assert is a
product decision — no English in Python, every key translated — and a product
decision that only a reviewer enforces is one that drifts.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

PKG = pathlib.Path(__file__).parent.parent / "custom_components" / "unifi_play"
PLATFORMS = (
    "binary_sensor",
    "button",
    "media_player",
    "number",
    "select",
    "sensor",
    "switch",
    "text",
)


def _strings() -> dict:
    return json.loads((PKG / "strings.json").read_text(encoding="utf-8"))


def _translations() -> dict:
    return json.loads((PKG / "translations" / "en.json").read_text(encoding="utf-8"))


# ── No English left in Python ─────────────────────────────────────────────


@pytest.mark.parametrize("platform", PLATFORMS)
def test_no_entity_is_named_in_python(platform: str) -> None:
    """A name= or _attr_name beside a translation_key silently wins over it.

    Which means the translation exists, looks correct in review, and is never
    shown to anybody.
    """
    source = (PKG / f"{platform}.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s+name=\"", source, re.MULTILINE), platform
    for match in re.finditer(r"^\s+_attr_name = (.+)$", source, re.MULTILINE):
        # None is the exception: it means "this entity is the device", which
        # is a structural statement, not a name.
        assert match.group(1).strip() == "None", (platform, match.group(0))


def test_every_translation_key_has_a_name() -> None:
    """A key with no entry renders as the raw key in the UI.

    Ten actions once shipped exactly like that.
    """
    entity = _strings()["entity"]
    for platform in PLATFORMS:
        source = (PKG / f"{platform}.py").read_text(encoding="utf-8")
        keys = set(re.findall(r'translation_key="([a-z_0-9]+)"', source))
        keys |= set(re.findall(r'_attr_translation_key = "([a-z_0-9]+)"', source))
        # Action and zone error keys live under `exceptions`, not `entity`.
        keys -= set(_strings()["exceptions"])
        for key in keys:
            assert key in entity.get(platform, {}), (platform, key)
            assert entity[platform][key].get("name"), (platform, key)


def test_strings_and_the_english_translation_agree() -> None:
    """They are separate files and drift silently.

    strings.json is what the developer edits; translations/en.json is what
    Home Assistant actually loads, so a change to one alone shows nothing in
    review and nothing in the UI either.
    """
    assert _strings() == _translations()


def test_every_exception_key_used_in_code_is_translated() -> None:
    """An untranslated key renders as `unifi_play.some_key` in a red toast."""
    exceptions = set(_strings()["exceptions"])
    used: set[str] = set()
    for path in PKG.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r'translation_key="([a-z_0-9]+)"[^)]*?\)', source, re.DOTALL
        ):
            used.add(match.group(1))
    entity_keys = {
        key for platform in _strings()["entity"].values() for key in platform
    }
    options_errors = set(_strings()["options"]["error"])
    # Repair issues have their own block; they are raised through the issue
    # registry rather than as an exception, but the key still has to resolve.
    issues = set(_strings()["issues"])
    unaccounted = used - exceptions - entity_keys - options_errors - issues
    assert not unaccounted, unaccounted


# ── Categories ────────────────────────────────────────────────────────────


async def test_settings_are_config_entities(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """A balance slider is not something the speaker is doing.

    Leaving them uncategorised buries the two or three entities that describe
    what is actually happening under twenty that describe how it is set up.
    """
    registry = er.async_get(hass)
    for entity_id in (
        "number.living_room_balance",
        "number.living_room_volume_limit",
        "number.living_room_screen_brightness",
        "number.living_room_led_brightness",
        "select.living_room_channels",
        "select.living_room_eq_preset",
        "switch.living_room_dynamic_boost",
        "switch.living_room_persistent_dashboard",
        "text.living_room_led_color",
        "button.living_room_reset_eq",
    ):
        entry = registry.async_get(entity_id)
        assert entry is not None, entity_id
        assert entry.entity_category is EntityCategory.CONFIG, entity_id


async def test_technical_status_is_diagnostic(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    for entity_id in (
        "binary_sensor.living_room_connected",
        "binary_sensor.living_room_admin_lock",
        "sensor.living_room_firmware_status",
    ):
        entry = registry.async_get(entity_id)
        assert entry is not None, entity_id
        assert entry.entity_category is EntityCategory.DIAGNOSTIC, entity_id


async def test_what_the_speaker_is_doing_has_no_category(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """A category hides an entity from the main card.

    These are the ones that belong on it.
    """
    registry = er.async_get(hass)
    for entity_id in (
        "media_player.living_room",
        "binary_sensor.living_room_announcing",
        "binary_sensor.living_room_in_zone",
        "sensor.living_room_streaming_service",
        "select.living_room_audio_input",
    ):
        entry = registry.async_get(entity_id)
        assert entry is not None, entity_id
        assert entry.entity_category is None, entity_id


# ── Default enablement ────────────────────────────────────────────────────


async def test_the_noisy_entities_are_off_by_default(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """Ten EQ sliders per speaker is a wall, and almost nobody moves them.

    They are registered so they can be switched on, and disabled so they are
    not in the way of everyone who will not.
    """
    registry = er.async_get(hass)
    for entity_id in (
        "number.living_room_eq_32hz",
        "number.living_room_eq_16k",
        "sensor.living_room_uptime",
        "sensor.living_room_space",
    ):
        entry = registry.async_get(entity_id)
        assert entry is not None, entity_id
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION, entity_id
        assert hass.states.get(entity_id) is None


async def test_an_entity_the_user_enabled_stays_enabled(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    settle,
) -> None:
    """Turning one on is a decision, and a reload must not undo it.

    Home Assistant only consults the default when it first registers the
    entity; this asserts nothing in the integration overrides that.
    """
    registry = er.async_get(hass)
    registry.async_update_entity("number.living_room_eq_32hz", disabled_by=None)

    assert await hass.config_entries.async_reload(setup_direct.entry_id)
    await settle(hass)

    entry = registry.async_get("number.living_room_eq_32hz")
    assert entry is not None
    assert entry.disabled_by is None


# ── Icons ─────────────────────────────────────────────────────────────────


def test_icon_translations_name_real_entities() -> None:
    """An icon translation for a key that does not exist is silently ignored."""
    icons = json.loads((PKG / "icons.json").read_text(encoding="utf-8"))
    entity = _strings()["entity"]
    for platform, keys in icons["entity"].items():
        for key, spec in keys.items():
            assert key in entity[platform], (platform, key)
            assert spec["default"].startswith("mdi:")
            for state_icon in spec.get("state", {}).values():
                assert state_icon.startswith("mdi:")


def test_a_state_driven_icon_is_not_also_hard_coded() -> None:
    """A static _attr_icon wins over the translation, so the translation
    would be dead weight that looks like configuration."""
    icons = json.loads((PKG / "icons.json").read_text(encoding="utf-8"))
    for platform, keys in icons["entity"].items():
        source = (PKG / f"{platform}.py").read_text(encoding="utf-8")
        for key in keys:
            block = re.search(
                rf'_attr_translation_key = "{key}"(.*?)(?=\n\n|\nclass )',
                source,
                re.DOTALL,
            )
            if block is None:
                continue
            assert "_attr_icon" not in block.group(1), (platform, key)


async def test_the_connectivity_icon_follows_the_connection(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp, settle
) -> None:
    """The one place a state-dependent icon earns its place: it is the entity
    that stays visible when everything else goes unavailable."""
    icons = json.loads((PKG / "icons.json").read_text(encoding="utf-8"))
    spec = icons["entity"]["binary_sensor"]["connectivity"]
    assert spec["default"] != spec["state"]["off"]
