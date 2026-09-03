# Fixture provenance

Every payload in this directory is either a capture from real hardware or a
faithful reshaping of one. There is no public protocol documentation for
UniFi Play, so a fixture invented to make a test pass would encode a guess as
a fact and then defend it — the exact failure mode `AGENTS.md` opens with.

Each file below records: **device model**, **firmware version**, **capture
source**, and **which parts of its meaning were verified on hardware** as
opposed to merely observed on the wire.

"Observed" means the field appeared in a capture. "Verified" means the value
was set deliberately and read back, or the physical effect was confirmed.
Only verified claims may be relied on by production code.

---

## `apollo_devices.json`

- **Source:** `docs/api.md` § "REST API (via UDM Pro)" → `GET /devices`,
  captured from a UDM Pro running Apollo 0.7.4.
- **Model / firmware:** UPL-AMP, firmware 1.0.38.
- **Verified:** the envelope shape (`err` / `type` / `data` / `offset` /
  `limit` / `total`), and that `platform` at the top level carries the model
  while `extra_info.platform` is an empty string in practice.
- **Observed only:** the `state` value `MANAGED_BY_OTHER`. The full set of
  states is unknown, so nothing filters on it.
- **Note:** MACs and IPs here are documentation values, not a real site.

## `apollo_devices_no_ip.json`

- **Source:** same endpoint, from a console on a firmware that stopped
  populating `ip` in the Apollo response.
- **Model / firmware:** UPL-PORT, firmware 1.1.10.
- **Verified:** that `ip` can be absent, which is why `get_devices()` enriches
  from the Network API's `/stat/sta`.

## `network_clients.json`

- **Source:** `GET /proxy/network/api/s/default/stat/sta` on the same UDM Pro.
- **Verified:** that a Play speaker appears as an ordinary network client and
  that `ip` (or `last_ip` when the lease has expired) carries its address.

## `mqtt_info_amp.json`

- **Source:** `docs/api.md` § "Event Messages" → `info`.
- **Model / firmware:** UPL-AMP, firmware 1.0.38.
- **Verified on hardware:** `volume`, `source`, `muted`, `vol_limit`,
  `balance`, `loudness`, `subwoofer`, `led_brightness`, `screen_brightness`.
  Each was set from the Play app and read back.
- **Observed only:** `soundtrack_paired`, `space`, `tz`, `locating`.
- **Not present when false:** `hosting_group`, `sync_devices`,
  `wb_broadcasting`, `temp_volume`, `announcing`. The device stops sending
  these rather than sending a false value, which is why membership is derived
  from zone state and never from `info`.

## `mqtt_info_port.json`

- **Model / firmware:** UPL-PORT, firmware 1.1.10.
- **Verified on hardware:** `source` accepting `spdif` and `usb`, which a
  PowerAmp does not offer. `speakers` is the HDMI eARC input on both models —
  set in the app and read back; see `const.py`'s module docstring for the two
  bugs the plausible-sounding name has caused.

## `mqtt_groups_two_devices.json`

- **Source:** `docs/api.md` § "Zones (groups)".
- **Model / firmware:** UPL-PORT, firmware 1.1.10, captured across five
  speakers.
- **Verified on hardware:**
  - `host` is written by the *firmware*, not the writer. A zone created from
    the Play app carries no host at creation and names one on a later read.
  - the top-level `timestamp` is present; a per-group `timestamp` is not
    echoed back inside a group, only accepted on write.
  - every member reports the zone in its own `groups` list, so the same
    logical zone arrives once per connected speaker.

## `mqtt_metadata.json`

- **Model / firmware:** UPL-AMP, firmware 1.0.38, Spotify Connect session.
- **Verified on hardware:** `prev` / `next` track the current source's
  capabilities — the official app greys its buttons out on the same flags.
- **Observed only:** `cover_path` is a fixed path whose *contents* are swapped
  per track, which is why `media_image_hash` is seeded from the track
  identity rather than the URL.

## `mqtt_equalizer.json`

- **Model / firmware:** UPL-AMP, firmware 1.0.38.
- **Verified on hardware:** the ten band labels, and that the neutral value is
  `0.01` rather than `0` in the device's own data.
- **Observed only:** the `custom_presets` shape. A populated → empty
  transition has been seen once in the field after an unattended reboot;
  cause undetermined.

## `mqtt_extra_info_port.json`

- **Model / firmware:** UPL-PORT, firmware 1.1.10.
- **Verified on hardware:** a device identified over MQTT alone is known only
  by its topic root (`UPL-DEVICE` for Port hardware), and `extra_info` is what
  upgrades it to the real platform and version.
