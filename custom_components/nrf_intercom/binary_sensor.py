"""Ringing: on from the first ring edge until the ring window closes."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntercomConfigEntry
from .coordinator import IntercomEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntercomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([IntercomRinging(entry.runtime_data, "ringing")])


class IntercomRinging(IntercomEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.SOUND

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.state
        return None if state is None else state["ringing"]
