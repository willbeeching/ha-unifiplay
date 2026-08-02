# UniFi Play for Home Assistant

[![CI](https://github.com/willbeeching/ha-unifiplay/actions/workflows/ci.yaml/badge.svg)](https://github.com/willbeeching/ha-unifiplay/actions/workflows/ci.yaml)
[![GitHub Release](https://img.shields.io/github/v/release/willbeeching/ha-unifiplay)](https://github.com/willbeeching/ha-unifiplay/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/willbeeching/ha-unifiplay/blob/master/LICENSE)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![vibe-coded](https://img.shields.io/badge/vibe-coded-ff69b4?logo=musicbrainz&logoColor=white)](https://en.wikipedia.org/wiki/Vibe_coding)

A Home Assistant custom integration for **UniFi Play** devices (PowerAmp, In-Wall, etc.) — with or without a UniFi OS Console. Since v1.1.0 the integration can discover and control speakers directly, so it works even on consoles that never get the Apollo application (UCG-Fiber, Cloud Key, …), or with no console at all.

## Features

- **Media player** — volume, mute, now-playing metadata (song, artist, album), cover
  art, and transport control (play / pause / next / previous) while streaming
- **Selects** — audio input, audio output (Ports), EQ preset, sub phase, channels
  (stereo/mono)
- **Switches** — Dynamic Boost, Dolby Atmos / Equalizer, persistent dashboard
- **Number controls** — balance, volume limit, screen brightness, LED brightness, sub crossover, sub level
- **Text** — LED color (hex)
- **Buttons** — locate (flash LEDs), restart
- **Sensors** — firmware update status
- **Real-time state** via direct MQTT connection to each device

## Requirements

The integration has two connection modes, chosen at setup:

- **Direct connection (recommended, no console needed).** Home Assistant finds the
  speakers itself with the standard Ubiquiti discovery probe (UDP 10001 — the same
  one WiFiman uses) and talks MQTT straight to each device. Works on any network,
  regardless of console model, update channel, or whether you own a console at all.
  Speakers on Home Assistant's own subnet are found automatically; speakers on other
  VLANs can be listed by IP.
- **Via a UniFi OS Console** running the **Apollo** application (Ubiquiti's console-side
  name for UniFi Play — the console installs it automatically when it discovers Play
  hardware, but only on some console models). Discovery goes through the console's
  `/proxy/apollo` API with an API key; control is still direct MQTT.

Console mode additionally needs your Play hardware discovered by that console and an
API key created on it (**Settings → Control Plane → API Keys**). If Apollo never
appears on your console — it is model-gated, and changing the update channel does not
help — just use direct connection. The full story of what Apollo is, which consoles
get it, and how to check yours lives in [docs/apollo.md](docs/apollo.md).

## Installation

### HACS (recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu → **Custom repositories**
3. Add `https://github.com/willbeeching/ha-unifiplay` as an **Integration**
4. Install **UniFi Play**
5. Restart Home Assistant

### Manual

Copy the `custom_components/unifi_play` folder into your Home Assistant `config/custom_components/` directory and restart.

## Configuration

Go to **Settings → Devices & Services → Add Integration**, search for **UniFi Play**,
and pick a connection mode.

### Direct connection (no console needed)

1. Choose **Direct connection**
2. If your PowerAmps are on the same subnet as Home Assistant, leave the field empty —
   they are found automatically
3. Enter IP addresses (comma separated) for speakers on other VLANs/subnets — and
   **always for Audio Ports (`UPL-PORT`)**, which don't answer the automatic probe.
   You can read the IPs from the UniFi Network client list or the Play mobile app.
   Make sure nothing blocks UDP 10001 and TCP 8883 between Home Assistant and the
   speakers
4. New devices are re-scanned for every 5 minutes

### Via UniFi OS Console

1. Create an API key on the console: **Settings → Control Plane → API Keys** →
   **Create API Key** (you won't be able to view it again)
2. Choose **Via UniFi OS Console** in the integration setup
3. Enter your console's IP address or hostname only (e.g. `10.0.0.1` — do not include
   `https://`) and the API key
4. Devices will be discovered automatically

## How it works

Control and state are always a direct **MQTT** connection (port 8883, mTLS) to each
speaker — the same channel the Play mobile app uses. The only difference between the
modes is discovery:

- **Direct**: Ubiquiti's discovery protocol (UDP 10001, broadcast on the local subnet,
  unicast to any manually listed IPs) — answered by the speakers themselves
- **Console**: the console's Apollo REST API (`/proxy/apollo/api/v1/`)

All communication stays local on your network.

## Device support

| Platform | Device | Tested |
|----------|--------|--------|
| `UPL-AMP` | PowerAmp | Yes (maintainer hardware) |
| `UPL-PORT` | Audio Port | Community-confirmed working (direct connection; see #4) |

Ports don't answer UDP discovery, so always enter their IPs during direct-connection
setup. Port-specific notes: no subwoofer entities, `spdif` is the optical S/PDIF jack,
inputs include Speakers and USB, and an **Audio Output** select (Line Out / S/PDIF /
USB) exists only on Ports. If you run into device-specific issues, please include the
device platform from the logs when opening an issue — and attach a
`scripts/dump_device.py` capture if you can.

### Transport controls

Play, pause, next and previous are relayed by the speaker to whatever is streaming to
it (Cast, AirPlay, Soundtrack), so they act on the *source*, not the device. Next and
previous appear only while the current source reports that it can skip — the official
app greys its own buttons out the same way. On the analogue and passthrough inputs
there is no session to control, so the media player sits idle.

## Troubleshooting

### Direct connection

| Message | Cause | Fix |
|---------|-------|-----|
| **No UniFi Play devices answered on Home Assistant's subnet** | The broadcast probe got no reply — the speakers are on a different VLAN/subnet, unreachable, or are UPL-PORT hardware (which does not answer the UDP probe) | Enter the speakers' IP addresses in the setup field; each is then probed directly over UDP and, failing that, identified over MQTT (works across VLANs and on Ports). |
| **The addresses you entered answered neither the discovery probe nor an MQTT connection** | Wrong IP, device offline, or a firewall dropping the traffic | Verify the IPs, and allow UDP 10001 (discovery) and TCP 8883 (MQTT) from Home Assistant to the speakers. |

**Audio Port (`UPL-PORT`) owners: always enter the Port's IP address.** Ports have
been reported not to answer the UDP discovery probe at all (issue #5), so automatic
subnet discovery cannot find them — but given an IP, the integration identifies the
device through its MQTT broker instead, which Ports do serve.

To test the UDP discovery probe by hand from any machine that can reach the speaker:

```bash
python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
s.sendto(b'\x01\x00\x00\x00', ('SPEAKER_IP', 10001))
print(s.recvfrom(2048))"
```

Any reply at all means discovery works; a timeout means the IP is wrong or UDP 10001
is blocked.

### Console mode

Setup reports a specific reason for each failure. Find your message below:

| Message | Cause | Fix |
|---------|-------|-----|
| **Could not reach the console** | No HTTP response at all — wrong address, not routable from HA, or a timeout | Enter only the IP or hostname (e.g. `192.168.10.1`), without `https://`. Confirm Home Assistant can reach it. |
| **The console rejected the API key (HTTP 401)** | Purely a credential problem. A 401 means the Apollo route exists and its auth layer turned you away — so Apollo *is* installed | Paste a fresh key from **Settings → Control Plane → API Keys** on this console, whole and untruncated. |
| **The console refused the API key (HTTP 403)** | Key is valid but not for this console, or revoked | API keys are per-console — create the key on the same console you entered. |
| **This console has no Apollo application** | The console answered with its web UI instead of an API, meaning no Apollo route exists | Use **direct connection** instead — some console models never get Apollo ([details](docs/apollo.md)). |
| **Apollo answered but has no device API** | Apollo is installed but does not serve the expected path — a version mismatch | Please open an issue with your console firmware and Apollo version. |
| **That address is Ubiquiti's cloud (ui.com)** | You entered `api.ui.com` or another ui.com address. That is the Site Manager cloud API — a different API that does not proxy Apollo | Enter your console's own local IP or hostname, with a key created on that console. |

Setup succeeds but no devices appear? The API answered with an empty list, so your
address and key are fine — the console just has no Play hardware visible yet. Devices
are re-checked every 5 minutes, so there is no need to reload the integration once
they appear.

To probe the Apollo API by hand (including how to tell "no Apollo" from "bad key"),
see [docs/apollo.md](docs/apollo.md).

For anything else, enable debug logging (**Settings → Devices & services → UniFi Play → ⋮ → Enable debug logging**), retry setup, and share lines containing `custom_components.unifi_play` in a GitHub issue (redact your API key). At debug level the integration logs the exact URL requested, HTTP status, and response body.

## License

[MIT](LICENSE)
