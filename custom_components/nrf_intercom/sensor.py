"""Battery, from the module's VDDH reading."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfElectricPotential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntercomConfigEntry
from .coordinator import IntercomEntity
from .protocol import battery_percent


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntercomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    c = entry.runtime_data
    async_add_entities([IntercomBattery(c, "battery"), IntercomVoltage(c, "voltage")])


class IntercomBattery(IntercomEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def native_value(self) -> int | None:
        if (state := self.coordinator.state) is None:
            return None
        return battery_percent(state["battery_mv"])


class IntercomVoltage(IntercomEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricPotential.MILLIVOLT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.state
        return None if state is None else state["battery_mv"]
