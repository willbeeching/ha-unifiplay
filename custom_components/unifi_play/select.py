"""Select platform for UniFi Play devices."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BROADCASTING_MODE_LABELS,
    BROADCASTING_MODE_REVERSE,
    DOMAIN,
    OUTPUT_LABELS,
    OUTPUT_REVERSE,
    is_amp,
    source_label,
    source_labels,
    source_value,
)
from .coordinator import (
    UnifiPlayConfigEntry,
    UnifiPlayCoordinator,
    UnifiPlayDeviceState,
    UnifiPlayGroupState,
)
from .entity import (
    UnifiPlayEntity,
    async_setup_optional_entities,
    async_setup_platform_entities,
)

_LOGGER = logging.getLogger(__name__)

PHASE_OPTIONS = {"0": "0\u00b0", "180": "180\u00b0"}
PHASE_REVERSE = {v: k for k, v in PHASE_OPTIONS.items()}

CHANNEL_OPTIONS = {"0": "Stereo", "1": "Mono"}
CHANNEL_REVERSE = {v: k for k, v in CHANNEL_OPTIONS.items()}

EQ_PRESET_OPTIONS = ["Custom", "Music", "Movie", "Night", "Off"]

# Streaming timeout: how long the device stays in streaming mode with no
# audio before switching back to the previous input. Seconds on the wire;
# 0 is what the app calls "Default". Options mirror the official app.
TIMEOUT_OPTIONS = {
    0: "Default",
    10: "10 Seconds",
    30: "30 Seconds",
    60: "1 Minute",
    180: "3 Minutes",
    300: "5 Minutes",
    600: "10 Minutes",
}
TIMEOUT_REVERSE = {v: k for k, v in TIMEOUT_OPTIONS.items()}

# Announcement chimes. The device never advertises the list, so these are the
# names observed in the official app; whatever the device currently reports is
# merged in at runtime so an unlisted chime can never make the entity invalid.
CHIME_OPTIONS = [
    "Ascending Steps",
    "Chimes",
    "Hopscotch",
    "Quick Steps",
    "Vibraphone",
]


@dataclass(frozen=True, kw_only=True)
class UnifiPlaySelectDescription(SelectEntityDescription):
    """Describes a UniFi Play select entity."""

    value_fn: Callable[[UnifiPlayDeviceState], str | None]
    set_fn: str
    # None when convert_state_fn is set instead - the label needs the device
    # to resolve, so there is no meaningful label-only conversion.
    convert_fn: Callable[[str], str | int] | None = None
    amp_only: bool = False
    # Optional hardware: created only once the device reports a
    # subwoofer attached, not merely because the model has a sub
    # output. See #17.
    requires_sub: bool = False
    port_only: bool = False
    options_fn: Callable[[UnifiPlayDeviceState], list[str]] | None = None
    # Set instead of convert_fn when the label -> device value mapping depends
    # on the hardware. eARC is "speakers" on both models, but the input sets
    # differ - a Port has S/PDIF and USB jacks the amp lacks - so the source
    # select cannot use one platform-blind map.
    convert_state_fn: Callable[[UnifiPlayDeviceState, str], str | int] | None = None


SELECTS: tuple[UnifiPlaySelectDescription, ...] = (
    UnifiPlaySelectDescription(
        key="audio_input",
        translation_key="audio_input",
        icon="mdi:audio-input-stereo-minijack",
        options=[],
        options_fn=lambda s: list(source_labels(s.platform).values()),
        value_fn=lambda s: source_label(s.platform, s.source),
        set_fn="set_source",
        convert_state_fn=lambda s, v: source_value(s.platform, v) or v,
    ),
    UnifiPlaySelectDescription(
        key="audio_output",
        entity_category=EntityCategory.CONFIG,
        translation_key="audio_output",
        icon="mdi:audio-input-rca",
        options=list(OUTPUT_LABELS.values()),
        value_fn=lambda s: OUTPUT_LABELS.get(s.out) if s.out else None,
        set_fn="set_output",
        convert_fn=lambda v: OUTPUT_REVERSE[v],
        port_only=True,
    ),
    UnifiPlaySelectDescription(
        key="eq_preset",
        entity_category=EntityCategory.CONFIG,
        translation_key="eq_preset",
        icon="mdi:equalizer",
        options=EQ_PRESET_OPTIONS,
        # Built-in profiles plus whatever custom presets the device holds.
        options_fn=lambda s: EQ_PRESET_OPTIONS
        + [
            p["name"]
            for p in s.eq_custom_presets
            if isinstance(p, dict) and p.get("name")
        ],
        # A loaded custom preset wins: the profile stays "custom" for those,
        # so reporting the profile alone would hide which preset is active.
        value_fn=lambda s: (
            s.eq_active_preset or (s.eq_preset.capitalize() if s.eq_preset else None)
        ),
        # Built-ins are lower-cased profile names; anything else is a saved
        # preset recalled through a different field entirely, so the entity
        # dispatches on the value rather than the description.
        set_fn="set_eq_preset",
        convert_fn=lambda v: v.lower(),
    ),
    UnifiPlaySelectDescription(
        key="sub_phase",
        entity_category=EntityCategory.CONFIG,
        translation_key="sub_phase",
        icon="mdi:sine-wave",
        options=list(PHASE_OPTIONS.values()),
        value_fn=lambda s: PHASE_OPTIONS.get(str(s.sub_phase)),
        set_fn="set_sub_phase",
        convert_fn=lambda v: int(PHASE_REVERSE[v]),
        amp_only=True,
        requires_sub=True,
    ),
    UnifiPlaySelectDescription(
        key="channels",
        entity_category=EntityCategory.CONFIG,
        translation_key="channels",
        icon="mdi:surround-sound-2-0",
        options=list(CHANNEL_OPTIONS.values()),
        value_fn=lambda s: CHANNEL_OPTIONS.get(str(s.channels)),
        set_fn="set_channels",
        convert_fn=lambda v: int(CHANNEL_REVERSE[v]),
    ),
    UnifiPlaySelectDescription(
        key="streaming_timeout",
        entity_category=EntityCategory.CONFIG,
        translation_key="streaming_timeout",
        icon="mdi:timer-sand",
        options=list(TIMEOUT_OPTIONS.values()),
        value_fn=lambda s: TIMEOUT_OPTIONS.get(s.streaming_timeout),
        set_fn="set_streaming_timeout",
        convert_fn=lambda v: TIMEOUT_REVERSE[v],
    ),
    UnifiPlaySelectDescription(
        key="announce_chime",
        entity_category=EntityCategory.CONFIG,
        translation_key="announce_chime",
        icon="mdi:bell-ring",
        options=CHIME_OPTIONS,
        options_fn=lambda s: sorted(
            set(CHIME_OPTIONS) | ({s.ann_chime} if s.ann_chime else set())
        ),
        value_fn=lambda s: s.ann_chime or None,
        set_fn="set_announce_chime",
        convert_fn=lambda v: v,
    ),
)


# Every command is a fire-and-forget MQTT publish to a device on the LAN:
# nothing here blocks, nothing rate-limits, and the coordinator's own poll is
# the only thing that fetches. Serialising would only add latency to a
# "turn everything down" script.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UnifiPlayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play select entities."""
    coordinator = entry.runtime_data

    def _factory(device_id: str) -> list[UnifiPlaySelect]:
        state = coordinator.data[device_id]
        return [
            UnifiPlaySelect(coordinator, device_id, desc)
            for desc in SELECTS
            if (not desc.amp_only or is_amp(state.platform))
            and (not desc.port_only or not is_amp(state.platform))
            and not desc.requires_sub
        ]

    async_setup_platform_entities(coordinator, entry, async_add_entities, _factory)

    def _sub_factory(device_id: str) -> list[UnifiPlaySelect]:
        state = coordinator.data[device_id]
        return [
            UnifiPlaySelect(coordinator, device_id, desc)
            for desc in SELECTS
            if desc.requires_sub and (not desc.amp_only or is_amp(state.platform))
        ]

    async_setup_optional_entities(
        coordinator,
        entry,
        async_add_entities,
        _sub_factory,
        lambda s: s.subwoofer,
    )

    # Dynamic zone-level selects — one per zone, created and cleaned up as
    # zones appear and disappear, mirroring the pattern in binary_sensor.py.
    # Tracks which zones already have a select. Only the keys matter; the
    # entities are owned by HA once added.
    known_zone_selects: set[str] = set()

    @callback
    def _sync_zone_selects() -> None:
        active = set(coordinator.groups)

        # Drop tracking entries for zones that are gone. The entities
        # themselves go when _sync_zones removes the zone device.
        for gid in list(known_zone_selects):
            if gid not in active:
                known_zone_selects.discard(gid)

        new_ids = [gid for gid in active if gid not in known_zone_selects]
        if not new_ids:
            return
        new_entities = [
            UnifiPlayZoneBroadcastingSelect(coordinator, gid) for gid in new_ids
        ]
        for e in new_entities:
            known_zone_selects.add(e.zone_group_id)
        async_add_entities(new_entities)

    _sync_zone_selects()
    entry.async_on_unload(coordinator.async_add_listener(_sync_zone_selects))


class UnifiPlayZoneBroadcastingSelect(
    CoordinatorEntity[UnifiPlayCoordinator], SelectEntity
):
    """Stream broadcasting mode for a zone.

    Controls which targets advertise themselves to streaming clients: the zone
    only, the zone plus each speaker individually, or nothing at all.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "zone_broadcasting"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:cast-variant"
    _attr_options = list(BROADCASTING_MODE_LABELS.values())

    def __init__(self, coordinator: UnifiPlayCoordinator, group_id: str) -> None:
        super().__init__(coordinator)
        self._group_id = group_id
        self._attr_unique_id = f"unifi_play_zone_{group_id}_broadcasting_mode"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"zone_{group_id}")},
        )

    @property
    def zone_group_id(self) -> str:
        return self._group_id

    @property
    def _group(self) -> UnifiPlayGroupState | None:
        return self.coordinator.groups.get(self._group_id)

    @property
    def available(self) -> bool:
        return self._group is not None

    @property
    def current_option(self) -> str | None:
        gs = self._group
        if gs is None:
            return None
        # A mode this integration does not know is returned as-is, which HA
        # renders as "unknown" because SelectEntity.state is @final and drops
        # any current_option that is not in options. That is the intended
        # outcome: a firmware that adds a mode should read as unknown rather
        # than be silently reported as one of the three we do know.
        return BROADCASTING_MODE_LABELS.get(
            gs.broadcasting_mode, gs.broadcasting_mode or None
        )

    async def async_select_option(self, option: str) -> None:
        """Change how the zone advertises itself to streaming clients.

        Only the advertising mode moves: no physical input is touched, so
        nothing here publishes set_audio_src, which would switch a real
        input as a side effect. Every other field of the zone is preserved
        by the write path, which replaces the whole document on each write.
        """
        gs = self._group
        if gs is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="zone_not_found"
            )
        mode = BROADCASTING_MODE_REVERSE.get(option)
        if mode is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_option",
                translation_placeholders={"option": option},
            )
        self.coordinator.zones.set_broadcasting_mode(gs.group_id, mode)


class UnifiPlaySelect(UnifiPlayEntity, SelectEntity):
    """A select entity for a UniFi Play device setting."""

    entity_description: UnifiPlaySelectDescription

    def __init__(
        self,
        coordinator: UnifiPlayCoordinator,
        device_id: str,
        description: UnifiPlaySelectDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"unifi_play_{self._device_state.mac}_{description.key}"

    @property
    def options(self) -> list[str]:
        if self.entity_description.options_fn is not None:
            return self.entity_description.options_fn(self._device_state)
        return list(self.entity_description.options or [])

    @property
    def current_option(self) -> str | None:
        return self.entity_description.value_fn(self._device_state)

    async def async_select_option(self, option: str) -> None:
        client = self._require_mqtt()
        # Saved EQ presets are recalled by name through active_preset, which
        # is a different action shape from selecting a built-in profile.
        if (
            self.entity_description.key == "eq_preset"
            and option not in EQ_PRESET_OPTIONS
        ):
            client.apply_eq_preset(option)
            return
        desc = self.entity_description
        if desc.convert_state_fn is not None:
            value = desc.convert_state_fn(self._device_state, option)
        elif desc.convert_fn is not None:
            value = desc.convert_fn(option)
        else:
            value = option
        getattr(client, desc.set_fn)(value)
