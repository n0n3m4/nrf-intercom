"""Doorbell events, one per counter increment in the adverts."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntercomConfigEntry
from .coordinator import COUNTERS, IntercomEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntercomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([IntercomDoorbell(entry.runtime_data, "doorbell")])


class IntercomDoorbell(IntercomEntity, EventEntity):
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = list(COUNTERS.values())

    @callback
    def _handle_coordinator_update(self) -> None:
        events = self.coordinator.events
        for event_type in events:
            self._trigger_event(event_type)
            self.async_write_ha_state()
        if not events:  # availability changes come through here too
            self.async_write_ha_state()
