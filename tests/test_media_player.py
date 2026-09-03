"""The per-speaker media player.

Zone players are covered in the zone tests; this file is about one speaker.
"""

from __future__ import annotations

import pytest
from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_LEVEL,
    ATTR_MEDIA_VOLUME_MUTED,
    MediaPlayerEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import entity_object
from .const import fixture
from .fake_mqtt import FakeDevice

AMP = "media_player.living_room"


async def _call(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        "media_player", service, {ATTR_ENTITY_ID: AMP, **data}, blocking=True
    )


# ── State ─────────────────────────────────────────────────────────────────


async def test_offline_speaker_reads_off(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """``online`` is a device-reported flag, separate from the MQTT session."""
    amp.emit("online", {"status": 0})
    await settle(hass)
    assert hass.states.get(AMP).state == "off"


async def test_playing_paused_and_idle(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """Paused looks exactly like playing minus the flag.

    The source is still streaming and the track is still loaded, so the only
    difference is ``stream_playing``. Anything else is idle.
    """
    amp.emit("online", {"status": 1})
    amp.emit("info", {"stream_playing": True, "source": "streaming"})
    await settle(hass)
    assert hass.states.get(AMP).state == "playing"

    amp.emit("metadata", fixture("mqtt_metadata.json"))
    amp.emit("info", {"stream_playing": False, "source": "streaming"})
    await settle(hass)
    assert hass.states.get(AMP).state == "paused"

    amp.emit("info", {"stream_playing": False, "source": "lineIn"})
    await settle(hass)
    assert hass.states.get(AMP).state == "idle"


async def test_metadata_is_surfaced(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.emit("online", {"status": 1})
    amp.emit(
        "info", {"stream_playing": True, "source": "streaming", "service": "spotify"}
    )
    amp.emit("metadata", fixture("mqtt_metadata.json"))
    await settle(hass)

    attrs = hass.states.get(AMP).attributes
    assert attrs["media_title"] == "Weightless"
    assert attrs["media_artist"] == "Marconi Union"
    assert attrs["media_album_name"] == "Ambient Transmissions Vol. 2"
    assert attrs["media_playlist"] == "Focus"
    assert attrs["app_name"] == "Spotify Connect"
    assert attrs["media_duration"] == 486
    assert attrs["media_position"] == 61
    # Home Assistant draws the playhead as position + (now - this). Without
    # it the progress bar freezes at whatever arrived last.
    assert attrs["media_position_updated_at"] is not None


async def test_skip_buttons_follow_the_source(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """The official app greys its buttons out on the same flags (#4)."""
    features = hass.states.get(AMP).attributes["supported_features"]
    assert not features & MediaPlayerEntityFeature.NEXT_TRACK

    amp.emit("metadata", {"prev": True, "next": True})
    await settle(hass)

    features = hass.states.get(AMP).attributes["supported_features"]
    assert features & MediaPlayerEntityFeature.NEXT_TRACK
    assert features & MediaPlayerEntityFeature.PREVIOUS_TRACK


async def test_cover_art_hash_changes_with_the_track(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """The speaker serves artwork from a fixed path and swaps the file.

    Home Assistant's default hashes the URL, which never changes, so the
    frontend kept serving the first track's image forever.
    """
    amp.emit("online", {"status": 1})
    amp.emit("metadata", fixture("mqtt_metadata.json"))
    await settle(hass)
    first = hass.states.get(AMP).attributes["entity_picture"]

    amp.emit("metadata", {"title": "Something else", "cover_path": "cover/current.jpg"})
    await settle(hass)
    second = hass.states.get(AMP).attributes["entity_picture"]

    assert first and second and first != second


async def test_no_cover_path_means_no_picture(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.emit("online", {"status": 1})
    amp.emit("metadata", {"title": "No art"})
    await settle(hass)
    assert "entity_picture" not in hass.states.get(AMP).attributes


async def test_an_absolute_cover_url_is_left_alone(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """Some services hand the speaker a URL rather than a local path.

    Prefixing the speaker's address to it produces a 404 on the speaker and
    no artwork at all.
    """
    amp.emit("online", {"status": 1})
    amp.emit("metadata", {"cover_path": "https://art.example/cover.jpg"})
    await settle(hass)

    entity = entity_object(hass, AMP)
    assert entity.media_image_url == "https://art.example/cover.jpg"


async def test_no_address_means_no_cover_url(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """A speaker identified over MQTT alone has no address to fetch from."""
    amp.emit("metadata", {"cover_path": "cover/current.jpg"})
    await settle(hass)

    entity = entity_object(hass, AMP)
    entity._device_state.ip = ""
    assert entity.media_image_url is None
    assert await entity.async_get_media_image() == (None, None)


async def test_cover_art_is_fetched_by_home_assistant(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    settle,
    aioclient_mock,
) -> None:
    """The speaker's certificate is self-signed, so the browser cannot.

    Home Assistant proxies it instead, which is the only reason the artwork
    appears at all.
    """
    amp.emit("metadata", {"cover_path": "cover/current.jpg"})
    await settle(hass)

    entity = entity_object(hass, AMP)
    aioclient_mock.get(
        entity.media_image_url,
        content=b"\x89PNG-not-really",
        headers={"Content-Type": "image/png"},
    )
    assert await entity.async_get_media_image() == (b"\x89PNG-not-really", "image/png")


async def test_a_cover_the_speaker_will_not_serve(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    settle,
    aioclient_mock,
) -> None:
    """The path is reported before the file is written often enough to
    matter, and a 404 must not become an exception in the frontend."""
    amp.emit("metadata", {"cover_path": "cover/current.jpg"})
    await settle(hass)

    entity = entity_object(hass, AMP)
    aioclient_mock.get(entity.media_image_url, status=404)
    assert await entity.async_get_media_image() == (None, None)


async def test_a_speaker_that_stops_answering_mid_fetch(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    settle,
    aioclient_mock,
) -> None:
    import aiohttp

    amp.emit("metadata", {"cover_path": "cover/current.jpg"})
    await settle(hass)

    entity = entity_object(hass, AMP)
    aioclient_mock.get(
        entity.media_image_url, exc=aiohttp.ClientConnectionError("gone")
    )
    assert await entity.async_get_media_image() == (None, None)


async def test_zone_membership_attributes(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """``hosting_group`` and friends appear only while true.

    A device leaving a zone simply stops sending them, which is why the
    attributes are omitted rather than reported false.
    """
    assert "hosting_group" not in hass.states.get(AMP).attributes

    amp.emit("info", {"hosting_group": "zone-1", "wb_broadcasting": True})
    await settle(hass)

    attrs = hass.states.get(AMP).attributes
    assert attrs["hosting_group"] == "zone-1"
    assert attrs["wideband_broadcasting"] is True
    assert "zone_synced" not in attrs

    amp.emit("info", {"sync_devices": ["AABBCCDDEE11"]})
    await settle(hass)
    assert hass.states.get(AMP).attributes["zone_synced"] is True


# ── Commands ──────────────────────────────────────────────────────────────


async def test_volume_set(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    await _call(hass, "volume_set", **{ATTR_MEDIA_VOLUME_LEVEL: 0.42})
    assert amp.last_action("set_volume").body["volume"] == 42


async def test_volume_up_respects_the_device_limit(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """The speaker enforces its own limit; stepping past it is pointless."""
    amp.emit("info", {"volume": 68, "vol_limit": 70})
    await settle(hass)
    amp.clear()

    await _call(hass, "volume_up")
    assert amp.last_action("set_volume").body["volume"] == 70


async def test_volume_down_stops_at_zero(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.emit("info", {"volume": 3})
    await settle(hass)
    amp.clear()

    await _call(hass, "volume_down")
    assert amp.last_action("set_volume").body["volume"] == 0


async def test_mute_remembers_the_level_to_restore(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """Mute is software-only: the client maps it to volume 0.

    Reading the restore level at unmute time is too late - the volume is
    already zero by then, which is why it always restored the fallback.
    """
    amp.emit("online", {"status": 1})
    amp.emit("info", {"volume": 55})
    await settle(hass)
    amp.clear()

    await _call(hass, "volume_mute", **{ATTR_MEDIA_VOLUME_MUTED: True})
    assert amp.last_action("set_volume").body["volume"] == 0
    assert hass.states.get(AMP).attributes["is_volume_muted"] is True

    await _call(hass, "volume_mute", **{ATTR_MEDIA_VOLUME_MUTED: False})
    assert amp.last_action("set_volume").body["volume"] == 55


async def test_an_in_flight_info_event_does_not_cancel_a_mute(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """An info event sent before set_volume(0) landed still carries the old level.

    Treating that as "volume rose above zero" would drop the mute flag the
    moment it was set.
    """
    amp.emit("online", {"status": 1})
    amp.emit("info", {"volume": 40})
    await settle(hass)
    await _call(hass, "volume_mute", **{ATTR_MEDIA_VOLUME_MUTED: True})

    amp.emit("info", {"volume": 40})  # still in flight
    await settle(hass)
    assert hass.states.get(AMP).attributes["is_volume_muted"] is True

    amp.emit("info", {"volume": 0})  # the device has caught up
    await settle(hass)
    assert hass.states.get(AMP).attributes["is_volume_muted"] is True

    amp.emit("info", {"volume": 25})  # someone turned the dial
    await settle(hass)
    assert hass.states.get(AMP).attributes["is_volume_muted"] is False


async def test_the_device_never_reports_our_mute(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    """Only a positive ``muted`` is trusted.

    Our mute is software, which the device reports as ``muted: false``;
    honouring that would stomp the flag the moment it was set.
    """
    amp.emit("online", {"status": 1})
    await settle(hass)
    await _call(hass, "volume_mute", **{ATTR_MEDIA_VOLUME_MUTED: True})
    amp.emit("info", {"muted": False})
    await settle(hass)
    assert hass.states.get(AMP).attributes["is_volume_muted"] is True

    amp.emit("info", {"muted": True})
    await settle(hass)
    assert hass.states.get(AMP).attributes["is_volume_muted"] is True


@pytest.mark.parametrize(
    ("service", "action"),
    [
        ("media_play", "play"),
        ("media_pause", "pause"),
    ],
)
async def test_transport_commands(
    hass: HomeAssistant,
    setup_direct: MockConfigEntry,
    amp: FakeDevice,
    service: str,
    action: str,
) -> None:
    amp.clear()
    await _call(hass, service)
    assert amp.last_action("set_player").body == {"action": action}


async def test_skip_commands_once_the_source_allows_them(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice, settle
) -> None:
    amp.emit("metadata", {"prev": True, "next": True})
    await settle(hass)
    amp.clear()

    await _call(hass, "media_next_track")
    assert amp.last_action("set_player").body == {"action": "next"}
    await _call(hass, "media_previous_track")
    assert amp.last_action("set_player").body == {"action": "prev"}


async def test_select_source_on_an_amp(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    await _call(hass, "select_source", source="Line In")
    assert amp.last_action("set_audio_src").body == {"source": "lineIn"}


async def test_selecting_a_source_the_model_lacks_publishes_nothing(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    """A PowerAmp has no optical jack, so the label resolves to no value.

    Home Assistant does not validate ``source`` against ``source_list``, so
    the entity has to. What must not happen is a value being invented: the
    amp accepts ``spdif`` and echoes it back while routing nothing, which is
    exactly how selecting eARC once reported success and passed no audio.
    """
    amp.clear()
    await _call(hass, "select_source", source="S/PDIF")
    assert amp.published_actions("set_audio_src") == []


async def test_turn_off_stops_playback(
    hass: HomeAssistant, setup_direct: MockConfigEntry, amp: FakeDevice
) -> None:
    amp.clear()
    await _call(hass, "turn_off")
    assert amp.last_action("stop").body == {}
