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
pip install pre-commit && pre-commit install   # once per clone
pre-commit run --all-files --hook-stage pre-commit   # black, isort, json/yaml
pre-commit run --all-files --hook-stage pre-push     # flake8, client-call check
python scripts/check_mqtt_client_calls.py            # the guard on its own
python scripts/check_mqtt_cert_generations.py        # cert-generation fallback, paho stubbed
python scripts/dump_device.py <DEVICE_IP>            # dump a device's live state
```

Python 3.13, matching CI and Home Assistant. `__init__.py` uses `type X = Y`,
so older interpreters cannot parse the package at all.

## There is no test suite

Nothing is mocked and there are no unit tests. "Verified" here means one of:

1. **Against hardware** — state a model and firmware version.
2. **Statically** — `scripts/check_mqtt_client_calls.py`,
   `scripts/check_mqtt_cert_generations.py`, or reasoning from a captured
   payload in `docs/api.md`.

Do not claim a change is tested because it lints. If you could not exercise
something, say so plainly and say what would exercise it — the git history is
full of contributors doing exactly that, and it is what makes review possible.

Be especially careful with `config_flow.py`: it is the largest module, drives
all zone management, and has no coverage of any kind.

## Traps that have caused shipped bugs

**Client method drift.** v1.2.0 shipped calling two `mqtt_client` methods that
had been deleted in an unrelated refactor. Connecting raised `AttributeError`,
the error path discarded the client, and every device was left permanently
without state. `scripts/check_mqtt_client_calls.py` now catches this and runs
in CI and on push. If you add an accessor returning a client, annotate it
`-> UnifiPlayMqttClient` or the guard cannot see through it.

**`set_groups` is replace-all, per device, and does not propagate.** Every
device holds a copy of every zone. Writing to one device leaves the others
serving stale copies that then compete on merge. Use
`coordinator.publish_zones()`, which fans out to all of them.

**Fields that only appear while true.** `hosting_group` and `sync_devices` are
sent in `info` only while set, so a device leaving a zone simply stops sending
them and the last value stands forever. Derive membership from zone state, never
from these. The same shape burned the `In Zone` sensor.

**Unique IDs are MAC-based, not per-entry** (`unifi_play_<MAC>_<key>`). Two
config entries covering the same speaker mint colliding IDs; Home Assistant
rejects the later ones, and which entry loses is a startup race. Rejected
entities keep their registry rows and read `unavailable` forever.
`_entry_already_covering()` in `config_flow.py` blocks this at setup.

**A registered client is not a connected one.** The coordinator inserts the
client before dialling out, and `publish_action` drops commands silently while
disconnected. Check `is_connected`, not presence — that distinction is why
`_require_mqtt()` and `_connected_mqtt()` are separate.

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
