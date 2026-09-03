# Verification

What was actually run, and what was not. The distinction matters more here
than in most repositories: there is no public protocol documentation for
UniFi Play, so a claim that something works is either backed by a test, by a
static guard, or by a reading taken off real hardware. Nothing in this
repository should assert protocol behaviour on any other basis.

## The lanes

Both run in CI on every push and pull request, and both have to be green.

| Lane | Home Assistant | Python | Requirements |
|---|---|---|---|
| minimum | 2025.8.0 | 3.13 | `requirements_test_min.txt` |
| latest | 2026.9.0 | 3.14 | `requirements_test_latest.txt` |

The minimum is the floor declared in `hacs.json`.
`scripts/check_min_ha_version.py --expect-min` fails the build if the two ever
disagree, because a floor nobody runs against is not a floor.

Three bugs were only visible on one lane or the other:

- An `asyncio.Event.set()` from paho's callback thread. Home Assistant's test
  harness runs the loop in debug mode, where `call_soon` from the wrong thread
  raises; the latest lane hung on every test until the callback was routed
  through `call_soon_threadsafe`. It was a production bug, not a test one.
- `device_registry.async_get_device` raises on 2026.9 and its replacement does
  not exist on 2025.8. Neither call is usable across the supported range;
  walking `async_entries_for_config_entry` is.
- `registry.devices` iterates ids on one release and entries on the other.

## The matrix

Run all of it before tagging a release.

| Check | Command |
|---|---|
| Tests, minimum lane | `pytest` |
| Tests, latest lane | `pytest` against `requirements_test_latest.txt` |
| Per-module coverage | `pytest --cov --cov-report=json:coverage.json && python scripts/check_coverage.py coverage.json` |
| Strict typing | `mypy custom_components/unifi_play` |
| Formatting and lint | `black --check .`, `isort --check-only .`, `ruff check .`, `flake8` |
| Import smoke, both lanes | `python scripts/check_min_ha_version.py --expect-min` |
| MQTT client calls resolve | `python scripts/check_mqtt_client_calls.py` |
| Certificate-generation fallback | `python scripts/check_mqtt_cert_generations.py` |
| Every `tls_set` uses the bundled pairs | `python scripts/check_cert_generation_users.py` |
| `set_groups` carries no firmware-owned keys | `python scripts/check_set_groups_payload.py` |
| No expression reaches a shell script | `python scripts/check_workflow_injection.py` |
| Release archive builds and verifies | `python scripts/build_release_archive.py` |

The coverage gate is per module, not just overall: 95% for every module and
100% for `config_flow.py`. A single repository-wide number is comfortably
reachable with the largest and most branch-heavy file barely touched.

## What tests cannot reach

The suite replaces exactly three things: paho's `Client`, the UDP discovery
socket, and the console's HTTP API. Everything above those runs for real,
including the BinMe framing, the certificate-generation fallback and the
coordinator's dispatch. What it cannot do is tell you whether a value the
firmware accepts actually does anything.

That gap is not theoretical. The PowerAmp accepts `spdif` as an input, echoes
it back, and routes no audio to it, because the amp has no optical jack. A
test asserting the publish would have passed. Only listening to the speaker
caught it.

So the checklist below is not a formality, and no release should go out
without it. Record the model and firmware version alongside each result; a
pass on `UPL-AMP` 1.0.38 says nothing about `UPL-PORT` 1.1.10.

## Hardware smoke test

Run on both models. Note the firmware version of each first
(**sensor.\<name\>_firmware_status**, or the Play app).

### Setup

1. Add the integration in direct mode with no manual hosts. PowerAmps on the
   same subnet should appear on their own.
2. Add the Audio Port's IP address. It does not answer the UDP probe (#5) and
   is identified over MQTT instead.
3. If you have a console with Apollo, add a second Home Assistant instance in
   console mode and confirm the same speakers appear. Two entries on one
   instance covering the same speakers should be refused.
4. Console mode: leave certificate verification on and confirm the flow
   reports `certificate_untrusted` rather than a connection failure, then
   clear the box and confirm setup completes.

### Per speaker

| Check | Expected |
|---|---|
| Volume, mute | Audible, and the app agrees |
| Every input on the **Audio Input** select | Audio actually arrives from that jack. Listen; do not read the state back |
| eARC specifically | `speakers` on both models. This is the one that has been wrong twice |
| Transport controls while streaming | Act on the source. Next and previous appear only when the source says it can skip |
| EQ preset, then a band | The app shows the same |
| Locate, restart | LEDs flash; the speaker comes back and its entities return |
| Announcement with no length given | Plays to the end |
| Pull power, wait 30s, restore | Entities go unavailable, then come back with no restart |

### Zones

| Check | Expected |
|---|---|
| Create a zone of two from **Configure** | Appears in the Play app, both speakers play in sync |
| Rename, reorder | The app agrees |
| Add a third speaker, then remove it | The zone survives, and the app agrees |
| Remove the hosting speaker | The host role moves rather than the action failing |
| Broadcast a wired source from each member | Audio from that member's jack reaches the others |
| Try an input the source speaker lacks (USB on an amp) | Refused, with nothing published |
| Power off one member, then edit the zone | Refused, naming that speaker, and nothing changes on the others |
| Bring it back and repeat the edit | Succeeds |
| Zone volume and mute | Every member moves together |
| Delete the zone | Every member returns to standalone in the app |
| Create a zone from the Play app while Home Assistant is running | One `unifi_play_zone_created` event, not one per speaker |
| Restart Home Assistant with zones present | No zone events at all, and no zone entity is destroyed |

### Firmware rotation

If you have a speaker on firmware older than 1.0.41 and can update it, do the
update with the integration running. The entities should go unavailable, a
repair issue may appear, and the speaker should reconnect on its own within
about five minutes as the poll rebuilds the client and re-probes both bundled
certificate generations. Record what happened either way.

## Recording a result

Add anything you learn about the protocol to `docs/api.md` with the model,
firmware version and method. Negative results belong there too: "these six
values were published and ignored" is what stops the next person deriving it
again.
