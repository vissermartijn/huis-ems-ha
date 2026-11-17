"""Getallen die hun waarde in de instellingen van het EMS bewaren."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode

from .entity import EmsEntity


class SettingNumber(EmsEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, key, name, low, high, step, unit, icon):
        super().__init__(coordinator, key, name)
        self._key = key
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon

    @property
    def native_value(self):
        return self.coordinator.settings[self._key]

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set(self._key, value)
