"""The one place a zone is written.

A zone document is **replace-all, per device, and does not propagate**. Every
speaker holds its own copy of every zone; writing to one leaves the others
serving their previous copies, which then compete on merge and make an
accepted edit appear to revert. That single fact is why every mutation goes
through here rather than through whichever caller happened to need one.

The shape of a write is fixed:

1. normalise and de-duplicate the members;
2. resolve every one of them to a speaker this coordinator knows;
3. check the zone rules (two members minimum, nobody in two zones);
4. **preflight** — every speaker that must receive the write is connected;
5. publish the complete intended document to each of them;
6. confirm every publish was submitted;
7. only then adopt the submitted document as the pending write snapshot.

Steps 4 to 6 are the point. Publishing to whoever happens to be online and
reporting success is a partial write reported as a whole one: the zone forms
on some speakers, competes on merge, and reverts minutes later with nothing
in the log. So a write either reaches every required speaker or it makes no
change at all and says why.

**Submission is not acknowledgement.** The protocol has no acknowledgement
for ``set_groups`` — no response event, no status echo, nothing to correlate
a write with. "Written" here means the command was handed to a connected MQTT
client for that speaker. A speaker can still fail to apply it, and the only
way to find out is the ``groups`` event it sends afterwards, which the
coordinator re-reads on a short schedule after every write for exactly this
reason. Anything stronger would be a claim the protocol cannot support.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import (
        UnifiPlayCoordinator,
        UnifiPlayDeviceState,
        UnifiPlayGroupState,
    )

_LOGGER = logging.getLogger(__name__)

#: A zone is two speakers playing in sync. One speaker is not a zone, it is a
#: speaker, and the firmware treats a one-member zone as a malformed document.
MIN_ZONE_MEMBERS = 2


def normalise_mac(mac: str) -> str:
    """Uppercase hex, no delimiters.

    MACs arrive from three places that disagree: the device registry stores
    them as the device reported them, the wire carries them raw, and users
    type them with colons. Everything compared here is normalised; everything
    *written* keeps the device's own spelling, because that is what the rest
    of the payload uses.
    """
    return mac.upper().replace(":", "").replace("-", "")


@dataclass(frozen=True)
class ZoneWriteResult:
    """What a completed zone write actually did.

    Returned rather than a bare success flag so callers can say which
    speakers were reached — and so a caller that ignores the result is
    visible in review as a caller that ignores the result.
    """

    group_id: str
    #: Speakers the document was submitted to, in the order written.
    written_macs: tuple[str, ...]
    #: True when the write removed the zone rather than defining it.
    deleted: bool = False

    def __bool__(self) -> bool:
        return bool(self.written_macs)


class ZoneWriteError(HomeAssistantError):
    """A zone write reached the wire and could not be completed.

    Distinct from the validation errors below, which are raised before
    anything is published. This one means some speakers may have been
    written to and others not, which is the state the preflight exists to
    prevent and which is therefore worth its own type.
    """


def _translated(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


class ZoneWriter:
    """Owns every mutation of zone topology for one coordinator."""

    def __init__(self, coordinator: UnifiPlayCoordinator) -> None:
        self._coordinator = coordinator

    # ── Resolution ────────────────────────────────────────────────────────

    def device_for_mac(self, mac: str) -> UnifiPlayDeviceState | None:
        """The speaker with this MAC, or None if this coordinator has none."""
        target = normalise_mac(mac)
        for state in self._coordinator.data.values():
            if normalise_mac(state.mac) == target:
                return state
        return None

    def _resolve_members(self, macs: Sequence[str]) -> list[UnifiPlayDeviceState]:
        """Normalise, de-duplicate and resolve a member list.

        Order is preserved: the first speaker listed is the one the firmware
        is most likely to elect as host, and a caller that put a particular
        speaker first meant it.
        """
        seen: set[str] = set()
        resolved: list[UnifiPlayDeviceState] = []
        for raw in macs:
            mac = normalise_mac(raw)
            if not mac or mac in seen:
                # A duplicate is a caller listing the same speaker twice,
                # which the firmware would take literally: dev_count would
                # disagree with dev_info and the zone would form wrong.
                continue
            seen.add(mac)
            state = self.device_for_mac(mac)
            if state is None:
                raise _translated("zone_unknown_device", mac=mac)
            resolved.append(state)
        return resolved

    # ── Rules ─────────────────────────────────────────────────────────────

    def _check_membership_rules(
        self, members: Iterable[UnifiPlayDeviceState], group_id: str
    ) -> None:
        """A speaker belongs to at most one zone, and a zone needs two.

        The one-zone rule is the firmware's, not this integration's: a
        speaker listed in two zones registers membership in both and plays
        in neither reliably.
        """
        members = list(members)
        if len(members) < MIN_ZONE_MEMBERS:
            raise _translated("zone_needs_two_devices")

        member_macs = {normalise_mac(state.mac) for state in members}
        for other_gid, other in self._coordinator.groups_for_write().items():
            if other_gid == group_id:
                continue
            for entry in other.dev_info:
                mac = normalise_mac(entry.get("mac", ""))
                if mac not in member_macs:
                    continue
                state = self.device_for_mac(mac)
                raise _translated(
                    "zone_device_in_other_zone",
                    device=state.device_name if state else mac,
                    zone=other.name or other_gid,
                )

    # ── Preflight ─────────────────────────────────────────────────────────

    def required_macs(self, group_id: str, member_macs: Iterable[str]) -> list[str]:
        """Speakers that must receive this write, normalised and sorted.

        Three groups, and each is here for a reason:

        - the zone's members *before* the write — one of them is the host,
          whose copy wins the merge, so leaving it out means the edit loses
          to the state it replaced;
        - the zone's members *after* the write — a speaker joining has to be
          told, and a speaker leaving has to stop listing the zone;
        - any other speaker whose cached copy of this zone would be left
          stale. Those copies compete on merge whenever no member claims
          host, which is exactly the window after a fresh write.

        A speaker that has never reported a zone list is not required: it
        holds nothing that can go stale, and blocking every zone edit on a
        speaker that has never been reachable would be a worse failure than
        the one this prevents.
        """
        required = {normalise_mac(mac) for mac in member_macs}

        existing = self._coordinator.groups_for_write().get(group_id)
        if existing is not None:
            required.update(
                normalise_mac(entry.get("mac", ""))
                for entry in existing.dev_info
                if entry.get("mac")
            )

        for device_id, cached in self._coordinator.device_zone_cache().items():
            if group_id not in cached:
                continue
            state = self._coordinator.data.get(device_id)
            if state is not None and state.mac:
                required.add(normalise_mac(state.mac))

        required.discard("")
        return sorted(required)

    def _preflight(self, macs: Sequence[str]) -> list[tuple[str, Any]]:
        """Resolve every required speaker to a live client, or refuse.

        Nothing is published before this returns. A zone written to half the
        speakers is worse than a zone not written at all: it forms, competes
        on merge, and reverts minutes later with nothing in the log to say
        why.
        """
        clients: list[tuple[str, Any]] = []
        offline: list[str] = []
        for mac in macs:
            client = self._coordinator.get_mqtt_client_for_mac(mac)
            if client is None:
                state = self.device_for_mac(mac)
                offline.append(state.device_name if state else mac)
                continue
            clients.append((mac, client))
        if offline:
            raise _translated("zone_members_offline", devices=", ".join(offline))
        return clients

    # ── Writes ────────────────────────────────────────────────────────────

    def apply(
        self,
        *,
        group_id: str,
        name: str,
        member_macs: Sequence[str],
        group_index: int = 0,
        broadcasting_mode: str = "zone_only",
        wb_enable: bool = False,
        wb_device: str = "",
        wb_input: str = "",
    ) -> ZoneWriteResult:
        """Define a zone. Creates it when the id is new, replaces it otherwise.

        Every mutation the integration offers is this call with different
        arguments — create, rename, reorder, add a member, remove one, change
        the broadcast source. Keeping them one code path is what stops six
        callers each getting the preflight slightly wrong.
        """
        from .helpers import dev_info_entry, group_payload

        members = self._resolve_members(member_macs)
        self._check_membership_rules(members, group_id)

        macs = [normalise_mac(state.mac) for state in members]
        required = self.required_macs(group_id, macs)
        clients = self._preflight(required)

        # The host flag is deliberately absent from dev_info_entry: the
        # firmware elects a host and echoes the flag back, and asserting it
        # produces a zone that registers on every speaker and only ever
        # sounds on one.
        document = group_payload(
            group_id=group_id,
            name=name,
            dev_info=[dev_info_entry(state) for state in members],
            group_index=group_index,
            broadcasting_mode=broadcasting_mode,
            wb_enable=wb_enable,
            wb_device=wb_device,
            wb_input=wb_input,
        )
        return self._publish(group_id, document, clients, deleted=False)

    def delete(self, group_id: str) -> ZoneWriteResult:
        """Remove a zone from every speaker that holds a copy."""
        if group_id not in self._coordinator.groups_for_write():
            raise _translated("zone_not_found")
        required = self.required_macs(group_id, ())
        clients = self._preflight(required)
        return self._publish(group_id, None, clients, deleted=True)

    def _publish(
        self,
        group_id: str,
        document: dict[str, Any] | None,
        clients: Sequence[tuple[str, Any]],
        *,
        deleted: bool,
    ) -> ZoneWriteResult:
        """Send the complete zone list to each preflighted speaker.

        The list is rebuilt from the write snapshot for every write, so
        zones this call is not touching survive — including a change that
        has been submitted but not yet reported back — and a zone changing
        hands needs no separate "strip it from the old host" write, because
        the old host is given the same list as everyone else.
        """
        groups = self._coordinator.zone_documents(group_id, document)

        written: list[str] = []
        failed: list[str] = []
        for mac, client in clients:
            if client.publish_action("set_groups", {"groups": groups}):
                written.append(mac)
            else:
                # The socket went between preflight and here. Rare, and the
                # one case where a partial write is possible; saying so is
                # the only honest option, because there is no way to unsend
                # what already left.
                failed.append(mac)

        if failed:
            raise ZoneWriteError(
                translation_domain=DOMAIN,
                translation_key="zone_publish_failed",
                translation_placeholders={
                    "written": ", ".join(written) or "none",
                    "failed": ", ".join(failed),
                },
            )

        _LOGGER.debug(
            "zone %s: %s %d zone(s) to %d speaker(s) %s",
            group_id,
            "deleted from" if deleted else "wrote",
            len(groups),
            len(written),
            written,
        )
        # Submitted, not acknowledged. Hold this document as the source of
        # the next write until a groups event confirms it; otherwise a
        # rename immediately followed by an index change rebuilds the
        # second list from the pre-rename report and undoes the first.
        self._coordinator.adopt_written_groups(groups)
        self._coordinator.schedule_host_election_reread()
        return ZoneWriteResult(
            group_id=group_id, written_macs=tuple(written), deleted=deleted
        )

    # ── Convenience wrappers, all of them one call to apply() ─────────────

    def _existing(self, group_id: str) -> UnifiPlayGroupState:
        gs = self._coordinator.groups_for_write().get(group_id)
        if gs is None:
            raise _translated("zone_not_found")
        return gs

    def _member_macs(self, gs: UnifiPlayGroupState) -> list[str]:
        return [
            normalise_mac(entry["mac"]) for entry in gs.dev_info if entry.get("mac")
        ]

    def _apply_to(self, gs: UnifiPlayGroupState, **overrides: Any) -> ZoneWriteResult:
        """Rewrite an existing zone, changing only what is named."""
        fields: dict[str, Any] = {
            "group_id": gs.group_id,
            "name": gs.name,
            "member_macs": self._member_macs(gs),
            "group_index": gs.group_index,
            "broadcasting_mode": gs.broadcasting_mode,
            "wb_enable": gs.wb_enable,
            "wb_device": gs.wb_device,
            "wb_input": gs.wb_input,
        }
        fields.update(overrides)
        return self.apply(**fields)

    def create(self, *, name: str, member_macs: Sequence[str]) -> ZoneWriteResult:
        """Create a zone with a fresh id."""
        import uuid

        return self.apply(
            group_id=str(uuid.uuid4()), name=name, member_macs=member_macs
        )

    def rename(self, group_id: str, name: str) -> ZoneWriteResult:
        return self._apply_to(self._existing(group_id), name=name)

    def set_index(self, group_id: str, group_index: int) -> ZoneWriteResult:
        return self._apply_to(self._existing(group_id), group_index=group_index)

    def set_broadcasting_mode(self, group_id: str, mode: str) -> ZoneWriteResult:
        return self._apply_to(self._existing(group_id), broadcasting_mode=mode)

    def set_members(self, group_id: str, member_macs: Sequence[str]) -> ZoneWriteResult:
        return self._apply_to(self._existing(group_id), member_macs=list(member_macs))

    def add_member(self, group_id: str, mac: str) -> ZoneWriteResult:
        gs = self._existing(group_id)
        current = self._member_macs(gs)
        target = normalise_mac(mac)
        if target in current:
            state = self.device_for_mac(target)
            raise _translated(
                "zone_already_member",
                device=state.device_name if state else target,
                zone=gs.name or group_id,
            )
        return self._apply_to(gs, member_macs=[*current, target])

    def remove_member(self, group_id: str, mac: str) -> ZoneWriteResult:
        """Remove a speaker, including the one currently hosting.

        The host is an internal protocol role rather than something the user
        chose, so removing it hands the role over instead of refusing. The
        successor is not named: the zone is rewritten without a host and the
        survivors elect one, exactly as they do for a new zone. Writing the
        flag ourselves is what breaks audio sync to the members.

        UNVERIFIED: re-election after the host is removed from a *live* zone
        has not been confirmed on hardware — only re-election on creation
        has. See docs/api.md.
        """
        gs = self._existing(group_id)
        current = self._member_macs(gs)
        target = normalise_mac(mac)
        if target not in current:
            state = self.device_for_mac(target)
            raise _translated(
                "zone_member_not_in_zone",
                device=state.device_name if state else target,
                zone=gs.name or group_id,
            )
        remaining = [candidate for candidate in current if candidate != target]
        if len(remaining) < MIN_ZONE_MEMBERS:
            raise _translated("zone_would_be_too_small")

        # If the departing speaker was the one broadcasting a wired source,
        # that source leaves with it and the zone falls back to streaming.
        keeps_broadcast = (
            gs.wb_enable and normalise_mac(gs.wb_device or gs.host_mac) != target
        )
        return self._apply_to(
            gs,
            member_macs=remaining,
            wb_enable=keeps_broadcast,
            wb_device=gs.wb_device if keeps_broadcast else "",
            wb_input=gs.wb_input if keeps_broadcast else "",
        )

    def set_broadcast_source(
        self, group_id: str, *, source_mac: str, wb_input: str
    ) -> ZoneWriteResult:
        """Broadcast one member's wired input across the zone.

        Two writes to two different places: the zone document goes to every
        required speaker, and the input switch goes to the speaker that will
        actually broadcast — frequently not the host. Sending both to the
        host switches the wrong speaker's input and leaves the chosen one
        untouched.

        The input switch happens only after the zone write has succeeded, so
        a refused zone write cannot leave a speaker on an input nothing is
        listening to.
        """
        gs = self._existing(group_id)
        target = normalise_mac(source_mac)
        if target not in self._member_macs(gs):
            state = self.device_for_mac(target)
            raise _translated(
                "zone_member_not_in_zone",
                device=state.device_name if state else target,
                zone=gs.name or group_id,
            )
        result = self._apply_to(
            gs, wb_enable=True, wb_device=source_mac, wb_input=wb_input
        )
        client = self._coordinator.get_mqtt_client_for_mac(source_mac)
        if client is None:  # pragma: no cover - preflight covered this speaker
            raise ZoneWriteError(
                translation_domain=DOMAIN,
                translation_key="zone_publish_failed",
                translation_placeholders={"written": "the zone", "failed": source_mac},
            )
        client.set_source(wb_input)
        return result

    def clear_broadcast_source(self, group_id: str) -> ZoneWriteResult:
        """Return the zone to streaming.

        The previously broadcasting speaker is handed back to streaming where
        it can be reached. Its being offline is not an error here: the zone
        is already off the wired source, and the speaker will be on streaming
        anyway once it rejoins a zone that has none.
        """
        gs = self._existing(group_id)
        previous = gs.wb_device or gs.host_mac
        result = self._apply_to(gs, wb_enable=False, wb_device="", wb_input="")
        if gs.wb_enable and previous:
            client = self._coordinator.get_mqtt_client_for_mac(previous)
            if client is not None:
                client.set_source("streaming")
            else:
                _LOGGER.debug(
                    "Zone %s: previous broadcast speaker %s is offline; leaving "
                    "its input alone",
                    gs.name,
                    previous,
                )
        return result
