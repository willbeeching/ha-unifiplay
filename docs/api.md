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
  disableBackups: true
  installOnDeviceDiscovery: true
```

Note there is no channel, version, or per-model gating in this entry.

`installOnDeviceDiscovery` is why nobody installs Apollo by hand — and Apollo is the
only application in the catalogue that carries the flag. Discovering a device from the
Apollo hardware line (`UPL-Amp-B/W` PowerAmp, `UPL-Port-B/W` Play Audio Port) triggers
the fetch. `unifi-core` then writes
`/data/unifi-core/config/http/shared-runnable-apollo.conf` and reloads nginx, which is
what makes `/proxy/apollo/` exist.

**Consequence for clients:** no Apollo application means no `location /proxy/apollo/`
block, so requests hit the UniFi OS single-page-app catch-all and return **`200` with
an HTML body** — not 404, and regardless of credentials. See
[Distinguishing failures](#distinguishing-failures).

### The install gate: discovery × release channel

`installOnDeviceDiscovery` is necessary but not sufficient. Two conditions must both
hold ([#4](https://github.com/willbeeching/ha-unifiplay/issues/4)):

1. An Apollo-line device is discovered on the console.
2. A published `apollo` package exists at or below the console's release channel.

The console's channel lives in `/data/unifi-core/config/firmware.yaml` as
`releaseChannel` (`release`, `release-candidate`, `beta`) and surfaces in the logs as
`max_release_channel`. `runnables.yaml` carries a `releaseChannels:` map naming a
channel per application, plus an `updates:` map holding any pinned version.

**That map is a per-application channel preference the owner can change**, not a record
of where Ubiquiti publishes a package. Observed `apollo` values differ across consoles
(`release`, `release-candidate`, `beta`) and follow the owner's settings — changing
another application's channel in the UI rewrites its entry directly. Do not infer
package availability from it.

**The channel is not the gate.** Across [#4](https://github.com/willbeeching/ha-unifiplay/issues/4),
two consoles were raised to `beta` — the highest tier — with Play hardware adopted, and
neither installed Apollo; on one, other applications gained new version choices at the
same time, so the change had clearly taken effect. The discriminating variable is the
console **model**: Apollo runs on UDM Pros and has never been seen on a UCG-Fiber or a
Cloud Key Plus at any channel. The sharpest case is one owner's two consoles, both on
firmware `v5.1.27` and both non-Official, where the UDM Pro runs Apollo and the Cloud
Key Plus has no `apollo` process at all.

This does not conflict with Play being a retail product: Play hardware is driven by the
Play mobile app and requires no console, so Apollo's console-side rollout is independent
of hardware availability.

**Apollo is not distributed through apt.** On a working UDM Pro, `apt-cache policy
apollo` reports `0.7.4` with its only source `/var/lib/dpkg/status` — no repository
URL — and the apt sources are stock Debian bullseye with an empty `sources.list.d/`.
`unifi-core` downloads the `.deb` directly from `fw-download.ubnt.com/data/apollo/…`
and invokes `dpkg`, gated by `max_release_channel`. There is no repository a user can
add to obtain Apollo while remaining on Official.

A console failing gate 2 logs this cycle on every boot and never progresses:

```
systemd.log  info: Initialize apollo service
uos.log      INFO Getting current version of installed package package_name=apollo
uos.log      ERROR Exit with error: Package "apollo" not installed
apps.log     warn: Attempted to enable auto-update for apollo application but it is
                   not installed, configured, or is not ready
```

No download, unpack, or signature error appears — the console is not failing to install
Apollo, it never requests it. A console that passes both gates logs instead:

```
uos.log   INFO Start to download package package_name=apollo version=None
               max_release_channel=Some(ReleaseCandidate) use_user_prefs=false
uos.log   INFO Downloading runnable package_name=apollo url=<fw-download…/apollo/….deb>
          Unpacking apollo (0.7.4) → Setting up apollo (0.7.4)
apps.log  info: Installing the latest version of "apollo" application from
                "release-candidate" release channel
```

`Start to download` is the discriminating line. The UDM Pro documented here sat on
`release-candidate` for six months emitting the failure cycle, then installed Apollo the
day an Apollo device appeared — so neither gate alone suffices. (The device-discovery
half is inferred from the mechanism and the timing, not from a contiguous trace: the
retained install and discovery log lines are hours apart.)

### consoleGroup.yaml is not the gate

`/data/unifi-core/config/consoleGroup.yaml` carries an `applications:` block on a UDM
Pro at `5.1.26` (`apollo: { required: false, owned: false, supported: true }`) but only
console *group* membership on a UCG-Fiber at `5.1.19`. Both consoles report
`self.role: UNADOPTED`, so console-group role does not explain the difference — a point
firmware version is the likelier cause. Either way the file does not gate installation:
the UDM Pro installed Apollo while `UNADOPTED` with `apollo.owned: false`.

### Play devices in UniFi Network

Play hardware appears in the UniFi Network device list like any other adopted device,
with the managing application named in an `Application` column — `Play` for `UPL Amp`
and `UPL Port` hardware, as against `Network` or `Access` for everything else. A UDM Pro
serving two working PowerAmps lists both as `Play` / `UPL Amp` / Online alongside its
switches and UPS.

The Apollo API's `MANAGED_BY_OTHER` device state reflects exactly that: the device is
managed by an application *other than Network*, not hidden from it.

Presence in Network is no evidence that Apollo is installed — a Cloud Key Plus has been
reported listing five Audio Ports while having no Apollo application at all. Absence,
however, is informative: a Play device missing from the device list of the console you
are querying will not appear in that console's Apollo `/devices` response either, and
the integration will set up with no entities.

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
  - Pre-2.0.2 the pair lived in `res/raw/mqtt_cert.crt` + `mqtt_cert_key.key`
  - 2.0.2 moved both generations into `libnative-lib.so` (AES-256-CTR); see below
  - Issued by `mqtt.unifi-play.ui.com` (RSA)
  - Server cert verification: this integration disables it (`CERT_NONE`). The
    official app still skips it for the 2023 generation, and started verifying
    the broker CA for the 2026 generation.
- **Client ID:** Any unique string (e.g. `ha-unifiplay-{random}`)
- **Keep-alive:** 60 seconds
- **Clean session:** true

#### The client certificate is rotated by firmware

PowerAmp firmware `1.0.41` (~2026-08-23) changed the CA behind that mutual TLS,
and devices that take it stop accepting the certificate the app shipped in
2023. The official app was cut off the same way: UniFi Play 2.0.2's release
notes read *"Requires PowerAmp 1.0.41 or newer."* Reported and diagnosed in
[#20](https://github.com/willbeeching/ha-unifiplay/issues/20) against a
`1.0.41` amp; the bundled-certificate figures below were confirmed locally
with `openssl`.

| | Bundled client (2023) | Bundled client (2026) | Device cert after 1.0.41 |
|---|---|---|---|
| Issuer | `C=US, CN=mqtt.unifi-play.ui.com` | `C=US, CN=mqtt.unifi-play.ui.com` | `C=US, CN=mqtt.unifi-play.ui.com` |
| Subject | `CN=68D79A05B497` | `CN=68D79A05B497` | `CN=mqtt.unifi-play.ui.com` (no C) |
| Generated | 2023-09-25 09:36:27 | 2026-07-20 09:37:13 | 2026-07-20 09:37:13 |
| Serial | `0x6511549b` | `0x6a5dec4a` | `0x6a5dec49` |

Same common name, different keypair — the old certificate is unexpired (valid
to 2033) and structurally fine; the device simply no longer holds the CA that
signed it. Connecting with no client certificate is still refused, so mutual
TLS is not being dropped, only re-keyed.

The 2026 client pair is `certs/mqtt_cert_2026.crt` + `mqtt_cert_2026_key.key`,
taken from UniFi Play **2.0.2** (`com.ui.unifi.play`). That build no longer
ships the PEMs in `res/raw`. They sit in `libnative-lib.so` as
`mqtt_cert2_crt` / `mqtt_cert2_key`, AES-256-CTR encrypted (tiny-AES-c),
behind JNI `getNativeCrt2` / `getNativeKey2`. Decrypting them recovered
valid PEM; the RSA moduli match; `openssl verify` against the app's own
broker-CA bundle (`mqtt_broker_cert2_crt`) returns OK. The 2023 blobs in
the same `.so` decrypt to the pair this repo has shipped since 2023, which
is the check that the decrypt is the one the app uses.

**Verified against hardware** on a PowerAmp `UPL-AMP` / `UPL-Amp-B`
(`6C63F8AA2F29`, hostname Garage, firmware
`UPL-AMP.qcs405.v1.0.41.aa8b53c.260803.07:20:10`):

- 2026 generation: CONNACK in ~30 ms (TLS default and TLS 1.2). A subsequent
  `info` request returned `deviceName=Garage` on
  `UPL-AMP/6C63F8AA2F29/status`.
- 2023 generation: no CONNACK under TLS 1.3 (`on_disconnect` with no
  exception); TLS 1.2 raises `ssl.SSLError: [SSL: TLSV1_ALERT_UNKNOWN_CA]`.

The device cert presented on 8883 is serial `0x6a5dec49`, issued the same
second as the 2026 client cert (`0x6a5dec4a`). [#20](https://github.com/willbeeching/ha-unifiplay/issues/20)
described that device cert as `CN=mqtt.unifi-play.ui.com` with **no C**;
that is the **subject**. The issuer, measured here with `openssl` on the
same firmware, is `C=US, CN=mqtt.unifi-play.ui.com`.

Not verified: a pre-`1.0.41` device still connecting on the 2023 pair after
this fallback is live. That is the other half of the rollout.

Two things make this hard to see, and both are worth knowing before debugging
a "device is up but does nothing" report:

- **TLS 1.3 hides the rejection.** The server defers client-certificate
  verification until after the handshake, so the TCP connect and handshake
  both succeed, `on_connect` never fires, and the refusal arrives as a bare
  disconnect with no exception. Forcing TLS 1.2 surfaces the real alert:
  `ssl.SSLError: [SSL: TLSV1_ALERT_UNKNOWN_CA]`. Absence of a CONNACK is
  therefore the only reliable signal, which is why `connect()` waits for one.
- **The certificate is not bound to the device.** The bundled cert's CN is a
  MAC (`68D79A05B497`) belonging to no device any user of this integration
  owns, and it authenticated against every device for two years. So the device
  accepts anything chaining to its CA and does not pin identity — meaning one
  certificate per CA generation is enough, and a certificate taken from a
  newer app build should work on any device of that generation.

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

**The same `source` value means a different physical jack depending on the
model, so these must never be treated as one shared list.**

Verified on a **UPL-PORT** (fw 1.1.10) by selecting each input in the Play app
and reading back what the device reported. This is the complete set the Port
accepts, and it maps 1:1 onto the five inputs the app offers:

| `source` | Play app label | Physical jack |
|----------|----------------|---------------|
| `streaming` | Streaming | none - network audio |
| `speakers` | eARC | HDMI eARC |
| `lineIn` | Line In | RCA analog in |
| `spdif` | S/PDIF | optical TOSLINK in |
| `usb` | USB | USB-C |

> **`speakers` is the HDMI eARC input, not a speaker-level output.** The name
> is misleading and cost real debugging time: because a Port has *both* an
> optical jack and an eARC port, and `spdif` is the optical one, eARC needs its
> own value - and `speakers` is it. Publishing `hdmi`, `earc`, `eArc`, `arc`,
> `hdmiArc` or `hdmiIn` is silently ignored by the device; none of them are
> real values. Do not "fix" `speakers` to one of those.

Verified on a **UPL-AMP** (fw 1.0.38) the same way, by publishing each
candidate with `set_audio_src` and reading back the device's reported `source`.
The amp offers three inputs in the app, and **eARC is `speakers` here too**:

| `source` | Play app label | Physical jack |
|----------|----------------|---------------|
| `streaming` | Streaming | none - network audio |
| `speakers` | eARC | HDMI eARC |
| `lineIn` | Line In | RCA analog in |

> **The amp also accepts `spdif`, and it is a trap.** The device echoes it back
> as its current source, but a PowerAmp has no optical jack and the Play app
> offers no such input - it is shared firmware accepting a value it cannot
> route. The integration deliberately omits it from the amp's map. Until
> v1.3.0 the amp's `HDMI eARC` label pointed at exactly this value, so
> selecting eARC on an amp reported success and passed no audio, which is the
> same silent-nothing failure the `speakers` name caused on the Port.

`hdmi`, `earc`, `eArc`, `arc` and `hdmiIn` were each published to an amp and
left the source unchanged, so - as on the Port - none of them are real values.

Both models therefore use `speakers` for eARC, and both label it `eARC`. The
per-platform maps still must not be merged, but the reason is the inputs, not
the values: the Port has optical S/PDIF and USB jacks the amp lacks, and the
amp accepts `spdif` with nowhere to route it.

Values seen elsewhere in captures but not confirmed as settable `source`
values: `bluetooth`, `airplay`, `spotify`, `optical`. The first three look like
`service` (what is streaming *to* the device) rather than a physical input.

The Port also accepts an output-routing form, `{"out": "lineOut"|"spdif"|"usb"}`.

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
| `announce` | `{...}` | Announcements: play, stop, schedule, file list |
| `set_alarm` | `{...alarm}` | Create or modify an alarm |
| `alarm_test` | `{"sound": "...", "on": true}` | Play an alarm tone until stopped |
| `set_announce_chime` | `{"chime": "Quick Steps"}` | Chime played before an announcement |
| `set_announcement_vol` | `{...}` | Announcement volume, separate from music |

Bodies for the announcement, alarm, quiet-hours and EQ actions - and the
non-obvious semantics of several of them - are in
[Write Actions in Detail](#write-actions-in-detail) below.

### Write Actions in Detail

Reverse-engineered from a 1,782-message capture of the app driving a PowerAmp
(fw 1.0.38) plus live testing on the same hardware. The bodies below are what
the app actually sends; the notes are the parts that are not guessable.

#### Announcements

Action: `announce`. The body's own `action` field selects the operation.

| `body.action` | Body | Notes |
|---------------|------|-------|
| `schedule-announcement` | `{"filename": "prerecord/X.mp3", "length": 17, "name": "...", "zone_play": false, "enable": true}` | **Plays immediately**, despite the name |
| — | `{"enable": false}` | Stops the announcement currently playing |
| `set_schedule` | `{"schedule": [{...}]}` | Replaces the whole schedule list |
| `add_file` / `del_file` | `{"files": [{"name": "X.mp3", "length": 17}], "file_count": 1}` | Bookkeeping only; see the upload note |

Two traps:

- **`schedule-announcement` is the fire-now primitive.** The name suggests it
  creates a schedule entry; with `enable: true` it starts playback there and
  then. Reading the name instead of testing the behaviour costs real time.
- **`filename` needs the `prerecord/` prefix here**, while the file list in the
  `announcement` event reports bare names. Pass the bare name and nothing plays.
- **The app's label for a clip is not its filename.** Recordings shown as "17"
  and "18" in the mobile app are `New Recording 17.m4a` and
  `New Recording 18.m4a` on the device — full prefix, and `.m4a` rather than
  the `.mp3` the app's UI implies
  ([#14](https://github.com/willbeeching/ha-unifiplay/issues/14)). Always read
  the real names from the `announcement` event rather than reconstructing them,
  because a wrong name fails the same way a missing prefix does.

A bad `filename` is silent in both directions: the device neither plays nor
complains, so there is nothing to catch. Read the names, don't guess them.

Music is paused for the duration and resumes by itself.

##### A chime always plays first, and cannot be turned off

Every announcement is preceded by one of five chimes: `Ascending Steps`,
`Chimes`, `Hopscotch`, `Quick Steps`, `Vibraphone`. There is no off switch:

- `set_announce_chime` carries only `{"chime": "<name>", "timestamp": N}`.
- The `schedule-announcement` body has no chime field.
- No off/none/enable flag for the chime appears anywhere in the capture,
  including the app cycling through all five.

The chime is therefore a fixed lead-in. Measured on a PowerAmp by timing the
`announcing` flag in `info`, the envelope runs a **constant** amount longer than
the clip regardless of clip length - about 4.76 s on `Ascending Steps` and
3.74 s on `Quick Steps` (chime plus pre/post buffer). Anything that needs to
line an announcement up with an external event has to allow for it.

##### Audio upload is not possible over MQTT

Ruled out, not merely unimplemented. A real 274-second upload from the app
produced no MQTT traffic carrying audio anywhere in the capture; roughly 30
candidate HTTP paths on the device were probed; and the firmware index has no
matching endpoint. `add_file` is bookkeeping the app sends *after* the transfer
has already happened by another route.

Practical boundary: upload in the app, then automate playback.

#### Alarms

Action: `set_alarm`. Omit `alarm_id` to create, pass an existing one to modify.

```json
{"action": "add", "alarm_id": "", "name": "Morning", "hour": 7, "minute": 30,
 "sound": "Lunar Chimes", "volume": 25, "duration": 2, "repeat": [1, 2, 3, 4, 5],
 "on": true, "timestamp": 1785926923}
```

`repeat` is weekday numbers with **0 = Sunday**; an empty list means fire once.
Sounds: `Lunar Chimes`, `Cosmic Bounce`, `Digital Ripple`, `Island Breeze`,
`Jungle Rhythm`. `duration` is in minutes.

`alarm_test` (`{"sound": "...", "on": true, "volume": 25, "name": "..."}`) plays
an alarm tone until sent again with `on: false` - the only fire-now sound
primitive besides an announcement.

Alarms are evaluated on the device, so they still fire with no client connected.

#### Graphic EQ

Action: `set_equalizer`.

```json
{"profile": "custom", "table": {"32": 0.01, "64": 0.01, "125": 0.01, "250": 0.01,
 "500": 0.01, "1k": -6.39, "2k": 0.01, "4k": 0.01, "8k": 0.01, "16k": 0.01},
 "info_sync": false}
```

Ten fixed bands, plus or minus 12 dB. The app sends `info_sync: false` while a
slider is in motion and `true` on release.

Five things worth knowing:

- **Preset recall is `active_preset`, not `profile` and not `preset_name`.**
  `{"profile": "custom", "active_preset": "<name>"}` recalls. Passing the preset
  name as `profile` is accepted silently and does nothing, which reads exactly
  like a device-side bug until you find the right field.
- **`preset_action: "apply"` DELETES the preset.** Discovered by losing one. The
  management verbs are `mod` (rename, with `preset_rename`) and `del`.
- **`0.01` is the app's placeholder for an untouched band, not a real baseline.**
  The device echoes those bands back as `0.0` in `active_table` and rounds gains
  to 1 dp - send `{1k: 0.89, rest: 0.01}` and it reports `{1k: 0.9, rest: 0.0}`.
  Sending `0` to flatten a band is correct.
- **The built-in profiles report a flat `active_table`.** Their shaping happens
  inside the device, so reading the table back while a built-in profile is
  active tells you nothing about what you are hearing.
- **Presets persist across a graceful reboot — but one field wipe has been
  seen.** Verified on a PowerAmp (fw `1.0.38.37ed30f`): two presets — one
  created minutes earlier, one two days old — both survived a reboot via the
  device's `restart` action, timestamps intact. The same device had earlier
  come back with an empty `custom_presets` after an unattended overnight
  reboot during electrical maintenance: firmware unchanged, and no client on
  the network could have deleted them (every released version of this
  integration recalls via `active_preset`; nothing sends `del` unasked).
  Cause undetermined; hard power loss is the suspect. What would settle it:
  cut mains power while holding a known preset list, then read back
  `custom_presets`. Until then, treat preset storage as durable against clean
  restarts but not proven durable against power loss. The coordinator logs a
  warning when a device's preset list transitions from populated to empty, so
  a future wipe carries a timestamp instead of surfacing weeks later.

#### Quiet hours

Action: `set_quiet_hour`. Start and end times plus a `repeat` weekday list (same
0 = Sunday numbering) and an optional wind-down fade. The device silences itself
for the window and restores the previous volume afterwards.

#### Zones (groups)

A zone is a set of devices that play in sync. Read via the `groups` event, written
with the `set_groups` action. Verified on UPL-PORT fw 1.1.10.

```json
{"groups": [
  {
    "group_id": "9c7ba639-ecf4-4c70-bc91-adf043f3e9ae",
    "name": "Test zone",
    "dev_info": [
      {"type": "UPL-PORT", "mac": "1C0B...CB", "name": "Living Room",
       "ip": "192.168.2.146", "color": "black", "host": true},
      {"type": "UPL-PORT", "mac": "1C0B...AA", "name": "Family Room",
       "ip": "192.168.2.112", "color": "black", "host": false}
    ],
    "dev_count": 2,
    "group_index": 1,
    "broadcasting_mode": "zone_only",
    "wb_enable": false,
    "wb_device": "",
    "wb_input": "",
    "timestamp": 1786371652
  }
]}
```

##### `set_groups` is replace-all, per device - and does not propagate

Each publish replaces **every** zone on the device it is sent to, and **only**
on that device. Nothing is forwarded between speakers.

Every device holds a copy of every zone, including zones it is not a member of.
So writing to the zone's host alone leaves every other device serving its
previous copy indefinitely. Measured on five UPL-PORTs (fw 1.1.10) right after
a host handover written only to the new host:

| device | in zone? | host it reported |
|--------|----------|------------------|
| Family Room | yes | Family Room (correct) |
| Living Room | yes | Living Room (stale) |
| Kitchen | yes | Living Room (stale) |
| Ryan's Office | no | Living Room (stale) |

Those stale copies then compete in the merge, which is how an edit the device
had accepted could appear to revert on the next resync.

**Write the complete zone list to every connected device.** Re-publishing the
full list to all five converged them immediately, and the three zone members
held that state across a reload. (A non-member reverted to its own stale copy
afterwards, which is harmless: a device only claims a zone when the copy names
*itself* as host, so a non-member's copy can never win the merge.)

`coordinator.publish_zones()` does this; `update_zone()` / `delete_zone()` wrap
it. Callers no longer assemble a sibling list - the full list is rebuilt from
coordinator state, so zones on other hosts survive without every caller having
to remember to resend them. It also means a zone changing hands needs no
separate "strip it from the old host" write.

This also makes Home Assistant and the mobile app equal peers with no locking:
whichever writes last wins. A zone created from HA while the Play app is open on
a zone screen will often vanish immediately, because the app republishes its own
view. This is a protocol limitation, not a bug to fix.

##### Every member reports the zone, and stale copies will bite you

A zone appears in the `groups` event of *every* device in it, not just the host.
After an edit the host emits the new state immediately while members keep
serving their previous copy until they resync. **Merging those copies naively -
last writer wins - lets a stale member copy silently revert an edit that the
device actually accepted.** Prefer the copy whose reporting device is the zone's
own `host`, falling back to a member copy only when the host is not reporting
(see `_update_from_groups` in `coordinator.py`).

Fanning writes out to every device (above) removes most of the divergence at
source; the host preference remains as the tie-break for the window between a
write and each device's resync, and for devices that were offline during it.

##### The host is an internal role

Exactly one entry in `dev_info` has `"host": true`. The host owns the zone: its
group list is authoritative and `set_groups` is published to it. The Play app
does **not** expose this - users just pick devices - so the integration does not
surface it either.

Removing the hosting device therefore has to hand the role over rather than
refuse. Because each device's list is replace-all, that is two writes: give the
zone to the new host (with `host` flipped in `dev_info`), then strip it from the
old host's list. Both devices must be online, or the zone can end up owned by
nobody or by two devices at once.

After the handoff the old host's device-level `hosting_group` field stays stale
until it pushes a fresh `info` event. That field comes from the device's own
reporting, so it cannot be corrected locally.

It does **not** always self-heal. `hosting_group` and `sync_devices` are sent
only while true, so a device that leaves every zone simply stops sending the
keys and its last values stand indefinitely - confirmed on two UPL-AMPs, which
still reported their old `hosting_group` and `sync_devices: true` through
several fresh `info` events after their zones were deleted, while the `groups`
event correctly reported none. Never derive membership from these fields; use
zone membership, as `binary_sensor` does.

##### A device may belong to more than one zone

The Play app allows only one zone per device and the integration enforces the
same, but **that is app policy, not a firmware limit.** Tested directly on two
UPL-AMPs (fw 1.0.38) by publishing a two-zone list where both zones contained
both devices:

| state written | what each device reported |
|---|---|
| one zone, both members | 1 zone, both devices agreeing |
| two zones, both members of each | **2 zones on both devices** |
| after an 8s settle | still 2 zones, nothing dropped |

No rejection, no silent drop, no revert on resync. Overlapping membership is
accepted by `set_groups`.

The real constraint is hosting, not membership. `hosting_group` is a single
scalar, so a device has no way to report hosting two zones at once. In the test
each device hosted a different zone and both reported correctly; a device asked
to host two would leave the second with no authoritative reporter, which
matters because zone state prefers the copy whose reporting device names itself
host.

Two things this test did **not** establish, before anyone builds on it:

- **Whether playback is coherent.** Nothing was streaming. How audio behaves
  when a speaker belongs to two zones playing different sources is untested.
- **Whether it survives the app.** The app republishes its own view and cannot
  represent overlapping zones, so opening a zone screen would likely collapse
  them - the same equal-peers race described above.

##### `broadcasting_mode` - stream broadcasting

Which targets advertise themselves to streaming clients (AirPlay, Spotify
Connect, Cast). Verified by setting each mode in the app and reading it back:

| Value | Play app | Meaning |
|-------|----------|---------|
| `zone_only` | Zone Only | only the zone is available for streaming |
| `zone_devices` | Zone & Devices | zone *and* each device individually |
| `off` | Off | neither is available |

##### `wb_*` - broadcasting a wired source

The protocol calls it "wideband"; the app calls it a **broadcast wired source**.
Any device in the zone can broadcast one of its physical inputs to the rest.

- `wb_enable` - whether a wired source is being broadcast
- `wb_device` - MAC of the device doing the broadcasting; **not** necessarily
  the host, which is why a UI must let the user pick which device
- `wb_input` - a `source` value from the tables above. eARC is `speakers` on
  both models, but the input *sets* differ (a Port has S/PDIF and USB; an amp
  has neither), so resolve it against the broadcasting device's own model
  rather than a shared map.

`""` for `wb_input` means no wired source: the zone is streaming.

##### Two publishes, to two different devices

Starting or stopping a broadcast wired source is **not one write**. The zone
itself is owned by the host, but the input switch belongs to whichever device
is actually broadcasting:

```
host device      <- set_groups   (wb_enable / wb_device / wb_input)
wb_device device <- set_audio_src ({"source": "lineIn"})
```

These are frequently different devices. There used to be a `set_group()` helper
that bundled both and sent them to the same client; it was removed because that
is wrong whenever the source is not the host. Use `update_group()` for the zone
write and `set_source()` on the source device's own client.

Observed on a three-Port zone (fw 1.1.10) before the fix: with Kitchen hosting
and Living Room selected as the source, `wb_device` was written correctly as
Living Room while `set_audio_src` went to **Kitchen** - so Kitchen switched to
Line In and Living Room stayed on Streaming. The wrong device was switched and
the chosen one was left alone.

Also note the mirror case when turning broadcasting off: the input has to be
handed back on the device that *was* broadcasting, again not necessarily the
host.

Still unverified: a **mixed-model zone** (host and source of different models).
The author has no PowerAmp. The failure this fix removes was model-independent,
but a mixed zone additionally has to resolve `wb_input` against the source
device's platform - the code does this, it has simply never run on real mixed
hardware. Anyone with both models: please confirm and record the result here.

### Push-only Events

These events are **never sent unless requested**. A client that only subscribes
and waits will show initialiser defaults forever, which looks like broken
entities rather than a missing request:

`equalizer`, `sub_audio`, `alarms`, `quiet_hours`, `announcement`,
`announce_chime`, `voice_enhancement`, `streaming_timeout`, `announcement_vol`.

Send the matching request on connect. Mind the naming: the request is
**`get_announcement`** but the reply arrives as **`announcement`**, so matching
reply names to request names does not work uniformly.

### Not Exposed by the Protocol

Confirmed absent rather than undiscovered, from the same capture:

| Wanted | Status |
|--------|--------|
| Audio file upload | Not over MQTT (see above) |
| Disabling the announcement chime | No field exists |
| Name of the connected AirPlay / Spotify client | The service is reported; the device never publishes the client's name |

### Device Models

| Constant | Device |
|----------|--------|
| `UPL-AMP` | PowerAmp |
| `UPL-PORT` | In-Wall (Port) |
| `UPL-DEVICE` | Generic/all devices |
