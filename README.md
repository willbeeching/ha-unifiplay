# UniFi Play for Home Assistant

[![CI](https://github.com/willbeeching/ha-unifiplay/actions/workflows/ci.yaml/badge.svg)](https://github.com/willbeeching/ha-unifiplay/actions/workflows/ci.yaml)
[![GitHub Release](https://img.shields.io/github/v/release/willbeeching/ha-unifiplay)](https://github.com/willbeeching/ha-unifiplay/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/willbeeching/ha-unifiplay/blob/master/LICENSE)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![vibe-coded](https://img.shields.io/badge/vibe-coded-ff69b4?logo=musicbrainz&logoColor=white)](https://en.wikipedia.org/wiki/Vibe_coding)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20me%20AI%20tokens-ffdd00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/willbeeching)

A Home Assistant custom integration for **UniFi Play** devices (PowerAmp, In-Wall, etc.) — with or without a UniFi OS Console. Since v1.1.0 the integration can discover and control speakers directly, so it works even on consoles that never get the Apollo application (UCG-Fiber, Cloud Key, …), or with no console at all.

## Features

- **Media player** — volume, mute, now-playing metadata (song, artist, album), cover
  art, and transport control (play / pause / next / previous) while streaming
- **Zones** — create and manage multi-room groups; zone volume, mute, and broadcast
  wired source control via a dedicated zone media player entity; full zone CRUD through
  the integration's Configure button and callable services
- **Selects** — audio input, audio output (Ports), EQ preset, sub phase, channels
  (stereo/mono)
- **Switches** — Dynamic Boost, Dolby Atmos / Equalizer, persistent dashboard
- **Number controls** — balance, volume limit, screen brightness, LED brightness, sub crossover, sub level
- **Text** — LED color (hex)
- **Buttons** — locate (flash LEDs), restart
- **Sensors** — firmware update status, streaming service, alarms, quiet hours, announcements
- **Binary sensors** — announcing, admin lock, in-zone status per device, broadcast wired source active per zone
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
the device value `speakers` is the HDMI eARC input (shown as **eARC**, matching the
Play app), inputs also include USB, and an **Audio Output** select (Line Out / S/PDIF /
USB) exists only on Ports. If you run into device-specific issues, please include the
device platform from the logs when opening an issue — and attach a
`scripts/dump_device.py` capture if you can.

### Transport controls

Play, pause, next and previous are relayed by the speaker to whatever is streaming to
it (Cast, AirPlay, Soundtrack), so they act on the *source*, not the device. Next and
previous appear only while the current source reports that it can skip — the official
app greys its own buttons out the same way. On the analogue and passthrough inputs
there is no session to control, so the media player sits idle.

## Zones

Zones group two or more speakers into a multi-room audio system. One device is the **host** (audio source); the rest are **members** that sync to it. Zones created here appear in the UniFi Play app and vice versa.

### Managing zones

Open **Settings → Devices & Services → UniFi Play → Configure**:

| Action | What it does |
|--------|--------------|
| **Create zone** | Pick two or more speakers. A speaker can only be in one zone at a time, so speakers already in a zone are not offered |
| **Rename / Reorder** | Change the display name or sort index (`group_index`) |
| **Add / Remove member** | Removing the hosting speaker hands the host role to another member rather than failing. A zone needs at least two speakers — to go below that, delete the zone |
| **Set audio source** | **Streaming** lets each device play independently. **Broadcast wired source** sends one speaker's physical input (Line In, S/PDIF / eARC, USB) to every other speaker in the zone — any zone member can be the source, not just the host |
| **Delete zone** | All members return to standalone mode |

> The device protocol uses a replace-all zone write. The integration always preserves sibling zones on the same host, but if you edit zones from the mobile app simultaneously, whichever write lands last wins.

### Entities

Each zone gets a `media_player` entity with volume, mute, and a source selector offering Streaming plus whatever its speakers can broadcast (an Audio Port: eARC / Line In / S/PDIF / USB; a PowerAmp: eARC / Line In). Key state attributes: `group_id`, `group_members` (online member MACs), `wb_enable`, `wb_input`, `host_mac`, `dev_count`. `host_mac` is empty on a freshly created zone — the speakers elect a host themselves shortly after, and it is populated from then on.

Two binary sensors are also created per zone/device:

- **`binary_sensor.<device>_in_zone`** — on when the device is participating in any zone (host or member). Carries a `hosting_group_id` attribute when it is the host. Useful as an automation condition before changing a device's source.
- **`binary_sensor.<zone>_broadcast_wired_source_active`** — on when the zone is broadcasting a speaker's physical input. Attributes: `source_label` (human-readable), `wb_input`, `wb_device_mac`. Installs created before this sensor was renamed keep the original `binary_sensor.<zone>_wideband_active` entity ID; only the display name changed.

### Services

| Service | Key fields |
|---------|------------|
| `unifi_play.create_zone` | `name`, `host_device_id`, `member_device_ids` |
| `unifi_play.delete_zone` | `entity_id` |
| `unifi_play.add_zone_member` | `entity_id`, `device_id` |
| `unifi_play.remove_zone_member` | `entity_id`, `device_id` |
| `unifi_play.rename_zone` | `entity_id`, `name` |
| `unifi_play.set_zone_index` | `entity_id`, `group_index` |
| `unifi_play.play_zone_announcement` | `entity_id`, `filename`, `length` — clip must already be on the host device |
| `unifi_play.stop_zone_announcement` | `entity_id` |

### Automation events

Four events fire on the HA event bus when zone topology changes (use `platform: event` to trigger on them):

| Event | Fires when | Data |
|-------|-----------|------|
| `unifi_play_zone_created` | A zone that did not exist now does | `group_id`, `name`, `host_mac`, `dev_count` |
| `unifi_play_zone_deleted` | A zone no speaker reports any more | `group_id`, `name` |
| `unifi_play_zone_renamed` | A zone's name changed | `group_id`, `name`, `previous_name` |
| `unifi_play_zone_member_changed` | A speaker joined or left a zone | `group_id`, `name`, `added_macs`, `removed_macs` |

**One change fires one event.** Every speaker in a zone reports that zone in
its own state, so a five-speaker zone is described to Home Assistant five
times. The integration merges those into a single view of each zone and fires
events from changes to *that*, so renaming a zone fires one
`unifi_play_zone_renamed` however many speakers are in it. Two consequences
worth knowing:

- A speaker leaving a zone is a `unifi_play_zone_member_changed`, not a
  `unifi_play_zone_deleted`, even though that speaker stops reporting the
  zone entirely. The zone is deleted only when *no* speaker reports it.
- A speaker re-sending its current state — which happens on every reconnect
  and after every edit — fires nothing, and neither does a speaker listing
  the same members in a different order.

**Nothing fires during startup.** Zones that existed before Home Assistant
connected are not new, so the first report from each speaker after a start or
a reload is applied silently. The same applies to a speaker that comes online
later: learning about a zone it was already in is discovery, not a change.
Automations on these events therefore only see changes made while Home
Assistant was running — from the Play app, from another Home Assistant
action, or from the front panel.

An example, on a zone gaining a speaker:

```yaml
automation:
  - alias: "Announce when a speaker joins the downstairs zone"
    triggers:
      - trigger: event
        event_type: unifi_play_zone_member_changed
    conditions:
      - condition: template
        value_template: "{{ trigger.event.data.name == 'Downstairs' }}"
      - condition: template
        value_template: "{{ trigger.event.data.added_macs | length > 0 }}"
    actions:
      - action: notify.persistent_notification
        data:
          message: >-
            {{ trigger.event.data.added_macs | join(', ') }} joined
            {{ trigger.event.data.name }}
```

`added_macs` and `removed_macs` are sorted, so comparing two payloads is
meaningful. MACs are uppercase hex with no separators.

> `host_mac` may be empty in `unifi_play_zone_created` for a zone that has
> just been written, because the host is elected by the speakers rather than
> chosen when the zone is created. Don't rely on it being set; read it from
> the zone entity a moment later if you need it. A host election on its own
> fires no event at all: the zone has not changed, only which speaker is
> carrying an internal role.

If your speakers disagree about a zone for more than a few seconds, the log
records it once (`Speakers disagree about zone …`) and once again when they
converge. Editing the zone from Home Assistant or the Play app rewrites it to
every speaker and resolves it.

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

## Contributing

Pull requests are very welcome — most of what this integration can do came from people testing
against hardware the author doesn't own. If you're adding protocol knowledge, please record how you
verified it in [`docs/api.md`](docs/api.md); a measured value beats a plausible one, and negative
results are worth writing down too.

Set up the hooks once per clone:

```bash
pip install pre-commit && pre-commit install
```

That wires up both stages. **On commit**, black and isort reformat the files you're committing — if
they change anything the commit stops so you can re-stage. **On push**, flake8 and a check that
every call on the MQTT client resolves to a method that actually exists run against the whole
package. That last one exists because v1.2.0 shipped calling two methods that had been deleted, and
nothing in the pipeline could see it: flake8 finds undefined *names*, never a missing *attribute*.

Formatting deliberately runs at commit rather than push. Rewriting files at push time would be
useless — the commits being pushed already contain the unformatted code.

Python 3.13 is needed, matching CI and Home Assistant itself. The same checks run in CI, so hooks
are a convenience rather than the gate.

## Support

This was reverse-engineered and vibe-coded over many late nights, and the AI tokens don't pay for
themselves. If this integration ever saved you reaching for your phone to turn the music down,
consider [buying me some AI tokens](https://buymeacoffee.com/willbeeching) ☕🤖. Entirely optional —
bug reports and stars are appreciated just as much.

## License

[MIT](LICENSE)
