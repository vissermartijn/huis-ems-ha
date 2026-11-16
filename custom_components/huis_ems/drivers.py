"""Rechtstreekse koppelingen met accu's, binnen het eigen netwerk.

Het EMS leest een accu alleen uit (lading en vermogen); de accu regelt zichzelf
op zelfverbruik. Accu's zonder eigen koppeling hier worden uitgelezen via de
entiteiten van hun Home Assistant-integratie.

Zendure: de lokale API van de zenSDK (https://github.com/Zendure/zenSDK).
  GET  /properties/report   toestand
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 3.0
TIMEOUT = aiohttp.ClientTimeout(total=4)


async def zendure_report(hass: HomeAssistant, host: str) -> dict:
    session = async_get_clientsession(hass)
    async with session.get(f"http://{host}/properties/report", timeout=TIMEOUT) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


class ZendureDriver:
    """Eén Zendure-accu via zijn lokale HTTP-API."""

    def __init__(self, hass: HomeAssistant, host: str, name: str) -> None:
        self.hass = hass
        self.host = host
        self.name = name
        self.sn: str | None = None
        self.soc_pct: float | None = None
        self.power_w: float | None = None      # + = laden
        self.connected = False
        self.seen: float | None = None         # tijdstip (loop.time) van de laatste geslaagde uitlezing
        self._failures = 0
        self._task: asyncio.Task | None = None

    async def async_start(self) -> None:
        self._task = self.hass.async_create_background_task(self._poll(), f"huis_ems_{self.host}")

    async def async_stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _poll(self) -> None:
        while True:
            try:
                data = await zendure_report(self.hass, self.host)
                props = data.get("properties", {})
                self.sn = data.get("sn") or self.sn
                self.soc_pct = props.get("electricLevel")
                # outputPackPower = naar de accu (laden), packInputPower = uit de accu (ontladen)
                self.power_w = float(props.get("outputPackPower") or 0) - float(props.get("packInputPower") or 0)
                if not self.connected:
                    _LOGGER.info("Zendure-accu %s bereikbaar op %s", self.name, self.host)
                self.connected, self._failures = True, 0
                self.seen = self.hass.loop.time()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._failures += 1
                if self._failures == 3:
                    _LOGGER.warning("Zendure-accu %s niet bereikbaar op %s: %s", self.name, self.host, err)
                    self.connected = False
                    self.soc_pct = None   # zonder actuele lading telt de accu niet mee
            await asyncio.sleep(POLL_SECONDS)
