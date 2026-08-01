"""Constants for the UniFi Play integration."""

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


# The device value "spdif" is the HDMI eARC jack on a PowerAmp and the
# optical S/PDIF input on an Audio Port; label it per platform.
SOURCE_LABELS_AMP = {
    "streaming": "Streaming",
    "spdif": "HDMI eARC",
    "lineIn": "Line In",
}
SOURCE_LABELS_PORT = {
    "streaming": "Streaming",
    "spdif": "S/PDIF",
    "lineIn": "Line In",
}
SOURCE_ALIASES = {"hdmi": "spdif"}
# Any label from either platform maps back to its device value.
SOURCE_REVERSE = {
    label: value
    for labels in (SOURCE_LABELS_AMP, SOURCE_LABELS_PORT)
    for value, label in labels.items()
}


def source_labels(platform: str) -> dict[str, str]:
    """Device source value -> display label for this platform."""
    return SOURCE_LABELS_AMP if is_amp(platform) else SOURCE_LABELS_PORT


def source_label(platform: str, device_source: str | None) -> str | None:
    """Display label for a device-reported source value."""
    if not device_source:
        return None
    canonical = SOURCE_ALIASES.get(device_source, device_source)
    return source_labels(platform).get(canonical, device_source)


BINME_TYPE_HEADER = 0x01
BINME_TYPE_BODY = 0x02
BINME_FORMAT_JSON = 0x01

DEFAULT_SCAN_INTERVAL = 30
