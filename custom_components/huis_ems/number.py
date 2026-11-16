"""Getallen om mee te spelen."""
from .const import DOMAIN
from .controls import SettingNumber


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        SettingNumber(c, "pv_factor", "Zonvoorspelling factor", 0.3, 2, 0.05, None, "mdi:weather-sunny"),
    ])
