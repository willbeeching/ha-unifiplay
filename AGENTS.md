# AGENTS.md

Guidance for coding agents working in this repository. Humans should read
[`README.md`](README.md) for what the integration does and
[`docs/api.md`](docs/api.md) for the protocol.

## What this is

A Home Assistant custom integration (`domain: unifi_play`) for UniFi Play
speakers — PowerAmp (`UPL-AMP`) and Audio Port (`UPL-PORT`). It talks to each
speaker directly over MQTT on TCP 8883 using the certificate the official
mobile app uses, and optionally discovers devices through a UniFi OS console's
Apollo REST API.

**There is no public protocol documentation.** Everything in `docs/api.md` was
reverse-engineered from packet captures and from publishing values at real
hardware and reading back what the device reported. That single fact drives
most of the rules below.

## The rule that matters most

**Never invent a protocol value, and never "correct" one that looks wrong.**

This has cost real debugging time twice, in both directions:

- `speakers` is the **HDMI eARC input**, not a speaker-level output. Labelled
  "Speakers" from the name alone, it hid eARC from the Audio Port entirely.
- The PowerAmp's eARC was then assumed to be `spdif`. The device *accepts*
  `spdif` and echoes it back, so it looked right — but an amp has no optical
  jack, so selecting eARC reported success and routed no audio.

Both were plausible guesses. Both were wrong, and both failed silently. If you
do not have a captured request/response or a measured read-back for a value,
say it is unverified, in the code and in the docs. An honest gap is cheap; a
confident wrong value is not.

When you do verify something, record **how** in `docs/api.md` — model, firmware
version, and method. Negative results belong there too: "these six values were
published and ignored" is what stops the next person re-deriving it.

## Layout

| File | Role |
|---|---|
| `coordinator.py` | `DataUpdateCoordinator`; owns device state and one MQTT client per device |
| `mqtt_client.py` | The wire protocol: BinMe framing, publish/subscribe, one method per action |
| `api.py` | Apollo REST client (console mode) |
| `discovery.py` | UDP broadcast + unicast probing (direct mode) |
| `entity.py` | `UnifiPlayEntity` base — availability, device info, `_require_mqtt()` |
| `config_flow.py` | Setup flow, plus the whole zone-management options flow |
| `services.py` | Service handlers; `helpers.py` holds logic shared with the config flow |
| `const.py` | Source/label maps, zone constants. Read its module docstring first |
| `<platform>.py` | Entity platforms, each an `EntityDescription` list plus a factory |

## Commands

```bash
pip install -r requirements_test_min.txt       # minimum lane: HA 2025.8.0
pytest                                          # the suite
pytest --cov --cov-report=json:coverage.json && python scripts/check_coverage.py coverage.json
mypy custom_components/unifi_play                # strict, against real HA types
ruff check . && black --check . && isort --check-only . && flake8

pip install pre-commit && pre-commit install    # once per clone
pre-commit run --all-files --hook-stage pre-commit   # black, isort, ruff, json/yaml
pre-commit run --all-files --hook-stage pre-push     # flake8, guards, coverage

python scripts/check_mqtt_client_calls.py       # the guard on its own
python scripts/check_mqtt_cert_generations.py   # cert-generation fallback, paho stubbed
python scripts/check_min_ha_version.py --expect-min   # import smoke + floor check
python scripts/check_workflow_injection.py      # no ${{ }} reaches a shell script
python scripts/check_quality_scale.py           # tracker matches the official rules
python scripts/build_release_archive.py         # the release zip, reproducibly
python scripts/dump_device.py <DEVICE_IP>       # dump a device's live state
```

Release and hardware verification, including the smoke test to run on both
models before tagging, is in [`docs/verification.md`](docs/verification.md).
`custom_components/unifi_play/quality_scale.yaml` records where the
integration stands against Home Assistant's quality scale, and what is not
met. `scripts/check_quality_scale.py` runs in CI and on push: a missing
official rule, an unknown one, an exemption with no reason, or a `done`
with no supporting comment fails the build. The manifest does not claim a
tier; this is a self-assessment for a custom integration.

Two supported lanes, both run in CI and both must stay green:

| Lane | Home Assistant | Python | Requirements |
|---|---|---|---|
| minimum | 2025.8.0 (the floor in `hacs.json`) | 3.13 | `requirements_test_min.txt` |
| latest | whatever `requirements_test_latest.txt` pins | 3.14 | `requirements_test_latest.txt` |

Changing the minimum means changing `hacs.json`, the README and
`requirements_test_min.txt` together — `scripts/check_min_ha_version.py`
fails the build if the declared floor and the tested release disagree.

## Testing

There is a suite now, and three seams, and no others:

- **`paho.mqtt.client.Client`** — replaced by `tests/fake_mqtt.py`. Binme
  framing, the certificate-generation fallback, CONNACK handling and the
  coordinator's dispatch all run for real above it.
- **`discovery.async_discover`** — the UDP socket. `async_resolve_direct` and
  the MQTT identification fallback still run for real.
- **`aioclient_mock`** — Home Assistant's own aiohttp mocker, standing in for
  a console's Apollo API.

Reach for `patch.object` on an integration method and you are testing the
patch: both bugs this repository is named for — a deleted client method, a
zone written to one device — would have survived that.

Captured payloads live in `tests/fixtures/` with provenance recorded in
`tests/fixtures/PROVENANCE.md`: model, firmware, capture source, and which
parts of the meaning were verified on hardware rather than merely observed.
**Do not invent a fixture to make a test pass.** A payload made up to fit an
assertion encodes a guess as a fact and then defends it.

Coverage is gated per module, not just overall: 95% for every production
module, 100% for `config_flow.py`. Both are enforced in CI and by the
pre-push hook, and a module that drops below fails the build.

"Verified" still means one of:

1. **Against hardware** — state a model and firmware version.
2. **Statically** — the guard scripts, or reasoning from a captured payload
   in `docs/api.md`.
3. **By test** — name the test.

Do not claim a change is tested because it lints, and do not claim hardware
verification a test gave you. If you could not exercise something, say so
plainly and say what would exercise it.

## Traps that have caused shipped bugs

**Client method drift.** v1.2.0 shipped calling two `mqtt_client` methods that
had been deleted in an unrelated refactor. Connecting raised `AttributeError`,
the error path discarded the client, and every device was left permanently
without state. `scripts/check_mqtt_client_calls.py` now catches this and runs
in CI and on push. If you add an accessor returning a client, annotate it
`-> UnifiPlayMqttClient` or the guard cannot see through it.

**`set_groups` is replace-all, per device, and does not propagate.** Every
device holds a copy of every zone. Writing to one device leaves the others
serving stale copies that then compete on merge. Every mutation goes through
`coordinator.zones` (`zone_writer.py`), which preflights every required
speaker, writes to all of them or none, and returns a result. Do not publish
`set_groups` from anywhere else.

**Fields that only appear while true.** `hosting_group` and `sync_devices` are
sent in `info` only while set, so a device leaving a zone simply stops sending
them and the last value stands forever. Derive membership from zone state, never
from these. The same shape burned the `In Zone` sensor.

**Unique IDs are MAC-based, not per-entry** (`unifi_play_<MAC>_<key>`). Two
config entries covering the same speaker mint colliding IDs; Home Assistant
rejects the later ones, and which entry loses is a startup race. Rejected
entities keep their registry rows and read `unavailable` forever.
`_entry_already_covering()` in `config_flow.py` blocks this at setup.
A console created while Apollo listed nothing is still a valid entry, so
the coordinator runs the same MAC check on every discovery poll and
refuses speakers another loaded entry already owns.

**A registered client is not a connected one.** The coordinator inserts the
client before dialling out, and `publish_action` drops commands silently while
disconnected (returning `False`, which the zone write path checks). Check
`is_connected`, not presence — that distinction is why `_require_mqtt()` and
`_connected_mqtt()` are separate. There is a third state: `is_retrying`, which
means the client is working on getting itself back and must be left alone.

**The MQTT network loop runs on paho's own thread**, started with
`loop_start()`. It must never go back to an executor: `client.loop()` in a
task holds one Home Assistant executor worker permanently per speaker, out of
a pool everything else shares. Every callback crosses back to the event loop
explicitly — `asyncio.Event` through `_signal`, everything else through the
coordinator's `call_soon_threadsafe`. Touching an asyncio primitive directly
from a paho callback deadlocks under Home Assistant's debug loop, which is
what the test harness runs.

**`${{ }}` in a `run:` block is not a variable.** GitHub substitutes it as
text before bash parses the line, so a workflow-dispatch input, a branch name
or a tag becomes source code in a job that, for the release workflow, holds a
token with write access to this repository. Bind the value to `env:` and read
`"$NAME"`. `scripts/check_workflow_injection.py` enforces this and runs in CI.

**Commands must fail loudly.** Entity commands used to no-op when disconnected
and report success, which made a dead connection look like a broken service
layer. Raise `HomeAssistantError` via `_require_mqtt()`; never swallow.

**Home Assistant and the Play app are equal peers with no locking.** Whichever
writes last wins. A zone edited from HA while the app sits on a zone screen will
often revert. This is a protocol limitation, not a bug to fix.

## Home Assistant specifics

- **Reloading a config entry does not reload code.** Python has already
  imported the modules; only a full restart picks up changed files. Several
  hours of issue #14 were this. Always say "restart", never "reload", when
  telling someone to pick up new code.
- Entities extend `UnifiPlayEntity`; availability already accounts for a live
  connection, so do not re-implement it per platform.
- New services need all four kept in sync: the handler, `services.yaml`,
  `strings.json`, and `translations/en.json`. Ten services once shipped with no
  names at all, appearing in the UI as bare keys.
- `iot_class` is `local_push`: state arrives via MQTT events. The coordinator's
  poll only discovers devices.
- Console TLS verification is per entry (`CONF_VERIFY_SSL`), offered on for a
  new entry and defaulting to off for one created before the option existed.
  Home Assistant caches one shared session per `verify_ssl` value, so asking
  for the right one is the whole of the choice; never build a session here.

## Conventions

- **Comments explain why, not what.** The existing code is heavily commented
  with the reasoning and the evidence behind non-obvious choices, frequently
  citing issue or PR numbers. Match that; it is the repository's main defence
  against someone "simplifying" a hard-won workaround.
- **Commit messages carry the reasoning** — what broke, how it was diagnosed,
  what was verified and what wasn't. Look at recent history before writing one.
- Reference issues and PRs by number; they are the audit trail for protocol
  decisions.
- Keep per-platform maps separate when models genuinely differ. `const.py`
  explains why the source maps must never be merged.

## Do not

- Guess a protocol value, or "fix" an odd-looking one without evidence.
- Claim hardware verification you did not do.
- Commit or push unless asked. Branch rather than committing to `master`.
- Add a dependency: `paho-mqtt` is the only one, and HACS users pay for each.
- Reformat or restructure code you were not asked to touch — it buries the
  change under noise and the comments are load-bearing.
