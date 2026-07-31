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

TOPIC_AMP = "UPL-AMP"
TOPIC_DEVICE = "UPL-DEVICE"
TOPIC_MOBILE = "UPL-MOB"

BINME_TYPE_HEADER = 0x01
BINME_TYPE_BODY = 0x02
BINME_FORMAT_JSON = 0x01

DEFAULT_SCAN_INTERVAL = 30
