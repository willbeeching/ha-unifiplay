# UniFi Play for Home Assistant

[![CI](https://github.com/willbeeching/ha-unifiplay/actions/workflows/ci.yaml/badge.svg)](https://github.com/willbeeching/ha-unifiplay/actions/workflows/ci.yaml)
[![GitHub Release](https://img.shields.io/github/v/release/willbeeching/ha-unifiplay)](https://github.com/willbeeching/ha-unifiplay/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/willbeeching/ha-unifiplay/blob/master/LICENSE)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![vibe-coded](https://img.shields.io/badge/vibe-coded-ff69b4?logo=musicbrainz&logoColor=white)](https://en.wikipedia.org/wiki/Vibe_coding)

A Home Assistant custom integration for **UniFi Play** devices (PowerAmp, In-Wall, etc.) managed by a UniFi OS Console (UDM Pro, Cloud Gateway, etc.).

## Features

- **Media player** — volume, mute, now-playing metadata (song, artist, album)
- **Selects** — audio input (Streaming / HDMI eARC / Line In), EQ preset, sub phase, channels (stereo/mono)
- **Switches** — Dynamic Boost, Dolby Atmos / Equalizer, persistent dashboard
- **Number controls** — balance, volume limit, screen brightness, LED brightness, sub crossover, sub level
- **Text** — LED color (hex)
- **Buttons** — locate (flash LEDs), restart
- **Sensors** — firmware update status
- **Real-time state** via direct MQTT connection to each device

## Requirements

- A UniFi OS Console that has surfaced the **UniFi Play** application. Play is not
  something you install — the console adds it by itself once it detects Play
  hardware on the network. That application's service (internally `apollo`) is what
  this integration talks to, so no Play application means no API to connect to.
- One or more UniFi Play **hardware devices** (PowerAmp, In-Wall Port, etc.) on the
  same network as the console
- An API key created on that same console (**Settings → Control Plane → API Keys**)

> **Not every console appears to offer Play.** A Cloud Key Plus has been reported to
> list Audio Ports in UniFi Network while never surfacing the Play application at all
> ([#4](https://github.com/willbeeching/ha-unifiplay/issues/4)). Devices showing up in
> UniFi Network is not the same thing as Play being available. If the API returns 401
> on your console, this is the most likely reason.

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

### 1. Create a UniFi API key

1. Log in to your UniFi OS Console (e.g. `https://10.0.0.1`)
2. Navigate to **Settings → Control Plane → API Keys**
3. Click **Create API Key**, give it a name (e.g. "Home Assistant"), and copy the key
4. Keep this key safe — you won't be able to view it again

### 2. Add the integration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **UniFi Play**
3. Enter your UniFi OS Console IP address or hostname only (e.g. `10.0.0.1` — do not include `https://`) and the API key from step 1
4. Devices will be discovered automatically

## How it works

The integration uses two communication channels:

- **REST API** on the UDM Pro (`/proxy/apollo/api/v1/`) for device discovery
- **MQTT** (port 8883, mTLS) directly to each device for real-time state updates and control

All communication stays local on your network.

## Device support

| Platform | Device | Tested |
|----------|--------|--------|
| `UPL-AMP` | PowerAmp | Yes |
| `UPL-PORT` | In-Wall Port | Community-reported, not hardware-tested by maintainer |

Both device types use the same Apollo REST discovery and MQTT control paths. If you run into device-specific issues (for example on a Port), please include the device platform from the logs when opening an issue.

## Troubleshooting

Setup reports a specific reason for each failure. Find your message below:

| Message | Cause | Fix |
|---------|-------|-----|
| **Could not reach the console** | No HTTP response at all — wrong address, not routable from HA, or a timeout | Enter only the IP or hostname (e.g. `192.168.10.1`), without `https://`. Confirm Home Assistant can reach it. |
| **The console rejected the request (HTTP 401)** | Key is wrong or truncated — *or* the console has not surfaced the Play application, so there is no service behind the proxy path | Paste a fresh key from **Settings → Control Plane → API Keys**. If the key is definitely right, check the console shows a **UniFi Play** section at all; if you have only just put Play hardware on the network, reboot the console so it can detect it. See Requirements above — some consoles never surface Play. |
| **The console refused the API key (HTTP 403)** | Key is valid but not for this console, or revoked | API keys are per-console — create the key on the same console you entered. |
| **Does not serve the UniFi Play API (HTTP 404)** | The console answered, but there is no Apollo API at `/proxy/apollo/api/v1` | Check you have Play hardware adopted, and that you're pointing at the console that adopted it — not another console on the network. |

Setup succeeds but no devices appear? The API answered with an empty list, so
your address and key are fine — there is just no Play hardware visible to that
console yet. The logs will show `returned no Play devices`. Adopt your hardware
(rebooting the console if it does not show up) and it will be picked up within
five minutes — the integration re-checks for newly adopted devices on that
interval, so there is no need to reload it.

To check the API by hand from any machine that can reach the console:

```bash
curl -k -sS -i \
  -H "X-API-KEY: YOUR_KEY_HERE" \
  -H "Accept: application/json" \
  "https://192.168.10.1/proxy/apollo/api/v1/devices"
```

The status line tells you which row above applies. Note the header must be
`X-API-KEY: <key>` — `curl` silently ignores a `-H` value with no colon in it.

For anything else, enable debug logging (**Settings → Devices & services → UniFi Play → ⋮ → Enable debug logging**), retry setup, and share lines containing `custom_components.unifi_play` in a GitHub issue (redact your API key). At debug level the integration logs the exact URL requested, HTTP status, and response body.

## License

[MIT](LICENSE)
