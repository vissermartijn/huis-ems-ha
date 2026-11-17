"""De P1-meter als bron voor het actuele netvermogen.

Twee manieren om aan metingen te komen:

- rechtstreeks: de lokale API van de HomeWizard P1-meter elke seconde
  uitlezen, binnen het eigen netwerk.
- via Home Assistant: de bestaande sensor volgen. Werkt met elke meter, maar
  is zo snel als die integratie ververst (HomeWizard: elke 5 seconden). Voor
  het voorspellen is dat ruim voldoende.

Valt de rechtstreekse verbinding weg, dan schakelt de bron terug naar de
sensor van Home Assistant.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import CONF_P1_HOST, CONF_P1_POWER

_LOGGER = logging.getLogger(__name__)
DIRECT_INTERVAL = 1.0
DIRECT, VIA_HA = "rechtstreeks (HomeWizard lokaal)", "via Home Assistant"


@dataclass
class Sample:
    power_w: float
    at: datetime


def _num(state):
    """Getalswaarde van een toestand; None als hij ontbreekt of ongeldig is."""
    try:
        value = float(state.state)
    except (AttributeError, TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


class Meter:

    def __init__(self, hass: HomeAssistant, options: dict, on_sample) -> None:
        self.hass = hass
        self.opt = options
        self._on_sample = on_sample
        self.latest: Sample | None = None
        self.interval_s: float | None = None     # gemeten tijd tussen metingen
        self.source = VIA_HA
        self._task: asyncio.Task | None = None
        self._unsub = None
        self._failures = 0

    async def async_start(self) -> None:
        entity = self.opt.get(CONF_P1_POWER)
        if entity:
            self._unsub = async_track_state_change_event(self.hass, [entity], self._on_state)
        if self.opt.get(CONF_P1_HOST):
            self._task = self.hass.async_create_background_task(self._poll(), "huis_ems_p1")

    async def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
        if self._task:
            self._task.cancel()

    def age_s(self) -> float | None:
        if self.latest is None:
            return None
        return (dt_util.utcnow() - self.latest.at).total_seconds()

    @callback
    def _publish(self, sample: Sample) -> None:
        if self.latest is not None:
            gap = (sample.at - self.latest.at).total_seconds()
            if 0 < gap < 120:
                self.interval_s = gap if self.interval_s is None else 0.9 * self.interval_s + 0.1 * gap
        self.latest = sample
        self._on_sample(sample)

    def read_entities(self) -> Sample | None:
        power = _num(self.hass.states.get(self.opt.get(CONF_P1_POWER) or ""))
        if power is None:
            return None
        return Sample(power, dt_util.utcnow())

    @callback
    def _on_state(self, event) -> None:
        if self.source == DIRECT:
            return
        sample = self.read_entities()
        if sample:
            self._publish(sample)

    async def _poll(self) -> None:
        session = async_get_clientsession(self.hass)
        url = f"http://{self.opt[CONF_P1_HOST]}/api/v1/data"
        while True:
            started = self.hass.loop.time()
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                if self.source != DIRECT:
                    _LOGGER.info("P1-meter wordt rechtstreeks uitgelezen op %s", url)
                self.source, self._failures = DIRECT, 0
                self._publish(Sample(float(data["active_power_w"]), dt_util.utcnow()))
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._failures += 1
                if self._failures == 5:
                    _LOGGER.warning(
                        "P1-meter rechtstreeks niet bereikbaar (%s); verder via Home Assistant", err)
                    self.source = VIA_HA
            await asyncio.sleep(max(0.2, DIRECT_INTERVAL - (self.hass.loop.time() - started)))
