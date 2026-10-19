"""How long allow_for lasts and how many rings allow_times lets in. Read when
that mode is picked; changing them does not touch a mode already running."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntercomConfigEntry
from .coordinator import IntercomEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntercomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    c = entry.runtime_data
    async_add_entities(
        [
            IntercomNumber(c, "allow_minutes", 1, 24 * 60, UnitOfTime.MINUTES),
            IntercomNumber(c, "allow_rings", 1, 100, None),
        ]
    )


class IntercomNumber(IntercomEntity, RestoreNumber):
    """Stored on the coordinator under the entity's key."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    def __init__(self, coordinator, key, lo, hi, unit) -> None:
        super().__init__(coordinator, key)
        self._key = key
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_unit_of_measurement = unit

    @property
    def available(self) -> bool:
        return True  # a setting, not a reading: editable while the module is away

    @property
    def native_value(self) -> float:
        return getattr(self.coordinator, self._key)

    async def async_set_native_value(self, value: float) -> None:
        setattr(self.coordinator, self._key, value)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) is not None and (
            last.native_value is not None
        ):
            setattr(self.coordinator, self._key, last.native_value)
