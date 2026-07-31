# Apollo: the console-side UniFi Play application

Everything here applies to **console mode only** — [direct connection](../README.md#configuration)
does not involve Apollo at all. This page preserves the findings from
[#4](https://github.com/willbeeching/ha-unifiplay/issues/4), which is the most complete
public record of how Apollo provisioning works.

## What Apollo is

- `Apollo` is Ubiquiti's internal name for the UniFi Play product line and for the
  console application that watches it. In UniFi OS the section is labelled **Apollo**,
  not "UniFi Play" — only the hardware is branded Play. It serves the
  `/proxy/apollo/api/v1/` API this integration's console mode uses.
- You never install it yourself. It is a separately versioned package that the console
  downloads and installs **automatically when it discovers Play hardware** — `apollo` is
  the only application in UniFi OS's catalogue flagged `installOnDeviceDiscovery: true`.
- Even where it runs, Apollo is a passive observer. On a working console its `/devices`
  list reports speakers as `MANAGED_BY_OTHER` with empty runtime state, and its own
  device database is empty — the speakers are managed by the Play mobile app, and
  control is direct MQTT. This is why the integration can bypass Apollo entirely.

## The install gate: model, not channel

Reported in [#4](https://github.com/willbeeching/ha-unifiplay/issues/4):

| Console | Channel tested | Apollo |
|---|---|---|
| UDM Pro | Early Access | Working |
| UDM Pro | `release-candidate` | Working |
| UCG-Fiber | `release`, then `beta` | Never installs |
| Cloud Key Plus | `beta` | Never installs |

**Changing your update channel is not a workaround — this has been tested.** Two
consoles were moved to `beta`, the highest tier, with Play hardware already adopted, and
Apollo still never installed. The cleanest evidence is a single owner's two consoles on
identical firmware (`v5.1.27`), both on non-Official channels, both with Play hardware:
Apollo runs on the UDM Pro and is entirely absent on the Cloud Key Plus. Model is the
only variable left.

Two conditions must hold before a console fetches Apollo; both are necessary, neither
alone is sufficient:

1. **A Play device is discovered on it.** A UDM Pro on `release-candidate` sat for six
   months logging `Package "apollo" not installed` every boot, then installed Apollo the
   day a Play device appeared.
2. **A package exists for the console's model** at or below its channel
   (`releaseChannel` in `firmware.yaml`, seen in logs as `max_release_channel`). Apollo
   is not distributed through apt — `unifi-core` fetches the `.deb` directly from
   Ubiquiti — so there is no repository you can add.

Caveats learned the hard way:

- `runnables.yaml`'s `releaseChannels:` map is a **per-application channel preference
  you can change**, not a record of where Ubiquiti publishes packages. Do not infer
  availability from it.
- Do not rely on `consoleGroup.yaml` either: its schema differs by platform (application
  catalogue on one UDM Pro, console-group membership on a UCG-Fiber), and it is not the
  install gate.
- Play hardware appearing in the UniFi **Network** device list is independent of Apollo:
  a Cloud Key Plus listed five Audio Ports in Network while never gaining Apollo.
- None of this conflicts with Play being a retail product: the hardware is driven by the
  Play mobile app and needs no console, so Apollo's rollout is invisible to normal
  buyers.

## Checking a console over SSH

This sequence distinguishes *Apollo was never fetched* from *installed but broken*:

```bash
ps auxwww | grep -i apollo                                     # is it running at all?
systemctl list-unit-files | grep -i apollo                     # is there a service?
grep -i apollo /data/unifi-core/logs/uos.log | tail -5         # fetch or version probe
grep -i releaseChannel /data/unifi-core/config/firmware.yaml   # for the record
```

`ERROR Exit with error: Package "apollo" not installed` plus no systemd unit and no
process means the package has never been on the console. Look for a
`Start to download package package_name=apollo` line: its absence means the console
never requested the package; a failed install would instead leave a download or unpack
error.

## Checking the API over HTTP

```bash
curl -k -sS -i \
  -H "X-API-KEY: YOUR_KEY_HERE" \
  -H "Accept: application/json" \
  "https://192.168.10.1/proxy/apollo/api/v1/devices"
```

**Read the `content-type`, not just the status line.** UniFi OS has no route for a
proxy path whose application is not installed, so the request falls through to the web
UI and comes back `200` with an HTML body — even with a perfectly valid key:

| Response | Meaning |
|----------|---------|
| `200` + `application/json` | Working. Apollo installed and the key accepted |
| `200` + `text/html` | **No Apollo application on this console** — that is the web UI |
| `401` + `application/json` | Apollo *is* installed; the key was rejected |
| `404` + `text/plain` | Apollo is installed but has no such path |

Note the header must be `X-API-KEY: <key>` — `curl` silently ignores a `-H` value with
no colon in it, which looks exactly like an auth failure.

If Apollo never appears on your console, the answer is not configuration — use the
integration's **direct connection mode**, which needs none of this. If you have a data
point to add (console model, channel, whether Apollo installed), please post it in
[#4](https://github.com/willbeeching/ha-unifiplay/issues/4).
