"""Huis EMS: voorspelt verbruik, zon en accu, en verschuift verbruik naar de zon."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

import os

from .const import DOMAIN
from .coordinator import EmsCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.NUMBER]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await _register_card(hass)
    snapshot = dict(entry.options)
    coordinator = EmsCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _remove_stale_entities(hass, entry, coordinator.unique_ids)
    await coordinator.async_start()
    entry.async_on_unload(entry.add_update_listener(_reload))
    if dict(entry.options) != snapshot:
        # Instellingen gewijzigd terwijl de integratie nog aan het laden was.
        hass.config_entries.async_schedule_reload(entry.entry_id)
    return True


def _remove_stale_entities(hass: HomeAssistant, entry: ConfigEntry, current: set[str]) -> None:
    """Ruimt entiteiten op die deze versie niet meer maakt (bijvoorbeeld van de accusturing)."""
    registry = er.async_get(hass)
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if item.unique_id not in current:
            registry.async_remove(item.entity_id)


CARD_URL = "/huis_ems/huis-ems-card.js"


async def _register_card(hass: HomeAssistant) -> None:
    """Maakt de overzichtskaart beschikbaar voor dashboards (type: custom:huis-ems-card)."""
    if hass.data.setdefault(DOMAIN, {}).get("_card"):
        return
    hass.data[DOMAIN]["_card"] = True
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig
    path = os.path.join(os.path.dirname(__file__), "frontend", "huis-ems-card.js")
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, path, False)])
    add_extra_js_url(hass, f"{CARD_URL}?v={int(os.path.getmtime(path))}")


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await hass.data[DOMAIN].pop(entry.entry_id).async_stop()
    return unloaded
