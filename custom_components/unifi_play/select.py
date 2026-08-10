"""Select platform for UniFi Play devices."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    OUTPUT_LABELS,
    OUTPUT_REVERSE,
    SOURCE_REVERSE,
    is_amp,
    source_label,
    source_labels,
)
from .coordinator import UnifiPlayCoordinator, UnifiPlayDeviceState
from .entity import UnifiPlayEntity, async_setup_platform_entities

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
    convert_fn: Callable[[str], str | int]
    amp_only: bool = False
    port_only: bool = False
    options_fn: Callable[[UnifiPlayDeviceState], list[str]] | None = None


SELECTS: tuple[UnifiPlaySelectDescription, ...] = (
    UnifiPlaySelectDescription(
        key="audio_input",
        translation_key="audio_input",
        name="Audio Input",
        icon="mdi:audio-input-stereo-minijack",
        options=[],
        options_fn=lambda s: list(source_labels(s.platform).values()),
        value_fn=lambda s: source_label(s.platform, s.source),
        set_fn="set_source",
        convert_fn=lambda v: SOURCE_REVERSE[v],
    ),
    UnifiPlaySelectDescription(
        key="audio_output",
        translation_key="audio_output",
        name="Audio Output",
        icon="mdi:audio-input-rca",
        options=list(OUTPUT_LABELS.values()),
        value_fn=lambda s: OUTPUT_LABELS.get(s.out) if s.out else None,
        set_fn="set_output",
        convert_fn=lambda v: OUTPUT_REVERSE[v],
        port_only=True,
    ),
    UnifiPlaySelectDescription(
        key="eq_preset",
        translation_key="eq_preset",
        name="EQ Preset",
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
        translation_key="sub_phase",
        name="Sub Phase",
        icon="mdi:sine-wave",
        options=list(PHASE_OPTIONS.values()),
        value_fn=lambda s: PHASE_OPTIONS.get(str(s.sub_phase)),
        set_fn="set_sub_phase",
        convert_fn=lambda v: int(PHASE_REVERSE[v]),
        amp_only=True,
    ),
    UnifiPlaySelectDescription(
        key="channels",
        translation_key="channels",
        name="Channels",
        icon="mdi:surround-sound-2-0",
        options=list(CHANNEL_OPTIONS.values()),
        value_fn=lambda s: CHANNEL_OPTIONS.get(str(s.channels)),
        set_fn="set_channels",
        convert_fn=lambda v: int(CHANNEL_REVERSE[v]),
    ),
    UnifiPlaySelectDescription(
        key="streaming_timeout",
        translation_key="streaming_timeout",
        name="Streaming Timeout",
        icon="mdi:timer-sand",
        options=list(TIMEOUT_OPTIONS.values()),
        value_fn=lambda s: TIMEOUT_OPTIONS.get(s.streaming_timeout),
        set_fn="set_streaming_timeout",
        convert_fn=lambda v: TIMEOUT_REVERSE[v],
    ),
    UnifiPlaySelectDescription(
        key="announce_chime",
        translation_key="announce_chime",
        name="Announcement Chime",
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UniFi Play select entities."""
    coordinator: UnifiPlayCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _factory(device_id: str) -> list[UnifiPlaySelect]:
        state = coordinator.data[device_id]
        return [
            UnifiPlaySelect(coordinator, device_id, desc)
            for desc in SELECTS
            if (not desc.amp_only or is_amp(state.platform))
            and (not desc.port_only or not is_amp(state.platform))
        ]

    async_setup_platform_entities(coordinator, entry, async_add_entities, _factory)


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
        value = self.entity_description.convert_fn(option)
        getattr(client, self.entity_description.set_fn)(value)
