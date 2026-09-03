"""Downloadable diagnostics.

What a diagnostics file is for here: the failures this integration produces
are almost all invisible from the outside. A speaker that accepted no
certificate, a zone the speakers disagree about, a console that answers with
its own error page — none of them look like anything except entities that do
not update. Everything below exists so a bug report can carry the answer
instead of a description.

**Nothing identifying leaves the house.** Diagnostics are pasted into public
issue trackers. So: no API key, no certificate or key material, no raw MQTT
payloads (they carry track titles, announcement filenames and speaker names
the user chose), and every address, MAC and hostname is replaced. Home
Assistant's own redaction helper handles the config entry; the rest is built
by naming what goes in, not by filtering what comes out — a denylist stops
working the day a new field is added, and the field that gets added is always
the interesting one.
"""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_API_KEY, CONF_CONTROLLER_HOST, MODEL_NAMES, source_label
from .coordinator import (
    UnifiPlayConfigEntry,
    UnifiPlayCoordinator,
    UnifiPlayDeviceState,
    UnifiPlayGroupState,
)
from .mqtt_client import bundled_generations

TO_REDACT = {CONF_API_KEY, CONF_CONTROLLER_HOST, "manual_hosts"}


def _anonymise(value: str) -> str:
    """A stable stand-in for an identifier, unique within one report.

    Hashed rather than dropped: a report has to be able to say "these three
    zones all name the same speaker" without saying which speaker. Truncated
    to eight characters because it is a label, not a checksum, and salted per
    report is unnecessary — the values are MACs and IPs, and pre-imaging a
    private address from a truncated digest tells an attacker nothing they
    could not guess.
    """
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _device_diagnostics(
    coordinator: UnifiPlayCoordinator, device_id: str, state: UnifiPlayDeviceState
) -> dict[str, Any]:
    """One speaker, with its identity replaced and its behaviour kept."""
    client = coordinator.get_mqtt_client(device_id)
    return {
        "id": _anonymise(device_id),
        "mac": _anonymise(state.mac),
        "address": _anonymise(state.ip),
        # The model matters and is not identifying: half the protocol
        # differences in this integration are between the two models.
        "platform": state.platform,
        "model": MODEL_NAMES.get(state.platform, state.platform),
        "firmware": state.firmware,
        "has_name": bool(state.device_name),
        "online": state.online,
        "mqtt": {
            "client_registered": client is not None,
            "connected": bool(client is not None and client.is_connected),
            # The machine key, not the sentence: it is what the code branches
            # on, and it does not change with a translation.
            "offline_reason": coordinator.mqtt_offline_reason(device_id),
        },
        "audio": {
            "volume": state.volume,
            "volume_limit": state.vol_limit,
            "muted": state.muted,
            "source": state.source,
            "source_label": source_label(state.platform, state.source),
            "output": state.out,
            "streaming": state.stream_playing,
            # Which service, not what is playing: "spotify" is a capability
            # report, a track title is the user's evening.
            "service": state.service,
            "channels": state.channels,
            "balance": state.balance,
            "loudness": state.loudness,
            "subwoofer": state.subwoofer,
        },
        "equaliser": {
            "enabled": state.eq_enable,
            "profile": state.eq_preset,
            "bands_reported": len(state.eq_table),
            "custom_presets": len(state.eq_custom_presets),
        },
        "features": {
            "alarms": len(state.alarms),
            "quiet_hours": len(state.quiet_hours),
            "announcement_files": len(state.ann_files),
            "voice_enhancement": state.voice_enhancement,
        },
        "zone_membership": {
            # These three are sent only while true, which is exactly the
            # trap that keeps catching people, so a report says whether the
            # speaker is currently asserting them.
            "hosting_group": bool(state.hosting_group),
            "sync_devices": state.sync_devices,
            "wb_broadcasting": state.wb_broadcasting,
        },
        "link_quality": state.link_quality,
        "uptime": state.uptime,
    }


def _zone_diagnostics(
    coordinator: UnifiPlayCoordinator, gs: UnifiPlayGroupState
) -> dict[str, Any]:
    return {
        "id": _anonymise(gs.group_id),
        "has_name": bool(gs.name),
        "member_count": len(gs.dev_info),
        "declared_dev_count": gs.dev_count,
        "members": sorted(_anonymise(entry.get("mac", "")) for entry in gs.dev_info),
        "host": _anonymise(gs.host_mac),
        "host_elected": bool(gs.host_mac),
        "group_index": gs.group_index,
        "broadcasting_mode": gs.broadcasting_mode,
        "broadcast": {
            "enabled": gs.wb_enable,
            "device": _anonymise(gs.wb_device),
            "input": gs.wb_input,
        },
        # The write path can only run when every required speaker is up, so a
        # report that says which ones are missing answers "why did my edit
        # refuse" without another round trip.
        "required_speakers_connected": all(
            coordinator.get_mqtt_client_for_mac(mac) is not None
            for mac in coordinator.zones.required_macs(
                gs.group_id,
                [entry["mac"] for entry in gs.dev_info if entry.get("mac")],
            )
        ),
    }


def _zone_agreement(coordinator: UnifiPlayCoordinator) -> dict[str, Any]:
    """Whether the speakers are telling the same story about each zone.

    A zone the speakers disagree about behaves like a zone that keeps
    reverting, and there is nothing in the UI that says so.
    """
    return {
        _anonymise(group_id): count
        for group_id, count in coordinator.zone_copy_counts().items()
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: UnifiPlayConfigEntry
) -> dict[str, Any]:
    """Diagnostics for one config entry."""
    coordinator = entry.runtime_data

    return {
        "entry": {
            # async_redact_data replaces the API key and the addresses; the
            # rest of entry.data is the connection mode, which is the first
            # thing anyone reading a report needs.
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "mode": entry.data.get("mode", "console"),
            "version": entry.version,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "device_count": len(coordinator.data),
            "zone_count": len(coordinator.groups),
            "zones_fully_synced": coordinator.zones_fully_synced,
            "uses_console_api": coordinator.api is not None,
            "manual_host_count": len(coordinator.manual_hosts),
        },
        "certificates": {
            # Which generations are bundled and present, never their
            # contents. Firmware 1.0.41 rotated the CA and cut every device
            # off from the certificate this integration had used since 2023,
            # so "which are available" is the first question of any
            # connection report (#20).
            "bundled": [generation.name for generation in bundled_generations()],
        },
        "devices": [
            _device_diagnostics(coordinator, device_id, state)
            for device_id, state in sorted(coordinator.data.items())
        ],
        "zones": [
            _zone_diagnostics(coordinator, gs)
            for gs in sorted(coordinator.groups.values(), key=lambda z: z.group_id)
        ],
        "zone_copies_reported": _zone_agreement(coordinator),
    }
