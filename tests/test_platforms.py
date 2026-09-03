"""Entity platforms, driven through Home Assistant's own service calls.

Nothing here reaches into an entity object. A test that calls
``entity.async_press()`` directly proves the method runs; calling
``button.press`` through the service registry proves the entity is
registered, addressable, available, and wired to the transport - which is
the part that has actually broken.
"""

from __future__ import annotations

import pytest
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import pump
from .const import fixture
from .fake_mqtt import FakeDevice

AMP = "living_room"
PORT = "kitchen"


async def _call(
    hass: HomeAssistant, domain: str, service: str, entity_id: str, **data
) -> None:
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )


# ── Availability ──────────────────────────────────────────────────────────


async def test_entities_are_unavailable_without_mqtt(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """Everything an entity reports arrives over MQTT.

    Without a connection the state shown would be whatever the device state
    was initialised with - plausible defaults that are not the speaker's
    (#15).
    """
    assert hass.states.get(f"switch.{AMP}_dynamic_boost").state != STATE_UNAVAILABLE

    amp.drop()
    await settle(hass)

    assert hass.states.get(f"switch.{AMP}_dynamic_boost").state == STATE_UNAVAILABLE
    assert hass.states.get(f"number.{AMP}_balance").state == STATE_UNAVAILABLE


async def test_connectivity_sensor_stays_visible_when_mqtt_drops(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """The one entity that must not go unavailable with the connection.

    The 1.0.41 certificate outage looked like a pile of broken entities
    rather than a speaker that was offline, because nothing stayed up to
    say so (#20).
    """
    connected = f"binary_sensor.{AMP}_connected"
    assert hass.states.get(connected).state == "on"

    amp.drop()
    await settle(hass)

    state = hass.states.get(connected)
    assert state.state == "off"
    assert state.attributes["reason"] == "Connection dropped"


async def test_a_command_to_a_disconnected_speaker_reaches_nothing(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """A speaker that has dropped takes its entities down with it.

    Home Assistant refuses the call before the entity is reached, and the
    entity's own ``_require_mqtt`` is the second line of defence for the
    window where availability is momentarily stale (see test_entity.py).
    What must never happen is the command being reported as delivered while
    the socket is down (#14).
    """
    amp.drop()
    await settle(hass)
    amp.clear()

    assert hass.states.get(f"button.{AMP}_locate").state == STATE_UNAVAILABLE
    await _call(hass, "button", "press", f"button.{AMP}_locate")
    assert amp.published_actions("locate") == []


# ── Buttons ───────────────────────────────────────────────────────────────


async def test_locate_and_restart(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    await _call(hass, "button", "press", f"button.{AMP}_locate")
    assert amp.last_action("locate").body == {"enable": True}

    await _call(hass, "button", "press", f"button.{AMP}_restart")
    assert amp.last_action("reboot").body == {}


async def test_eq_reset_flattens_the_bands_the_device_reported(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """Reset is a table of zeroes; the device has no reset action of its own."""
    amp.emit("equalizer", fixture("mqtt_equalizer.json"))
    await settle(hass)
    amp.clear()

    await _call(hass, "button", "press", f"button.{AMP}_reset_eq")

    table = amp.last_action("set_equalizer").body["table"]
    assert set(table) == {
        "32",
        "64",
        "125",
        "250",
        "500",
        "1k",
        "2k",
        "4k",
        "8k",
        "16k",
    }
    assert set(table.values()) == {0.0}


async def test_eq_reset_before_the_device_has_reported_a_table(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    """Falls back to the device's own band labels rather than doing nothing."""
    amp.clear()
    await _call(hass, "button", "press", f"button.{AMP}_reset_eq")
    assert len(amp.last_action("set_equalizer").body["table"]) == 10


# ── Switches ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("entity", "action", "key"),
    [
        ("dynamic_boost", "set_loudness", "loudness"),
        ("dolby_atmos_equalizer", "set_eq_enable", "enable"),
        ("persistent_dashboard", "set_persistent_dashboard", "enable"),
        ("voice_enhancement", "set_voice_enhancement", "enable"),
    ],
)
async def test_switches_publish_both_ways(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    entity: str,
    action: str,
    key: str,
) -> None:
    amp.clear()
    await _call(hass, "switch", "turn_on", f"switch.{AMP}_{entity}")
    assert amp.last_action(action).body[key] is True

    await _call(hass, "switch", "turn_off", f"switch.{AMP}_{entity}")
    assert amp.last_action(action).body[key] is False


async def test_switch_state_follows_the_device(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.emit("info", fixture("mqtt_info_amp.json"))
    await settle(hass)
    # The captured amp payload has loudness on and eq_enable on.
    assert hass.states.get(f"switch.{AMP}_dynamic_boost").state == "on"

    amp.emit("info", {"loudness": False})
    await settle(hass)
    assert hass.states.get(f"switch.{AMP}_dynamic_boost").state == "off"


async def test_alarm_test_switch(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    """The only fire-something-now primitive the device offers."""
    amp.clear()
    await _call(hass, "switch", "turn_on", f"switch.{AMP}_alarm_sound_test")
    body = amp.last_action("alarm_test").body
    assert body["on"] is True
    assert body["sound"]

    await _call(hass, "switch", "turn_off", f"switch.{AMP}_alarm_sound_test")
    assert amp.last_action("alarm_test").body == {"on": False}


# ── Numbers ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("entity", "action", "key", "value"),
    [
        ("balance", "set_balance", "balance", -40),
        ("volume_limit", "set_vol_limit", "percentage", 70),
        ("screen_brightness", "set_screen_brightness", "value", 55),
        ("led_brightness", "set_led_brightness", "value", 20),
        ("announcement_volume", "set_announcement_vol", "value", 80),
    ],
)
async def test_numbers_publish(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    entity: str,
    action: str,
    key: str,
    value: int,
) -> None:
    amp.clear()
    await _call(hass, "number", "set_value", f"number.{AMP}_{entity}", value=value)
    assert amp.last_action(action).body[key] == value


async def test_subwoofer_numbers_appear_only_once_the_amp_reports_one(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """Optional hardware is only knowable over MQTT, well after discovery.

    Checking inside the normal factory would suppress these on every device,
    including the ones that do have a subwoofer, because the flag is still
    at its default when discovery runs.
    """
    assert hass.states.get(f"number.{AMP}_sub_crossover") is None

    amp.emit("info", fixture("mqtt_info_amp.json"))  # subwoofer: true
    await settle(hass)

    assert hass.states.get(f"number.{AMP}_sub_crossover") is not None
    amp.clear()
    await _call(hass, "number", "set_value", f"number.{AMP}_sub_crossover", value=120)
    assert amp.last_action("set_sub_audio").body["crossover"] == 120


async def test_eq_band_numbers_write_the_whole_table(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    settle,
) -> None:
    """The app never sends a single band; the whole table goes every time.

    The individual bands are disabled by default - ten sliders per speaker
    is noise for most installs - so this test enables them.
    """
    amp.emit("equalizer", fixture("mqtt_equalizer.json"))
    await settle(hass)
    amp.clear()

    await _call(hass, "number", "set_value", f"number.{AMP}_eq_125hz", value=6.0)

    body = amp.last_action("set_equalizer").body
    assert body["profile"] == "custom"
    assert body["table"]["125"] == 6.0
    assert len(body["table"]) == 10


# ── Selects ───────────────────────────────────────────────────────────────


async def test_audio_input_options_differ_by_model(
    hass: HomeAssistant, setup_direct: MockConfigEntry
) -> None:
    """A PowerAmp has no optical or USB jack; a Port has both."""
    amp_options = hass.states.get(f"select.{AMP}_audio_input").attributes["options"]
    port_options = hass.states.get(f"select.{PORT}_audio_input").attributes["options"]
    assert amp_options == ["Streaming", "eARC", "Line In"]
    assert port_options == ["Streaming", "eARC", "Line In", "S/PDIF", "USB"]


async def test_selecting_earc_sends_speakers_on_both_models(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    port: FakeDevice,
) -> None:
    """The bug this repository has shipped twice, from both directions."""
    for device, entity in ((amp, AMP), (port, PORT)):
        device.clear()
        await _call(
            hass,
            "select",
            "select_option",
            f"select.{entity}_audio_input",
            option="eARC",
        )
        assert device.last_action("set_audio_src").body == {"source": "speakers"}


async def test_audio_output_is_port_only(
    hass: HomeAssistant, setup_direct: MockConfigEntry, port: FakeDevice
) -> None:
    """Output routing is an Audio Port feature; an amp has no such entity."""
    assert hass.states.get(f"select.{AMP}_audio_output") is None
    port.clear()
    await _call(
        hass,
        "select",
        "select_option",
        f"select.{PORT}_audio_output",
        option="S/PDIF",
    )
    assert port.last_action("set_audio_src").body == {"out": "spdif"}


async def test_eq_preset_select_offers_saved_presets(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.emit("equalizer", fixture("mqtt_equalizer.json"))
    await settle(hass)

    options = hass.states.get(f"select.{AMP}_eq_preset").attributes["options"]
    assert "Late night" in options

    amp.clear()
    await _call(
        hass,
        "select",
        "select_option",
        f"select.{AMP}_eq_preset",
        option="Late night",
    )
    assert amp.last_action("set_equalizer").body["active_preset"] == "Late night"


async def test_channels_select(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    await _call(
        hass, "select", "select_option", f"select.{AMP}_channels", option="Mono"
    )
    assert amp.last_action("set_channels").body == {"value": 1}


# ── Sensors ───────────────────────────────────────────────────────────────


async def test_sensors_report_device_state(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    settle,
) -> None:
    amp.emit("info", fixture("mqtt_info_amp.json"))
    amp.emit("alarms", [{"alarm_id": "a"}, {"alarm_id": "b"}])
    amp.emit("extra_info", fixture("mqtt_extra_info_port.json"))
    await settle(hass)

    assert hass.states.get(f"sensor.{AMP}_firmware_status").state == "latest"
    assert hass.states.get(f"sensor.{AMP}_alarms").state == "2"
    assert hass.states.get(f"sensor.{AMP}_uptime").state == "864000"


async def test_streaming_service_label(
    hass: HomeAssistant, setup_direct: MockConfigEntry, port: FakeDevice, settle
) -> None:
    port.emit("info", fixture("mqtt_info_port.json"))
    await settle(hass)
    assert (
        hass.states.get(f"sensor.{PORT}_streaming_service").state == "Spotify Connect"
    )


async def test_an_unknown_streaming_service_is_shown_raw(
    hass: HomeAssistant, setup_direct: MockConfigEntry, port: FakeDevice, settle
) -> None:
    """Hiding it would bury the one clue about a value nobody has seen."""
    port.emit("info", {"service": "tidalconnect"})
    await settle(hass)
    assert hass.states.get(f"sensor.{PORT}_streaming_service").state == "tidalconnect"


# ── Binary sensors ────────────────────────────────────────────────────────


async def test_admin_lock_reports_unlocked_as_on(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """device_class LOCK: on means unlocked."""
    amp.emit("info", {"locked": False})
    await settle(hass)
    assert hass.states.get(f"binary_sensor.{AMP}_admin_lock").state == "on"

    amp.emit("info", {"locked": True})
    await settle(hass)
    assert hass.states.get(f"binary_sensor.{AMP}_admin_lock").state == "off"


async def test_announcing_sensor(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """``announcing`` is sent only while true and must clear on absence."""
    amp.emit("info", {"announcing": True, "announcing_type": "manual"})
    await settle(hass)
    assert hass.states.get(f"binary_sensor.{AMP}_announcing").state == "on"

    amp.emit("info", {"volume": 20})
    await settle(hass)
    assert hass.states.get(f"binary_sensor.{AMP}_announcing").state == "off"


# ── Text ──────────────────────────────────────────────────────────────────


async def test_led_colour_accepts_hex_with_or_without_hash(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    await _call(hass, "text", "set_value", f"text.{AMP}_led_color", value="ff8800")
    body = amp.last_action("set_color").body
    assert body["led"] == "FF8800"
    assert body["screen"] == "FF8800"


async def test_led_colour_rejects_a_non_hex_value(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    """Home Assistant's own pattern rejects it before the entity is reached."""
    amp.clear()
    with pytest.raises(ValueError):
        await _call(hass, "text", "set_value", f"text.{AMP}_led_color", value="ZZZZZZ")
    assert amp.published_actions("set_color") == []


async def test_a_new_speaker_gets_entities_without_a_reload(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    mqtt_network,
    discovered_devices,
    settle,
) -> None:
    """A speaker adopted after setup appears on the next discovery pass."""
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.unifi_play.coordinator import DISCOVERY_INTERVAL

    from .const import THIRD_IP, THIRD_MAC, third_device
    from .fake_mqtt import FakeDevice as _FakeDevice

    mqtt_network.add(
        _FakeDevice(ip=THIRD_IP, mac=THIRD_MAC, platform="UPL-PORT", name="Study")
    )
    discovered_devices.append(third_device())

    async_fire_time_changed(hass, dt_util.utcnow() + DISCOVERY_INTERVAL)
    await settle(hass)
    await pump()
    await hass.async_block_till_done()

    assert hass.states.get("media_player.study") is not None
