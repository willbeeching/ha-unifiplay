"""Constants for the UniFi Play integration.

Source values and zone (``groups``) fields are documented with their verified
wire values in ``docs/api.md`` - see "Audio Source" and "Zones (groups)". Two
things there are easy to get wrong and are worth reading before touching the
maps below.

``speakers`` is the HDMI eARC input - on both the Audio Port and the PowerAmp
- and not a speaker output. Its plausible-looking name has now caused the same
bug twice: labelled "Speakers" it hid eARC from the Port entirely, and on the
amp its absence left eARC pointing at ``spdif``, which the firmware accepts
and does nothing with.

The per-platform maps must never be merged. Not because a label maps to
different values per model (it does not - eARC is ``speakers`` on both) but
because the models have different inputs: a Port has optical S/PDIF and USB
jacks the amp lacks, and the amp accepts ``spdif`` without having anywhere to
route it, so a merged map would offer inputs that silently do nothing.
"""

import re

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

# Whether to verify the console's TLS certificate.
#
# A UniFi OS console presents a certificate signed by Ubiquiti's own CA for a
# name the user is not connecting by; connecting to it at its LAN address
# therefore fails verification on a stock setup. It succeeds when the console
# is reached by a name that carries a certificate the machine trusts - a
# reverse proxy, or a hostname with a real certificate installed on the
# console - which is why this is a choice and not a constant.
#
# Absent from an entry created before this existed, and those entries were
# all set up with verification off, so False is the compatible default. New
# entries are offered True first: an unverified connection to something
# holding an API key is worth one extra click to accept knowingly.
CONF_VERIFY_SSL = "verify_ssl"

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
# Verified against a UPL-AMP on firmware 1.0.38 by publishing each candidate
# with set_audio_src and reading back the device's own reported source. eARC
# is "speakers", exactly as on the Port - the "spdif" assumed here previously
# was never observed and never had a jack behind it. See #16.
#
# The amp ALSO accepts and echoes back "spdif", but a PowerAmp has no optical
# input and the Play app offers only three: Streaming, eARC, Line In. So
# "spdif" is shared firmware accepting a value that routes nothing, and it is
# deliberately absent here: offering it would put a fourth input in the UI
# that silently does nothing. That is precisely what the old map did by
# labelling it "HDMI eARC" - selecting eARC on an amp reported success and
# passed no audio.
#
# Renamed to "eARC" to match both the app's own wording and the Port's label.
# No working automation is broken by this: the string it replaces pointed at
# the dead value, so anything selecting "HDMI eARC" was already a no-op. The
# two platforms must agree, because a mixed-model zone offers the union of its
# speakers' inputs and would otherwise list eARC twice under two names.
SOURCE_LABELS_AMP = {
    "streaming": "Streaming",
    "speakers": "eARC",
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

# A PowerAmp has been reported as describing its eARC input as "hdmi", so the
# read path tolerates it and canonicalises to "speakers", the value the amp
# was measured to use. Nothing here is load-bearing: probing an amp on 1.0.38
# with "hdmi" (and "earc", "eArc", "arc", "hdmiIn") left the source unchanged,
# so the device neither accepts nor emits it, and this only avoids showing a
# raw value should some other firmware do so.
#
# Deliberately AMP-only: an Audio Port has both jacks, so applying the alias
# there would collapse eARC and optical S/PDIF into a single entry.
SOURCE_ALIASES_AMP = {"hdmi": "speakers"}


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


# Firmware version strings, in the two shapes the hardware reports.
#
# The build string is dotted end to end -
# "UPL-AMP.qcs405.v1.0.38.37ed30f.260312.07:19:19" - and the commit hash that
# follows the version starts with a digit often enough that an unanchored
# "digits and dots" match swallows part of it: that exact string yields
# "1.0.38.37", and the Port's yields "1.1.10.9". Requiring a dot on both
# sides of the version is what stops it, because the character after the
# real version is always the start of a hash.
_FIRMWARE_BUILD_RE = re.compile(r"\.v(\d+(?:\.\d+)+)\.")
#: Apollo's own ``firmware`` field is already just the version.
_FIRMWARE_PLAIN_RE = re.compile(r"^v?(\d+(?:\.\d+)+)$")


def parse_firmware_version(raw: str) -> str:
    """Extract the version from whatever shape the device reported.

    Returns ``raw`` unchanged when neither shape matches: an unrecognised
    string is worth showing, because it is the only clue about a format
    nobody has seen.
    """
    if not raw:
        return ""
    for pattern in (_FIRMWARE_BUILD_RE, _FIRMWARE_PLAIN_RE):
        match = pattern.search(raw)
        if match:
            return match.group(1)
    return raw


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
# One event per logical change, whatever number of speakers report it: the
# coordinator diffs its canonical zone view, not each device's copy. Nothing
# fires while a speaker is doing its first sync after a start or reload -
# those zones existed before Home Assistant connected, and an automation
# cannot tell a startup burst from a real one.
EVENT_ZONE_CREATED = "unifi_play_zone_created"
EVENT_ZONE_DELETED = "unifi_play_zone_deleted"
EVENT_ZONE_RENAMED = "unifi_play_zone_renamed"
EVENT_ZONE_MEMBER_CHANGED = "unifi_play_zone_member_changed"

#: Delays (seconds) after a set_groups write at which zones are re-read, to
#: learn the host the firmware elects. The device never pushes a groups event
#: to announce the election, so a listener that does not ask keeps a hostless
#: copy. Spread out because election timing is not specified anywhere and has
#: been observed to take a while on some firmware.
HOST_ELECTION_REREAD_DELAYS = (3, 10, 30)

#: Unconfirmed HA zone documents remembered so a delayed echo of an
#: earlier write is not treated as a Play-app edit. Deduplicated, oldest
#: evicted first. Without a cap, a speaker that keeps serving an old
#: document (or goes silent) plus an automation that keeps writing would
#: grow the list without limit, and each write restarts the re-read
#: series so expiry never runs.
MAX_OUTSTANDING_WRITES = 8

#: Seconds after the last host-election re-read to drop an unconfirmed
#: write if the speakers have not reported it. The re-read series
#: restarts on every mutation, so this only fires once writes stop.
PENDING_WRITE_EXPIRE_GRACE = 5
