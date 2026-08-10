"""Constants for the UniFi Play integration.

Source values and zone (``groups``) fields are documented with their verified
wire values in ``docs/api.md`` - see "Audio Source" and "Zones (groups)". Two
things there are easy to get wrong and are worth reading before touching the
maps below: ``speakers`` is the HDMI eARC input on an Audio Port (not a
speaker output), and the same label maps to a different device value per
model, so the per-platform maps must never be merged into one.
"""

DOMAIN = "unifi_play"

CONF_CONTROLLER_HOST = "controller_host"
CONF_API_KEY = "api_key"

# Connection modes. Console mode discovers devices through the console's
# Apollo REST API; direct mode discovers them with UDP probes and needs no
# console at all. Entries created before modes existed have no CONF_MODE and
# are treated as console mode.
CONF_MODE = "mode"
MODE_CONSOLE = "console"
MODE_DIRECT = "direct"
CONF_MANUAL_HOSTS = "manual_hosts"

MQTT_PORT = 8883
MQTT_KEEPALIVE = 60

TOPIC_MOBILE = "UPL-MOB"

# Platform identifiers. The console/UDP discovery report "UPL-AMP"/"UPL-PORT";
# a device identified through its own MQTT broker is known only by its topic
# root, which for Port hardware is "UPL-DEVICE" (#4).
PLATFORM_AMP = "UPL-AMP"

MODEL_NAMES = {
    "UPL-AMP": "PowerAmp",
    "UPL-PORT": "Play Audio Port",
    "UPL-DEVICE": "Play Audio Port",
}


def is_amp(platform: str) -> bool:
    """True when the device is a PowerAmp (subwoofer, HDMI eARC)."""
    return platform == PLATFORM_AMP


# The two platforms have different inputs AND different device values for the
# same jack, so these maps must never be merged: an Audio Port has an optical
# S/PDIF jack and a separate eARC port, while a PowerAmp has only eARC and
# Line In.
#
# UNVERIFIED: no PowerAmp was available when the Port values below were
# established, so the eARC value here is inherited from the original map
# rather than observed. Treat it with suspicion - the Audio Port turned out
# to report eARC as "speakers", not the "hdmi"/"spdif" assumed here, so this
# may well be "speakers" too. See #16.
#
# The label is deliberately left as "HDMI eARC" rather than renamed to match
# the Port's "eARC": renaming it would break existing automations that select
# the source by name, and there is no evidence yet that the PowerAmp's own app
# screen calls it anything different. Rename it once someone with the hardware
# confirms both the value and the app's wording.
SOURCE_LABELS_AMP = {
    "streaming": "Streaming",
    "spdif": "HDMI eARC",
    "lineIn": "Line In",
}
# Verified against a UPL-PORT on firmware 1.1.10 by setting each input in the
# Play app and reading back what the device reported. Note "speakers": that is
# the HDMI eARC input, which the app calls "eARC" - it is NOT a speaker-level
# output, and labelling it "Speakers" (as this map previously did) hid eARC
# from the source list entirely.
SOURCE_LABELS_PORT = {
    "streaming": "Streaming",
    "speakers": "eARC",
    "lineIn": "Line In",
    "spdif": "S/PDIF",
    "usb": "USB",
}

# Audio Port output routing (set_audio_src with an "out" body key; captured
# from the official app in #4).
OUTPUT_LABELS = {
    "lineOut": "Line Out",
    "spdif": "S/PDIF",
    "usb": "USB",
}
OUTPUT_REVERSE = {v: k for k, v in OUTPUT_LABELS.items()}

# A PowerAmp has been seen reporting its eARC input as "hdmi"; that
# canonicalises to the "spdif" value its own label map uses. This is
# deliberately AMP-only: an Audio Port has both jacks, so applying the alias
# there would collapse eARC and optical S/PDIF into a single entry.
SOURCE_ALIASES_AMP = {"hdmi": "spdif"}


def source_labels(platform: str) -> dict[str, str]:
    """Device source value -> display label for this platform."""
    return SOURCE_LABELS_AMP if is_amp(platform) else SOURCE_LABELS_PORT


def source_aliases(platform: str) -> dict[str, str]:
    """Device values that canonicalise to another value on this platform."""
    return SOURCE_ALIASES_AMP if is_amp(platform) else {}


def source_label(platform: str, device_source: str | None) -> str | None:
    """Display label for a device-reported source value."""
    if not device_source:
        return None
    canonical = source_aliases(platform).get(device_source, device_source)
    return source_labels(platform).get(canonical, device_source)


def source_value(platform: str, label: str) -> str | None:
    """Display label -> device source value for this platform.

    Resolved per platform rather than through one shared reverse map: "eARC"
    is "speakers" on an Audio Port but "spdif" on a PowerAmp, so a merged map
    would silently send the wrong value to one of the two.
    """
    for value, lbl in source_labels(platform).items():
        if lbl == label:
            return value
    return None


BINME_TYPE_HEADER = 0x01
BINME_TYPE_BODY = 0x02
BINME_FORMAT_JSON = 0x01

DEFAULT_SCAN_INTERVAL = 30


# Friendly names for the info event's ``service`` field: which streaming
# source is feeding the speaker. "spotify" is confirmed on the wire; the rest
# are best-effort, and unknown values fall through as-is rather than hiding.
SERVICE_LABELS = {
    "spotify": "Spotify Connect",
    "airplay": "AirPlay",
    "cast": "Chromecast",
    "soundtrack": "Soundtrack Your Brand",
}

# The wb_* fields encode broadcasting one speaker's wired input across a
# zone (the protocol calls this "wideband"; the Play app calls it a broadcast
# wired source). The wb_input values are device-side source names, and ""
# means no wired source is being broadcast — the zone is streaming.
WB_STREAMING_LABEL = "Streaming"

# "streaming" is the only source that cannot be broadcast: it is not a
# physical input. Every wired input can be, including "speakers" (eARC).
NON_BROADCAST_SOURCES = frozenset({"streaming"})


def broadcast_input_labels(platform: str) -> dict[str, str]:
    """Device value -> label for the inputs this platform can broadcast.

    An Audio Port can broadcast eARC, S/PDIF, Line In or USB; a PowerAmp only
    eARC or Line In. Derived from the platform's source map so the two never
    drift apart.
    """
    return {
        value: label
        for value, label in source_labels(platform).items()
        if value not in NON_BROADCAST_SOURCES
    }


def broadcast_input_label(platform: str, wb_input: str | None) -> str:
    """Label for a zone's current wb_input, as reported by the device."""
    if not wb_input:
        return WB_STREAMING_LABEL
    canonical = source_aliases(platform).get(wb_input, wb_input)
    return broadcast_input_labels(platform).get(canonical, wb_input)


BROADCASTING_MODE_ZONE_ONLY = "zone_only"

# "Stream broadcasting" in the Play app: which targets advertise themselves to
# streaming clients (AirPlay, Spotify Connect, Cast). Values verified against a
# zone on firmware 1.1.10 by setting each mode in the app and reading it back.
BROADCASTING_MODE_LABELS = {
    "zone_only": "Zone Only",
    "zone_devices": "Zone & Devices",
    "off": "Off",
}
BROADCASTING_MODE_REVERSE: dict[str, str] = {
    v: k for k, v in BROADCASTING_MODE_LABELS.items()
}

# Events fired on the HA event bus when zone topology changes.
# Automations can trigger on these with event: unifi_play_zone_created etc.
EVENT_ZONE_CREATED = "unifi_play_zone_created"
EVENT_ZONE_DELETED = "unifi_play_zone_deleted"
EVENT_ZONE_MEMBER_CHANGED = "unifi_play_zone_member_changed"
