"""Battery, from the module's VDDH reading, and what is left of a timed mode."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntercomConfigEntry
from .coordinator import IntercomEntity
from .protocol import ALLOW_N, ALLOW_UNTIL, battery_percent


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntercomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    c = entry.runtime_data
    async_add_entities(
        [
            IntercomBattery(c, "battery"),
            IntercomVoltage(c, "voltage"),
            IntercomLeft(c, "time_left", ALLOW_UNTIL, UnitOfTime.SECONDS),
            IntercomLeft(c, "rings_left", ALLOW_N, None),
        ]
    )


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


class IntercomLeft(IntercomEntity, SensorEntity):
    """The advertised ``remaining``, only while its own timed mode runs."""

    def __init__(self, coordinator, key, mode, unit) -> None:
        super().__init__(coordinator, key)
        self._mode = mode
        self._attr_native_unit_of_measurement = unit
        if unit:
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_suggested_unit_of_measurement = UnitOfTime.MINUTES

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.state
        if state is None or state["mode"] != self._mode:
            return None
        return state["remaining"]
