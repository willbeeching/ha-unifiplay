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

- A UniFi OS Console running the **Apollo** application. This is the part people get
  stuck on, so in detail:
  - `Apollo` is Ubiquiti's name for the UniFi Play product line, and the application
    that manages it. In UniFi OS the section is labelled **Apollo**, not "UniFi Play"
    — only the hardware is branded Play. It serves the `/proxy/apollo/api/v1/` API
    this integration uses.
  - You never install it yourself. It is a real, separately versioned package that the
    console downloads and installs **automatically when it discovers UniFi Play
    hardware**, then keeps up to date on its own schedule.
  - So a console that has never seen a Play device has no Apollo application, and
    therefore no API to connect to.
- One or more UniFi Play **hardware devices** (PowerAmp, Play Audio Port) discovered by
  that console
- An API key created on that same console (**Settings → Control Plane → API Keys**)

> ### When a console never installs Apollo
>
> **Every console observed with Apollo working has been on a non-Official update
> channel**, and every console observed without it has been on Official
> ([#4](https://github.com/willbeeching/ha-unifiplay/issues/4)):
>
> | Console | Channel | Apollo |
> |---|---|---|
> | UDM Pro | Early Access | Working |
> | UDM Pro | `release-candidate` | Working |
> | UCG-Fiber | Official | Never installs |
> | Cloud Key Plus | Official | Never installs |
>
> That is four data points, not proof, and no console on Official has yet been seen with
> Apollo. Treat it as the first thing to check, not a settled rule — at least one further
> console was reported working with a Play Audio Port in
> [#3](https://github.com/willbeeching/ha-unifiplay/issues/3), and neither its model nor
> its channel was ever recorded.
>
> None of this contradicts UniFi Play being generally available at retail: Play hardware
> is managed through the Play mobile app and needs no console at all, so an
> Early-Access-only Apollo breaks nothing for an ordinary buyer. Apollo is the
> console-side application layered on top, and it is what this integration needs.
>
> Availability also looks **staged per model**, recorded on the console itself.
> `/data/unifi-core/config/runnables.yaml` holds a `releaseChannels:` map naming the
> channel Apollo is published at *for that console*, and the observed values differ:
> `release-candidate` on a UDM Pro, `beta` on a UCG-Fiber. So the channel your console
> needs may be higher than someone else's.
>
> Two conditions must both hold before a console fetches Apollo:
>
> 1. **An Apollo-line device is discovered on it.** `apollo` is the only application in
>    UniFi OS's catalogue flagged `installOnDeviceDiscovery: true`, and that discovery
>    is what triggers the fetch.
> 2. **A published package exists at or below the console's own channel**
>    (`releaseChannel` in `firmware.yaml`, seen in the logs as `max_release_channel`).
>
> Neither alone is enough. A UDM Pro on `release-candidate` sat for six months logging
> `Package "apollo" not installed` on every boot, then installed Apollo the day an
> Apollo device appeared.
>
> If Apollo never installs, compare the two values:
>
> ```bash
> grep -i releaseChannel /data/unifi-core/config/firmware.yaml      # what this console accepts
> grep -A10 releaseChannels /data/unifi-core/config/runnables.yaml  # where apollo is published
> ```
>
> If Apollo's channel sits above your console's, the package has not been released for
> your model at your channel yet. Note the UI's channel names and the on-disk values are
> not the same words, so check `firmware.yaml` rather than trusting the dropdown — and
> if Apollo is published at `beta` on your model, Release Candidate may not be far
> enough.
>
> Raising the console's channel is the available workaround, but it puts the whole
> console on pre-release firmware — a real cost on a gateway that routes your household,
> and worth weighing against simply waiting for Apollo to reach your model's Official
> channel. There is no repository to add instead: Apollo is not distributed through apt,
> and `unifi-core` fetches the `.deb` directly from Ubiquiti.
>
> Please add your console model, channel, and whether Apollo installed to
> [#4](https://github.com/willbeeching/ha-unifiplay/issues/4) — the table above is small
> and every data point sharpens it.

> **Devices in UniFi Network are a separate matter.** A Cloud Key Plus has been
> reported listing five Audio Ports in UniFi Network while never gaining an Apollo
> application. Conversely, an Apollo device in state `MANAGED_BY_OTHER` does **not**
> appear in the Network app's device list at all, yet is fully visible to Apollo. Neither
> list tells you whether Apollo is installed.

### Checking a console over SSH

If you have console access, this sequence distinguishes *Apollo was never fetched* from
*Apollo is installed but broken*:

```bash
grep -i releaseChannel /data/unifi-core/config/firmware.yaml   # the usual culprit
grep -i apollo /data/unifi-core/logs/uos.log | tail -5         # fetch or version probe
systemctl list-unit-files | grep -i apollo                     # is there a service?
```

`ERROR Exit with error: Package "apollo" not installed` in `uos.log`, plus no systemd
unit, means the package has never been on the console. Look for whether a
`Start to download package package_name=apollo` line appears anywhere in that log: its
absence means the console never requested the package, which is what an Official-channel
console does. An install that was attempted and failed would instead leave a download or
unpack error.

Do not rely on `/data/unifi-core/config/consoleGroup.yaml`. It carries an
`applications:` block on a UDM Pro at `5.1.26` and only console *group* membership on a
UCG-Fiber at `5.1.19`, and it is not the install gate either way — the UDM Pro installed
Apollo while `role: UNADOPTED` with `apollo.owned: false`.

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
| **The console rejected the API key (HTTP 401)** | Purely a credential problem. A 401 means the Apollo route exists and its auth layer turned you away — so Apollo *is* installed | Paste a fresh key from **Settings → Control Plane → API Keys** on this console, whole and untruncated. |
| **The console refused the API key (HTTP 403)** | Key is valid but not for this console, or revoked | API keys are per-console — create the key on the same console you entered. |
| **This console has no Apollo application** | The console answered with its web UI instead of an API, meaning no Apollo route exists | Confirm this is the console your Play hardware is adopted to, and if the hardware is new give it time to be discovered (a reboot forces the issue). Some console models do not get Apollo at all on their current update channel — see [When a console never installs Apollo](#when-a-console-never-installs-apollo). |
| **Apollo answered but has no device API** | Apollo is installed but does not serve the expected path — a version mismatch | Please open an issue with your console firmware and Apollo version. |
| **That address is Ubiquiti's cloud (ui.com)** | You entered `api.ui.com` or another ui.com address. That is the Site Manager cloud API — a different API that does not proxy Apollo | Enter your console's own local IP or hostname, with a key created on that console. |

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

**Read the `content-type`, not just the status line.** UniFi OS has no route for a
proxy path whose application is not installed, so the request falls through to the web
UI and comes back `200` with an HTML body — even with a perfectly valid key. A status
code on its own cannot tell "Apollo missing" from "Apollo working":

| Response | Meaning |
|----------|---------|
| `200` + `application/json` | Working. Apollo installed and the key accepted |
| `200` + `text/html` | **No Apollo application on this console.** You are looking at the UniFi OS web UI, not an API |
| `401` + `application/json` | Apollo *is* installed; the key was rejected |
| `404` + `text/plain` | Apollo is installed but has no such path |

Note the header must be `X-API-KEY: <key>` — `curl` silently ignores a `-H` value with
no colon in it, which looks exactly like an auth failure.

For anything else, enable debug logging (**Settings → Devices & services → UniFi Play → ⋮ → Enable debug logging**), retry setup, and share lines containing `custom_components.unifi_play` in a GitHub issue (redact your API key). At debug level the integration logs the exact URL requested, HTTP status, and response body.

## License

[MIT](LICENSE)
