"""The mode. Shows what the module advertises, or a just-picked option until
the write lands or fails; choosing an option sets what HA wants, which is
written to the module and re-written whenever it disagrees."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import IntercomConfigEntry, protocol
from .coordinator import IntercomEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntercomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([IntercomMode(entry.runtime_data, "mode")])


class IntercomMode(IntercomEntity, RestoreEntity, SelectEntity):
    _attr_options = list(protocol.MODES)

    @property
    def current_option(self) -> str | None:
        if self.coordinator.pending:
            return protocol.MODES[self.coordinator.desired.mode]
        state = self.coordinator.state
        if state is None or state["mode"] >= len(protocol.MODES):
            return None
        return protocol.MODES[state["mode"]]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        want = self.coordinator.desired
        state = self.coordinator.state
        timed = state is not None and state["mode"] in (
            protocol.ALLOW_UNTIL,
            protocol.ALLOW_N,
        )
        return {
            # Seconds left for allow_for, rings left for allow_times, else None.
            "remaining": state["remaining"] if timed else None,
            "desired": protocol.MODES[want.mode],
            "deadline": want.deadline,
            "count": want.count,
        }

    async def async_select_option(self, option: str) -> None:
        self.coordinator.async_set_mode(protocol.MODES.index(option))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is None:
            return
        a = last.attributes
        if a.get("desired") in protocol.MODES:
            self.coordinator.async_restore(
                protocol.Desired(
                    protocol.MODES.index(a["desired"]),
                    float(a.get("deadline") or 0),
                    int(a.get("count") or 0),
                )
            )
