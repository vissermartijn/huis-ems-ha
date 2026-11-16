"""Bedieningselementen die hun waarde in de planner-instellingen bewaren."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.switch import SwitchEntity

from .entity import EmsEntity


class SettingSwitch(EmsEntity, SwitchEntity):

    def __init__(self, coordinator, key, name, icon):
        super().__init__(coordinator, key, name)
        self._key = key
        self._attr_icon = icon

    @property
    def is_on(self):
        return bool(self.coordinator.settings[self._key])

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set(self._key, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set(self._key, False)


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


class StatusBinary(EmsEntity, BinarySensorEntity):

    def __init__(self, coordinator, key, name, value, device_class, icon, note=None):
        super().__init__(coordinator, key, name)
        self._value = value
        self._note = note
        self._attr_device_class = device_class
        self._attr_icon = icon

    @property
    def is_on(self):
        return bool(self._value(self.coordinator))

    @property
    def extra_state_attributes(self):
        return {"toelichting": self._note(self.coordinator) if self._note
                else self.coordinator.status_note()}
