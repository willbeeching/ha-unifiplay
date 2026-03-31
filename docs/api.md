# UniFi Play / Apollo API Reference

Reverse-engineered from the UniFi Play Android app v2.0.0 and live device testing.

## Architecture

```
┌─────────────┐   REST (HTTPS)    ┌──────────────┐
│  HA / Client ├──────────────────►│   UDM Pro     │
│              │  X-API-KEY header │ /proxy/apollo │
└──────┬───────┘                   └──────────────┘
       │
       │  MQTT over TLS (port 8883)
       │  mTLS with bundled client cert
       ▼
┌──────────────┐
│  PowerAmp    │
│  (UPL-AMP)   │
└──────────────┘
```

Two communication channels:

| Channel | Purpose | Auth |
|---------|---------|------|
| **REST API** (`/proxy/apollo/api/v1/`) | Device listing, metadata, adoption | `X-API-KEY` header |
| **MQTT** (port 8883 on device) | Real-time state, control commands | mTLS client certificate |

## REST API (via UDM Pro)

Base URL: `https://{udm_ip}/proxy/apollo/api/v1/`

### Authentication

Header: `X-API-KEY: {api_key}`

The API key is generated in UniFi OS Settings.

### Endpoints

#### GET /devices

Lists all known Play devices.

```json
{
  "err": null,
  "type": "collection",
  "data": [
    {
      "id": "<device-uuid>",
      "name": "My PowerAmp",
      "mac": "AABBCCDDEEFF",
      "platform": "UPL-AMP",
      "sys_id": "aa03",
      "guid": "<guid>",
      "firmware": "1.0.38",
      "ip": "192.168.1.100",
      "state": "MANAGED_BY_OTHER",
      "username": "ui",
      "info": {
        "locating": false,
        "volume": 0,
        "source": "",
        "stream_playing": false,
        "service": "",
        "upgrade_status": ""
      },
      "extra_info": { ... },
      "now_playing": {
        "song": "", "artist": "", "album": "",
        "length": 0, "current": 0, "cover_path": ""
      }
    }
  ],
  "offset": 0, "limit": 0, "total": 1
}
```

> **Note:** REST state data may be stale. Use MQTT for real-time state.

#### GET /groups

Lists speaker groups. Returns `{ data: null }` when no groups exist.

#### PATCH /devices/{id}

Update device metadata (e.g. name). Body must include valid update fields.

## MQTT Protocol

### Connection

- **Host:** Device IP (e.g. `192.168.1.100`)
- **Port:** `8883` (MQTT over TLS)
- **TLS:** mTLS required — client certificate + key bundled in the UniFi Play app
  - Certificate: `res/raw/mqtt_cert.crt` (issued by `mqtt.unifi-play.ui.com`)
  - Private key: `res/raw/mqtt_cert_key.key` (RSA)
  - Server cert verification: disabled (insecure trust manager)
- **Client ID:** Any unique string (e.g. `ha-unifiplay-{random}`)
- **Keep-alive:** 60 seconds
- **Clean session:** true

### Topics

| Direction | Topic Pattern | QoS |
|-----------|--------------|-----|
| Subscribe | `UPL-AMP/{MAC}/status` | 0 |
| Subscribe | `UPL-DEVICE/{MAC}/status` | 0 |
| Publish   | `UPL-MOB/{client_uuid}/action` | 0 |

MAC is uppercase, no colons (e.g. `AABBCCDDEEFF`).

### Message Format ("Binme")

All MQTT payloads use a custom binary framing:

```
┌─────────────── Part 1 (Header) ───────────────┐
│ Type (1B) │ Format (1B) │ Compressed (1B) │ Reserved (1B) │ Length (4B BE) │ Data... │
├─────────────── Part 2 (Body) ─────────────────┤
│ Type (1B) │ Format (1B) │ Compressed (1B) │ Reserved (1B) │ Length (4B BE) │ Data... │
└───────────────────────────────────────────────┘
```

| Field | Values |
|-------|--------|
| Type | `0x01` = Header, `0x02` = Body |
| Format | `0x01` = JSON, `0x02` = String, `0x03` = Binary |
| Compressed | `0x00` = No, `0x01` = Yes (zlib deflate) |
| Length | Big-endian uint32, byte count of data |

### Event Messages (device → client)

Header JSON:
```json
{"id": "uuid", "type": "event", "timestamp": 1774993656791, "name": "info"}
```

Event names and body shapes:

#### `online`
```json
{"status": 1}
```

#### `info` (main device state)
```json
{
  "locating": false,
  "volume": 25,
  "source": "lineIn",
  "deviceName": "Living Room",
  "space": "UniFi Play",
  "stream_playing": false,
  "muted": false,
  "upgrade_status": "latest",
  "balance": 0,
  "loudness": true,
  "screen_brightness": 100,
  "led_brightness": 100,
  "tz": "America/New_York",
  "screen_color": "0000FF",
  "led_color": "0000FF",
  "persistent_dashboard": false,
  "eq_enable": true,
  "vol_limit": 100,
  "channels": 0,
  "locked": false,
  "subwoofer": true,
  "soundtrack_paired": "unpair"
}
```

Other events: `metadata`, `extra_info`, `equalizer`, `groups`, `alarms`,
`quiet_hours`, `sub_audio`, `voice_enhancement`, `streaming_timeout`,
`announce_chime`, `announcement_vol`, `tos`, `admin_lock`, `online`,
`support_file`, `ap_scan_result`, `minimum_app_version`.

### Request Messages (client → device)

Header JSON:
```json
{"id": "uuid", "type": "request", "timestamp": 1774993656791, "action": "set_volume"}
```

#### Volume Control

Action: `set_volume`
```json
{"volume": 25, "info_sync": true}
```

#### Audio Source

Action: `select_audio_source`
```json
{"select_audio_source": "lineIn"}
```

Known sources: `lineIn`, `bluetooth`, `airplay`, `spotify`, `hdmi`, `optical`.

#### Other Actions

| Action | Body | Description |
|--------|------|-------------|
| `get_info` | `{}` | Request current device info |
| `get_extra_info` | `{}` | Request network/hardware info |
| `locate` | `{"enable": true}` | Flash device LEDs |
| `restart` | `{}` | Reboot device |
| `stop` | `{}` | Stop playback |
| `set_equalizer` | `{...eq settings}` | Configure EQ |
| `set_quiet_hour` | `{...schedule}` | Set quiet hours |
| `set_screen_brightness` | `{"screen_brightness": 50}` | Set screen brightness |
| `set_vol_limit` | `{"vol_limit": 80}` | Set max volume |
| `set_sub_audio` | `{...}` | Configure subwoofer |
| `set_voice_enhancement` | `{...}` | Configure voice enhancement |
| `set_streaming_timeout` | `{...}` | Set streaming timeout |
| `user_fw_upgrade` | `{"version": "..."}` | Trigger firmware update |

### Device Models

| Constant | Device |
|----------|--------|
| `UPL-AMP` | PowerAmp |
| `UPL-PORT` | In-Wall (Port) |
| `UPL-DEVICE` | Generic/all devices |
