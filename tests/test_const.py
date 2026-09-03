"""Source maps and label resolution.

``const.py`` is small, but it is where the same bug has now shipped twice, in
both directions: ``speakers`` is the HDMI eARC input on both models, and the
per-platform maps must never be merged. These tests exist to make the next
"simplification" fail loudly rather than silently route audio nowhere.
"""

from __future__ import annotations

import pytest

from custom_components.unifi_play.const import (
    MODEL_NAMES,
    NON_BROADCAST_SOURCES,
    SOURCE_LABELS_AMP,
    SOURCE_LABELS_PORT,
    broadcast_input_label,
    broadcast_input_labels,
    is_amp,
    source_aliases,
    source_label,
    source_labels,
    source_value,
)


def test_earc_is_speakers_on_both_models() -> None:
    """eARC is ``speakers`` on the Port *and* the amp.

    Verified on a UPL-AMP (fw 1.0.38) by publishing each candidate with
    set_audio_src and reading the device's own reported source back, and on a
    UPL-PORT (fw 1.1.10) from the Play app. The amp also accepts ``spdif``
    and echoes it back while routing nothing, which is how "HDMI eARC" once
    pointed at a jack the hardware does not have (#16).
    """
    assert SOURCE_LABELS_AMP["speakers"] == "eARC"
    assert SOURCE_LABELS_PORT["speakers"] == "eARC"
    assert source_value("UPL-AMP", "eARC") == "speakers"
    assert source_value("UPL-PORT", "eARC") == "speakers"


def test_amp_does_not_offer_spdif() -> None:
    """A PowerAmp has no optical jack, so it must not be offered one.

    The firmware accepts ``spdif`` and echoes it back, so a merged map looks
    correct in every read-back and passes no audio.
    """
    assert "spdif" not in SOURCE_LABELS_AMP
    assert "usb" not in SOURCE_LABELS_AMP
    assert source_value("UPL-AMP", "S/PDIF") is None
    assert source_value("UPL-AMP", "USB") is None


def test_port_offers_the_jacks_it_has() -> None:
    assert set(SOURCE_LABELS_PORT) == {
        "streaming",
        "speakers",
        "lineIn",
        "spdif",
        "usb",
    }


def test_source_maps_are_not_the_same_object() -> None:
    """The two maps must stay separate: the models have different inputs."""
    assert SOURCE_LABELS_AMP is not SOURCE_LABELS_PORT
    assert SOURCE_LABELS_AMP != SOURCE_LABELS_PORT


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("UPL-AMP", True), ("UPL-PORT", False), ("UPL-DEVICE", False), ("", False)],
)
def test_is_amp(platform: str, expected: bool) -> None:
    assert is_amp(platform) is expected


def test_source_labels_falls_back_to_port_for_unknown_platform() -> None:
    """An unidentified device is treated as a Port.

    A device first seen over MQTT alone is known only by its topic root, and
    Port hardware is the one that publishes under a root the platform map
    does not name.
    """
    assert source_labels("UPL-DEVICE") == SOURCE_LABELS_PORT
    assert source_labels("") == SOURCE_LABELS_PORT


def test_hdmi_alias_is_amp_only() -> None:
    """``hdmi`` canonicalises to ``speakers`` on an amp, and nowhere else.

    A Port has both an eARC input and an optical jack, so applying the alias
    there would collapse two distinct inputs into one entry.
    """
    assert source_aliases("UPL-AMP") == {"hdmi": "speakers"}
    assert source_aliases("UPL-PORT") == {}
    assert source_label("UPL-AMP", "hdmi") == "eARC"
    assert source_label("UPL-PORT", "hdmi") == "hdmi"


def test_unknown_source_value_shows_itself() -> None:
    """An unrecognised value is shown raw rather than hidden.

    Firmware can report something this integration has never seen; showing
    it is how the next protocol value gets discovered.
    """
    assert source_label("UPL-AMP", "quantumJack") == "quantumJack"


def test_source_label_of_nothing_is_nothing() -> None:
    assert source_label("UPL-AMP", "") is None
    assert source_label("UPL-AMP", None) is None


def test_streaming_cannot_be_broadcast() -> None:
    """Only a physical input can be broadcast across a zone."""
    assert frozenset({"streaming"}) == NON_BROADCAST_SOURCES
    assert "streaming" not in broadcast_input_labels("UPL-AMP")
    assert "streaming" not in broadcast_input_labels("UPL-PORT")


def test_broadcast_inputs_follow_the_platform() -> None:
    """An amp can broadcast eARC or Line In; a Port those plus S/PDIF and USB."""
    assert set(broadcast_input_labels("UPL-AMP")) == {"speakers", "lineIn"}
    assert set(broadcast_input_labels("UPL-PORT")) == {
        "speakers",
        "lineIn",
        "spdif",
        "usb",
    }


def test_empty_wb_input_reads_as_streaming() -> None:
    """No wired source being broadcast means the zone is streaming."""
    assert broadcast_input_label("UPL-PORT", "") == "Streaming"
    assert broadcast_input_label("UPL-PORT", None) == "Streaming"
    assert broadcast_input_label("UPL-PORT", "spdif") == "S/PDIF"
    assert broadcast_input_label("UPL-AMP", "hdmi") == "eARC"
    assert broadcast_input_label("UPL-AMP", "somethingNew") == "somethingNew"


def test_model_names_cover_the_mqtt_only_topic_root() -> None:
    """Port hardware identified over MQTT alone publishes under UPL-DEVICE (#4)."""
    assert MODEL_NAMES["UPL-DEVICE"] == MODEL_NAMES["UPL-PORT"]
    assert MODEL_NAMES["UPL-AMP"] == "PowerAmp"


# ── Firmware version strings ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Captured from a UPL-AMP on firmware 1.0.38 and a UPL-PORT on 1.1.10.
        # The commit hash that follows the version starts with a digit in both,
        # which an unanchored "digits and dots" match swallows part of: those
        # two strings used to yield "1.0.38.37" and "1.1.10.9".
        ("UPL-AMP.qcs405.v1.0.38.37ed30f.260312.07:19:19", "1.0.38"),
        ("UPL-PORT.qcs405.v1.1.10.9f3ac21.260401.09:12:44", "1.1.10"),
        # A hash that happens to start with a letter never showed the bug.
        ("UPL-AMP.qcs405.v1.0.41.abc1234.260401.09:12:44", "1.0.41"),
        # Apollo's own firmware field is already just the version.
        ("1.0.38", "1.0.38"),
        ("v2.0.1", "2.0.1"),
        # Nothing recognisable: hand it back rather than hiding it. An
        # unfamiliar format is the only clue about a shape nobody has seen.
        ("unreleased-build", "unreleased-build"),
        ("", ""),
    ],
)
def test_parse_firmware_version(raw: str, expected: str) -> None:
    from custom_components.unifi_play.const import parse_firmware_version

    assert parse_firmware_version(raw) == expected
