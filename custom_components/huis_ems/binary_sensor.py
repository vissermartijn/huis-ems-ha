"""Signalen: meterstoring en per buffer of hij zonnestroom mag opnemen."""
from .const import CONF_STAGE2, DOMAIN, L_ID, L_NAME
from .core.stage2 import LoadState
from .controls import StatusBinary


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        StatusBinary(c, "meterstoring", "Meterstoring", lambda co: co.meter_stale,
                     "problem", "mdi:meter-electric-outline"),
        # Ook bruikbaar als signaal voor een eigen automatisering (bij alleen advies).
        *[StatusBinary(c, f"trap_{item[L_ID]}", f"{item[L_NAME]} vrijgegeven",
                       lambda co, i=item[L_ID]: co.flex.get(i, LoadState()).on, None,
                       "mdi:sun-thermometer",
                       lambda co, i=item[L_ID]: co.flex.get(i, LoadState()).note)
          for item in c.opt.get(CONF_STAGE2) or []],
    ])
