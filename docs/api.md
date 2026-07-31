# UniFi Play / Apollo API Reference

Reverse-engineered from the UniFi Play Android app v2.0.0 and live device testing.
REST details verified against Apollo `0.7.4` on UDM-Pro firmware `5.1.26`
(unifi-core `5.1.126`, uos `5.1.4`).

## Where the API comes from

`Apollo` is Ubiquiti's product-line name for UniFi Play, and the name of the UniFi OS
application that manages it. On the console it is a Debian package (`apollo`) run by
systemd as the `apollo` user, listening on **loopback only** (`127.0.0.1:19880`).

UniFi OS's application catalogue at
`/usr/share/unifi-core/app/config/default.yaml` marks it:

```yaml
apollo:
  packageName: 'apollo'
  serviceName: 'apollo'
  ports: { http: { api: 19880 } }
  displayName: 'Apollo'
  installOnDeviceDiscovery: true
```

`installOnDeviceDiscovery` is why nobody installs Apollo by hand — and Apollo is the
only application in the catalogue that carries the flag. Discovering a device from the
Apollo hardware line (`UPL-Amp-B/W` PowerAmp, `UPL-Port-B/W` Play Audio Port) causes
the console to download and install the package. `unifi-core` then writes
`/data/unifi-core/config/http/shared-runnable-apollo.conf` and reloads nginx, which is
what makes `/proxy/apollo/` exist.

**Consequence for clients:** no Apollo application means no `location /proxy/apollo/`
block, so requests hit the UniFi OS single-page-app catch-all and return **`200` with
an HTML body** — not 404, and regardless of credentials. See
[Distinguishing failures](#distinguishing-failures).

Per-model support is tracked in `/data/unifi-core/config/consoleGroup.yaml` under
`applications:` — **on the UDM Pro, where this was observed**. An
`apollo: supported: false` there would mean that console can never run Apollo. The file
does not serve that purpose on UniFi OS 5.x: on a UCG-Fiber running 5.1.19 it holds
console *group* membership instead (`self.role`, `members`), with no `applications:`
block at all.

### Discovery does not guarantee installation

`installOnDeviceDiscovery` describes intent, not a guarantee. On the UCG-Fiber above,
with an Audio Port adopted and online for six months, the console repeats this cycle on
every boot and never installs the package
([#4](https://github.com/willbeeching/ha-unifiplay/issues/4)):

```
systemd.log  info: Initialize apollo service
uos.log      INFO Getting current version of installed package package_name=apollo
uos.log      ERROR Exit with error: Package "apollo" not installed
apps.log     warn: Attempted to enable auto-update for apollo application but it is
                   not installed, configured, or is not ready
```

No download, unpack, or signature error appears anywhere — the console is not failing
to install Apollo, it is never requesting it. `systemctl list-unit-files | grep -i
apollo` is empty on that console, confirming the package has never been present.

That console's `runnables.yaml` carries `apollo: beta` while every installed
application is on the Official release channel, suggesting Apollo is published
pre-release on some platforms. Unconfirmed, but it fits the observed behaviour: a
console tracking Official would never fetch a beta-channel package, however many Play
devices it discovers.

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

The API key is generated in UniFi OS Settings (**Control Plane → API Keys**) and is
per-console. The nginx location block for `/proxy/apollo/` includes
`auth.conf`, which resolves API keys via an `auth_request` subrequest and forwards
`X-ApiKeyId` upstream — so API keys are a first-class credential for this path (unlike
some `/api/*` routes, which accept only a session cookie).

`/proxy/apollo/public/` is routed without `auth.conf`, i.e. unauthenticated, though it
returns 404 on Apollo 0.7.4.

### Distinguishing failures

Status code alone is ambiguous. Branch on content type:

| Response | Meaning |
|----------|---------|
| `200` + `application/json` | Apollo installed, key accepted |
| `200` + `text/html` | **No Apollo application on this console** — nginx SPA fallback. Happens with a valid key too |
| `401` + `application/json` | Route exists, so Apollo *is* installed; credential rejected |
| `403` + `application/json` | Key not valid for this console, or revoked |
| `404` + `text/plain` | Apollo's own Go 404 — installed, but no handler at that path |

A `resp.ok`-style check will sail past the HTML case and then fail on JSON decode.

One more trap: `api.ui.com` (Ubiquiti's Site Manager cloud API) answers the Apollo
path with a JSON `404`, which reads identically to "Apollo installed, no handler".
Nothing at ui.com proxies Apollo — the config flow refuses ui.com hosts outright for
this reason.

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

Observed `state` values: `MANAGED_BY_OTHER` (device present in this console's list but
owned elsewhere — e.g. managed from the Play mobile app). The full set is not known, so
do not filter devices on `state`.

`extra_info.platform` and `extra_info.model` are empty strings in practice — use the
top-level `platform` field instead.

#### GET /groups

Lists speaker groups. Returns `data: null` — not `[]` — when no groups exist, so
null-guard it.

#### GET /info

Health and version. Cheap, and a better connectivity probe than fetching all devices.

```json
{
  "err": null,
  "type": "single",
  "data": {
    "status": "up",
    "host": "<console-hostname>",
    "build": { "build_time": "2024-10-15T06:52:00Z", "go_ver": "go1.22.3",
               "go_arch": "arm64", "go_os": "linux" },
    "vcs": { "version": "v0.7.4", "commit": "…", "modified": "false" },
    "fw_build": false
  }
}
```

#### Paths that do not exist

`/system`, `/version`, and `/` all return `404 text/plain` on Apollo 0.7.4.

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

Action: `set_audio_src`
```json
{"source": "lineIn"}
```

Known sources: `lineIn`, `bluetooth`, `airplay`, `spotify`, `spdif` (HDMI eARC), `optical`.

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
