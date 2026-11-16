"""Schakelaar: verschuifbaar verbruik echt schakelen, of alleen advies geven."""
from .const import DOMAIN
from .controls import SettingSwitch


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        SettingSwitch(c, "stage2", "Verbruik verschuiven", "mdi:sun-thermometer"),
    ])
