"""Het EMS: voorspelt verbruik, zon en wat de accu doet, en verschuift verbruik naar de zon.

De accu regelt zichzelf op zelfverbruik; het EMS leest hem alleen uit. Elk
kwartier rekent het vooruit wat de accu gaat doen (wanneer vol, wanneer leeg,
hoeveel van het net). Bij elke meting kijkt het of er na de accu nog zon over
is voor de warmtebuffers: de vloer via een hogere thermostaat en de boiler via
zijn element.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import *  # noqa: F401,F403
from .core import forecast as fc
from .core.balance import describe, export_ahead_w, outlook
from .core.buffers import (
    SWITCH, THERMOSTAT, Buffer, Context, IdleWatch, boost_target, spare_w, start_block,
    stop_reason)
from .core.fleet import Unit, aggregate, respond
from .core.loadmodel import (
    fit, level_correction, rolling_mean_24, thermostat_factors, thermostat_weight)
from .core.optimizer import ZELFVERBRUIK, Battery, Slot
from .core.series import delta as pv_delta, duplicate_groups, merge
from .core.stage2 import LoadState, decide
from .core.tariff import DynamicTariff
from .core.timeline import slot_starts
from .drivers import ZendureDriver
from .meter import DIRECT, Meter

_LOGGER = logging.getLogger(__name__)
DEFAULT_LOAD_W = 400.0
RELEASE_WAIT_SECONDS = 8


def _float(state):
    try:
        value = float(state.state)
        return value if math.isfinite(value) else None
    except (AttributeError, TypeError, ValueError):
        return None


def _attr_float(state, key):
    try:
        value = float(state.attributes.get(key))
        return value if math.isfinite(value) else None
    except (AttributeError, TypeError, ValueError):
        return None


class EmsCoordinator:

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.opt = {**entry.data, **entry.options}
        self.store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.settings = dict(DEFAULT_SETTINGS)
        self.unique_ids: set[str] = set()        # entiteiten van deze versie, voor het opruimen
        self.virtual_soc: dict[str, float] = {}
        self.actual_power: dict[str, float] = {}  # vermogen van virtuele accu's
        self.history: list[dict] = []
        self.costs = {"zonder": 0.0, "met": 0.0, "dag": "", "dag_zonder": 0.0, "dag_met": 0.0}
        self.grid_without: float | None = None
        self.grid_with: float | None = None
        self.battery_w: float = 0.0
        self.plan: dict = {}
        self.look: dict = {"tekst": "Nog geen vooruitblik berekend."}
        self.prices_now: tuple[float | None, float | None] = (None, None)
        self.models = {"rest": None, "hp": None}
        self.corrections = {"rest": 1.0, "hp": 1.0}
        self.model_note = "Nog niet geleerd; er wordt met een vast verbruik gerekend."
        self.model_names: dict[str, str] = {}
        self.model_info: dict[str, dict] = {}
        self.thermostat: dict[str, tuple[float, float]] = {}
        self.load_parts: dict[str, list] = {}
        self.timing = {"plan_ms": None, "leren_ms": None, "meting_ms": None}
        self.soc_issue: dict[str, str] = {}
        # Verschuifbaar verbruik
        self.flex: dict[str, LoadState] = {}      # stand per buffer
        self.flex_power = 0.0                     # wat de buffers nu zelf gebruiken
        self.spare_w: float | None = None         # overschot na de accu, zonder de buffers
        self.owned: set[str] = set()              # buffers die het EMS zelf heeft aangezet
        self.boost: dict[str, dict] = {}          # thermostaat: normale en verhoogde temperatuur
        self.boost_hours: set[int] = set()        # uren (epoch) waarin de thermostaat hoger stond
        self.flex_issue: dict[str, str] = {}
        self.resting: dict[str, float] = {}       # buffer neemt niets af: rust tot
        self.hands_off: dict[str, float] = {}     # met de hand gewijzigd: het EMS wacht tot
        self._pending: dict[str, tuple[bool, float, int]] = {}
        self._idle = IdleWatch()
        self._forecast_w: dict[str, float] = {}
        self._recent_temps: dict[datetime, float] = {}
        self._model_at: datetime | None = None
        self._record: dict | None = None
        self._marks: dict[str, float] = {}        # zonnetellers bij het begin van het kwartier
        self.pv_groups: list[list[str]] = []      # zonnetellers die dezelfde meter zijn
        self._acc = self._empty_acc()
        self._last_rt: datetime | None = None
        self._last_save: datetime | None = None
        self._last_notify: datetime | None = None
        self._planning = False
        self.meter = Meter(hass, self.opt, self._on_sample)
        self.drivers: dict[str, ZendureDriver] = {}
        self.meter_stale = False
        self._listeners: list = []
        self._unsubs: list = []

    @staticmethod
    def _empty_acc():
        return {"zonder": 0.0, "met": 0.0, "sec": 0.0, "flex": {}}

    # --- levensloop -------------------------------------------------------

    async def async_start(self) -> None:
        data = await self.store.async_load() or {}
        self.settings.update({k: v for k, v in data.get("settings", {}).items() if k in DEFAULT_SETTINGS})
        self.virtual_soc = data.get("virtual_soc", {})
        # Kwartieren uit de versie die de accu stuurde hebben andere velden.
        self.history = [r for r in data.get("history", []) if "verwacht_verbruik_w" in r]
        self.costs.update(data.get("costs", {}))
        self.owned = set(data.get("owned", []))
        self.boost = data.get("boost", {})
        self.boost_hours = set(data.get("boost_hours", []))
        self._unsubs.append(async_track_time_change(
            self.hass, self._on_quarter, minute=range(0, 60, SLOT_MIN), second=5))
        self._unsubs.append(async_track_time_interval(
            self.hass, self._on_watchdog, timedelta(seconds=WATCHDOG_SECONDS)))
        for b in self.batteries:   # eerst de accu's, zodat de eerste meting ze al kent
            if b.get(B_DRIVER) == DRIVER_ZENDURE and b.get(B_HOST):
                self.drivers[b[B_ID]] = ZendureDriver(self.hass, b[B_HOST], b[B_NAME])
                await self.drivers[b[B_ID]].async_start()
        await self.meter.async_start()
        self.hass.async_create_task(self.async_plan())

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        await self.meter.async_stop()
        # Wat het EMS had aangezet gaat terug naar normaal: zonder EMS werkt alles
        # zoals het zonder EMS zou werken.
        try:
            await asyncio.wait_for(self._release_all(), RELEASE_WAIT_SECONDS)
        except Exception as err:
            _LOGGER.warning("Terugzetten van verschoven verbruik niet afgerond: %s", err)
        for driver in self.drivers.values():
            await driver.async_stop()
        await self._save()

    async def _save(self) -> None:
        oldest = time.time() - BOOST_DAYS_KEPT * 86400
        self.boost_hours = {h for h in self.boost_hours if h >= oldest}
        await self.store.async_save({
            "settings": self.settings, "virtual_soc": self.virtual_soc,
            "history": self.history[-7 * 96:], "costs": self.costs,
            "owned": sorted(self.owned), "boost": self.boost,
            "boost_hours": sorted(self.boost_hours)})

    @callback
    def async_add_listener(self, update):
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    async def async_set(self, key: str, value) -> None:
        if key == "stage2" and not value:
            await self._release_all()   # schakelen gaat uit: eerst alles terug naar normaal
        if key == "stage2":
            self.flex.clear()           # opnieuw beginnen, met de wachttijden
            self._pending.clear()
            self.flex_issue.clear()
        self.settings[key] = value
        await self._save()
        await self.async_plan()

    # --- accu's: alleen uitlezen ------------------------------------------

    @property
    def batteries(self) -> list[dict]:
        return self.opt.get(CONF_BATTERIES, [])

    def units(self) -> list[Unit]:
        out = []
        for b in self.batteries:
            cap = float(b[B_CAPACITY])
            lo = cap * b.get(B_MIN_SOC, 10) / 100
            hi = cap * b.get(B_MAX_SOC, 100) / 100
            if b.get(B_VIRTUAL, True):
                soc = self.virtual_soc.setdefault(b[B_ID], lo)
            else:
                pct, problem = self._soc(b)
                if problem:
                    driver = self.drivers.get(b[B_ID])
                    if b.get(B_DRIVER) == DRIVER_ZENDURE and (driver is None or driver.seen is None):
                        # Net gestart: nog niet uitgelezen is geen storing.
                        self.soc_issue[b[B_ID]] = "is nog niet uitgelezen"
                        continue
                    if b[B_ID] not in self.soc_issue:
                        _LOGGER.warning("Laadtoestand van %s %s", b[B_NAME], problem)
                    self.soc_issue[b[B_ID]] = problem
                    continue
                if self.soc_issue.pop(b[B_ID], None):
                    _LOGGER.info("Laadtoestand van %s is weer bruikbaar", b[B_NAME])
                soc = cap * pct / 100
            out.append(Unit(
                b[B_ID], b[B_NAME], cap, lo, hi,
                b.get(B_MAX_CHARGE, 3000) / 1000, b.get(B_MAX_DISCHARGE, 3000) / 1000,
                math.sqrt(b.get(B_EFFICIENCY, 86) / 100), soc, b.get(B_VIRTUAL, True)))
        return out

    def _soc(self, battery: dict):
        """Laadtoestand van een echte accu in procenten, of de reden waarom die niet bruikbaar is."""
        driver = self.drivers.get(battery[B_ID])
        if driver:
            pct = driver.soc_pct
            if pct is None:
                return None, "is onbekend (accu niet bereikbaar)"
        else:
            state = self.hass.states.get(battery.get(B_SOC_SENSOR) or "")
            pct = _float(state)
            if pct is None:
                return None, "is onbekend"
            seen = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
            if seen is not None and (dt_util.utcnow() - seen).total_seconds() > SOC_STALE_SECONDS:
                return None, f"is verouderd (laatst gemeld {dt_util.as_local(seen):%H:%M})"
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            return None, "is ongeldig"
        if not 0 <= pct <= 100:
            return None, f"is ongeldig ({pct:.0f}%)"
        return pct, None

    def _soc_age(self, battery: dict):
        if battery.get(B_VIRTUAL, True):
            return None
        driver = self.drivers.get(battery[B_ID])
        if driver:
            return None if driver.seen is None else round(self.hass.loop.time() - driver.seen)
        state = self.hass.states.get(battery.get(B_SOC_SENSOR) or "")
        seen = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
        return None if seen is None else round((dt_util.utcnow() - seen).total_seconds())

    def _measured_power(self, battery: dict) -> float:
        """Vermogen van een echte accu (+ = laden), 0 als er geen meting is."""
        driver = self.drivers.get(battery.get(B_ID))
        if driver:
            return driver.power_w or 0.0
        value = _float(self.hass.states.get(battery.get(B_POWER_SENSOR) or ""))
        if value is None:
            return 0.0
        return -value if battery.get(B_POWER_INVERT) else value

    def _real_battery_power(self) -> float:
        return sum(self._measured_power(b) for b in self.batteries if not b.get(B_VIRTUAL, True))

    def soc_summary(self):
        """(lading van alle accu's samen in %, probleem) — (None, None) zonder accu."""
        if not self.batteries:
            return None, None
        units = self.units()
        problems = [f"{b[B_NAME]} {self.soc_issue[b[B_ID]]}" for b in self.batteries if b[B_ID] in self.soc_issue]
        if problems:
            return None, "; ".join(problems)
        cap = sum(u.capacity_kwh for u in units)
        return (sum(u.soc_kwh for u in units) / cap * 100 if cap else None), None

    # --- invoer -----------------------------------------------------------

    def _prices(self, starts):
        """Geeft ([(koop, verkoop)], toelichting, [herkomst per slot])."""
        o = self.opt
        mode = o.get(CONF_TARIFF_MODE, TARIFF_FIXED)
        salderen = o.get(CONF_NET_METERING, False)
        fixed_buy = o.get(CONF_FIXED_BUY, 0.27)
        fixed = (fixed_buy, fixed_buy if salderen else o.get(CONF_FIXED_SELL, 0.05))
        if mode == TARIFF_FIXED:
            return [fixed] * len(starts), "vast tarief", ["vast"] * len(starts)
        state = self.hass.states.get(o.get(CONF_PRICE_SENSOR) or "")
        series = fc.parse_series(dict(state.attributes)) if state else []
        scale = o.get(CONF_PRICE_SCALE, 1.0)
        raw = fc.price_lookup(series, starts)
        if raw[0][1] != fc.KNOWN:
            why = "geen dynamische prijzen gevonden" if not series else (
                "de prijssensor heeft geen prijs voor dit kwartier (laatste prijs geldt tot "
                f"{dt_util.as_local(series[-1][0]):%d-%m %H:%M})")
            return [fixed] * len(starts), f"{why}; vast tarief gebruikt", ["terugval"] * len(starts)
        tariff = DynamicTariff(o.get(CONF_ENERGY_TAX, 0.0916), o.get(CONF_BUY_FEE, 0.02),
                               o.get(CONF_SELL_FEE, 0.0), o.get(CONF_VAT, 21) / 100, salderen)
        out, origins = [], []
        for value, origin in raw:
            if value is None:
                break
            origins.append(origin)
            value *= scale
            if mode == TARIFF_ALL_IN:
                spot = value / (1 + tariff.vat) - tariff.energy_tax - tariff.buy_fee
                out.append((value, value if salderen else spot - tariff.sell_fee))
            else:
                out.append((tariff.buy(value), tariff.sell(value)))
        if fc.ESTIMATED in origins:
            first = starts[origins.index(fc.ESTIMATED)]
            return out, (f"dynamisch; prijzen bekend tot {first:%d-%m %H:%M}, daarna geschat met "
                         "de prijs van een etmaal eerder"), origins
        return out, "dynamisch; alle prijzen in de periode zijn bekend", origins

    async def _pv(self, starts, hours):
        total = [0.0] * len(starts)
        o = self.opt
        if o.get(CONF_USE_ENERGY_FORECAST, True):
            try:
                from homeassistant.components.energy.data import async_get_manager
                from homeassistant.components.energy.websocket_api import (
                    async_get_energy_platforms)
                manager = await async_get_manager(self.hass)
                platforms = await async_get_energy_platforms(self.hass)
                entry_ids = set()
                for source in (manager.data or {}).get("energy_sources", []):
                    if source.get("type") == "solar":
                        entry_ids.update(source.get("config_entry_solar_forecast") or [])
                for entry_id in entry_ids:
                    entry = self.hass.config_entries.async_get_entry(entry_id)
                    if entry is None or entry.domain not in platforms:
                        continue
                    result = await platforms[entry.domain](self.hass, entry_id)
                    if result and result.get("wh_hours"):
                        series = fc.parse_series({"wh_period": result["wh_hours"]}, ["wh_period"])
                        part = fc.energy_per_slot(series, starts, hours, "wh_period")
                        total = [a + b for a, b in zip(total, part)]
            except Exception as err:
                _LOGGER.warning("Zonvoorspelling van het Energie-dashboard niet beschikbaar: %s", err)
        for entity_id in o.get(CONF_PV_FORECAST_SENSORS) or []:
            state = self.hass.states.get(entity_id)
            if state:
                series = fc.parse_series(dict(state.attributes))
                part = fc.energy_per_slot(series, starts, hours, o.get(CONF_PV_FORECAST_UNIT, "kw"))
                total = [a + b for a, b in zip(total, part)]
        factor = self.settings.get("pv_factor", 1.0)
        return [v * factor for v in total]

    async def _statistics(self, ids, start, types):
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period
        if not ids:
            return {}
        return await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, None, set(ids), "hour", None, types)

    @staticmethod
    def _hourly(rows, key):
        out = {}
        for row in rows or []:
            value = row.get(key)
            if value is None:
                continue
            start = row["start"]
            if not isinstance(start, datetime):
                start = dt_util.utc_from_timestamp(start)
            out[start] = float(value)
        return out

    async def _deficits(self, consumer, start):
        """Per uur: ingestelde min gemeten temperatuur in het uur ervoor."""
        current_id, target_id = consumer.get(C_CURRENT), consumer.get(C_SETPOINT)
        if not current_id or not target_id:
            return None
        stats = await self._statistics([current_id, target_id], start, {"mean"})
        current = self._hourly(stats.get(current_id), "mean")
        target = self._hourly(stats.get(target_id), "mean")
        live = _float(self.hass.states.get(target_id))  # een instelling heeft vaak geen historie
        out = {}
        for t, value in current.items():
            wanted = target.get(t, live)
            if wanted is not None:
                out[t + timedelta(hours=1)] = wanted - value
        return out or None

    async def _learn(self, now) -> None:
        """Leert het verbruik per uur uit de langetermijnstatistieken van HA."""
        o = self.opt
        imports = o.get(CONF_IMPORT_ENERGY) or []
        exports = o.get(CONF_EXPORT_ENERGY) or []
        pvs = o.get(CONF_PV_ENERGY) or []
        hps = o.get(CONF_HP_ENERGY) or []
        consumers = [c for c in o.get(CONF_CONSUMERS) or [] if c.get(C_ENERGY)]
        # Wat een boiler op zonoverschot gebruikt hoort niet in het verbruiksmodel.
        switched_ids = [ld[L_ENERGY] for ld in o.get(CONF_STAGE2) or []
                        if ld.get(L_ENERGY) and ld.get(L_KIND, SWITCH) == SWITCH]
        temp_id = o.get(CONF_OUTDOOR_TEMP)
        if not imports:
            self.model_note = "Geen afnamemeters ingesteld; er wordt met een vast verbruik gerekend."
            return
        start = now - timedelta(days=365)
        ids = imports + exports + pvs + hps + switched_ids + [i for c in consumers for i in c[C_ENERGY]]
        energy = await self._statistics(ids, start, {"change"})
        temps = self._hourly((await self._statistics([temp_id], start, {"mean"})).get(temp_id), "mean") \
            if temp_id else {}
        t24 = rolling_mean_24(temps) if temps else {}
        self._recent_temps = {t: v for t, v in temps.items() if t >= now - timedelta(hours=30)}
        batt = {}
        for b in self.batteries:  # echte accu's uit het huisverbruik halen
            if b.get(B_VIRTUAL, True):
                continue
            sensor_id, sign = b.get(B_POWER_SENSOR), (-1 if b.get(B_POWER_INVERT) else 1)
            if b[B_ID] in self.drivers:  # eigen vermogenssensor van deze integratie
                sensor_id, sign = er.async_get(self.hass).async_get_entity_id(
                    "sensor", DOMAIN, f"{self.entry.entry_id}_vermogen_{b[B_ID]}"), 1
            if sensor_id:
                rows = (await self._statistics([sensor_id], start, {"mean"})).get(sensor_id)
                for t, v in self._hourly(rows, "mean").items():
                    batt[t] = batt.get(t, 0.0) + sign * v / 1000

        def total(ids):
            return [self._hourly(energy.get(i), "change") for i in ids]

        imp, exp, pv, switched = total(imports), total(exports), total(pvs), total(switched_ids)
        # Oude en nieuwe teller van dezelfde omvormer tellen samen maar één keer.
        groups = duplicate_groups(pv)
        self.pv_groups = [[pvs[i] for i in g] for g in groups]
        pv_hourly = merge(pv, groups)
        parts, short = [], []
        if hps:
            parts.append(("hp", "warmtepomp", total(hps), None))
        for consumer in consumers:
            parts.append((consumer[C_ID], consumer[C_NAME], total(consumer[C_ENERGY]),
                          await self._deficits(consumer, start)))
        for part in list(parts):
            if min(len(s) for s in part[2]) < 48:
                parts.remove(part)
                short.append(part[1])
        # Uren waarin het EMS de thermostaat hoger had gezet zeggen niets over het
        # gewone stookgedrag; die tellen niet mee voor de warmtepomp.
        boosted_key = "hp" if any(p[0] == "hp" for p in parts) else "rest"
        samples = {part[0]: [] for part in parts}
        samples["rest"] = []
        skipped = 0
        for t in sorted(imp[0]):
            if any(t not in s for s in imp):
                continue
            house = (sum(s[t] for s in imp) - sum(s.get(t, 0.0) for s in exp)
                     + pv_hourly.get(t, 0.0) - batt.get(t, 0.0)
                     - sum(s.get(t, 0.0) for s in switched))
            local = dt_util.as_local(t)
            hod, weekend, temp = local.hour, local.weekday() >= 5, t24.get(t)
            boosted = int(t.timestamp()) in self.boost_hours
            skipped += boosted
            complete = True
            for key, _, series, deficit in parts:
                if all(t in s for s in series):
                    kwh = sum(s[t] for s in series)
                    if not (boosted and key == boosted_key):
                        samples[key].append((t, hod, kwh, temp, weekend, deficit.get(t) if deficit else None))
                    house -= kwh
                else:
                    complete = False
            if complete and not (boosted and boosted_key == "rest"):
                samples["rest"].append((t, hod, max(0.0, house), temp, weekend))

        recent = now - timedelta(days=7)
        names = {"rest": "overig verbruik" if parts else "huisverbruik",
                 **{part[0]: part[1] for part in parts}}
        models, corrections, thermostat, info, notes = {"rest": None, "hp": None}, {}, {}, {}, []
        for key in [part[0] for part in parts] + ["rest"]:
            rows = [s[1:5] for s in samples[key]]
            model = await self.hass.async_add_executor_job(fit, rows)
            models[key] = model
            if not model:
                continue
            corrections[key] = level_correction(
                model, [s[1:5] for s in samples[key] if s[0] >= recent])
            temp_part = (f", {model.k * 24:.1f} kWh per dag per graad onder {model.t_limit:.0f}°C"
                         if model.k > 0 else ", geen temperatuureffect gevonden")
            note = (f"{names[key]}: {len(rows) // 24} dagen{temp_part}"
                    + (", eigen weekendprofiel" if model.daytype else "")
                    + f", bijstelling laatste week ×{corrections[key]:.2f}")
            info[names[key]] = {
                "dagen": len(rows) // 24,
                "kwh_per_dag_per_graad": round(model.k * 24, 2),
                "grens": model.t_limit, "weekendprofiel": model.daytype,
                "bijstelling": round(corrections[key], 2),
                "basis_per_uur_kwh": [round(v, 3) for v in model.base],
            }
            if key not in ("rest", "hp") and any(s[5] is not None for s in samples[key]):
                idle, demand = thermostat_factors(model, [s[1:] for s in samples[key]], THERMOSTAT_BAND)
                if (idle, demand) != (1.0, 1.0):
                    thermostat[key] = (idle, demand)
                    note += f", ×{idle:.1f} als hij op temperatuur is en ×{demand:.1f} als hij warmte vraagt"
                    info[names[key]]["thermostaat"] = {"op_temperatuur": round(idle, 2),
                                                       "vraagt_warmte": round(demand, 2)}
            notes.append(note)
        self.models, self.corrections, self.thermostat = models, corrections, thermostat
        self.model_names, self.model_info = names, info
        for group in self.pv_groups:
            if len(group) > 1:
                notes.append(" en ".join(group) + " meten hetzelfde en tellen één keer mee")
        if skipped:
            notes.append(f"{skipped} uur met verhoogde thermostaat niet meegeteld voor de "
                         + ("warmtepomp" if boosted_key == "hp" else "rest"))
        if short:
            notes.append("nog te weinig historie voor: " + ", ".join(short))
        self.model_note = "; ".join(notes) if notes else (
            "Te weinig historie (minder dan twee dagen); er wordt met een vast verbruik gerekend.")

    async def _temperature_forecast(self, starts):
        """Gemiddelde buitentemperatuur over de voorgaande 24 uur, per slot."""
        temps = dict(self._recent_temps)
        entity_id = self.opt.get(CONF_WEATHER)
        if entity_id:
            try:
                result = await self.hass.services.async_call(
                    "weather", "get_forecasts", {"entity_id": entity_id, "type": "hourly"},
                    blocking=True, return_response=True)
                for item in (result or {}).get(entity_id, {}).get("forecast", []):
                    if item.get("temperature") is not None:
                        temps[dt_util.as_utc(fc.parse_time(item["datetime"]))] = float(item["temperature"])
            except Exception as err:
                _LOGGER.warning("Weersverwachting niet beschikbaar: %s", err)
        if not temps:
            return [None] * len(starts)
        t24 = rolling_mean_24(temps)
        keys = sorted(t24)
        out = []
        for start in starts:
            utc = dt_util.as_utc(start)
            past = [k for k in keys if k <= utc]
            out.append(t24[past[-1]] if past else t24[keys[0]])
        return out

    def _thermostat_factor(self, key) -> float:
        factors = self.thermostat.get(key)
        consumer = next((c for c in self.opt.get(CONF_CONSUMERS) or [] if c[C_ID] == key), None)
        if not factors or not consumer:
            return 1.0
        wanted = _float(self.hass.states.get(consumer.get(C_SETPOINT) or ""))
        current = _float(self.hass.states.get(consumer.get(C_CURRENT) or ""))
        if wanted is None or current is None:
            return 1.0
        return factors[1] if wanted - current > THERMOSTAT_BAND else factors[0]

    async def _load(self, starts, hours, now):
        if self._model_at is None or now - self._model_at > timedelta(hours=6):
            self._model_at = now
            try:
                started = time.perf_counter()
                await self._learn(now)
                self.timing["leren_ms"] = round((time.perf_counter() - started) * 1000)
            except Exception as err:
                _LOGGER.warning("Verbruik leren mislukt: %s", err)
                self.model_note = f"Leren mislukt ({err}); er wordt met een vast verbruik gerekend."
        temps = await self._temperature_forecast(starts)
        rest = self.models.get("rest")
        parts = {}
        for key, model in self.models.items():
            if model is None:
                continue
            factor = self._thermostat_factor(key)
            values = []
            for start, h, temp in zip(starts, hours, temps):
                value = model.predict(start.hour, temp, start.weekday() >= 5) * self.corrections.get(key, 1.0)
                if factor != 1.0:
                    ahead = (dt_util.as_utc(start) - dt_util.as_utc(now)).total_seconds() / 3600
                    value *= 1 + (factor - 1) * thermostat_weight(ahead)
                values.append(value * h)
            parts[self.model_names.get(key, key)] = values
        if rest is None:
            parts["huisverbruik"] = [DEFAULT_LOAD_W / 1000 * h for h in hours]
        load = [sum(column) for column in zip(*parts.values())]
        hp_part = parts.get("warmtepomp", [0.0] * len(starts)) if self.models.get("hp") else [0.0] * len(starts)
        self.load_parts = parts
        return load, hp_part, temps

    # --- vooruitblik, elk kwartier ----------------------------------------

    @callback
    def _on_quarter(self, now) -> None:
        self.hass.async_create_task(self.async_plan())

    async def async_plan(self) -> None:
        if self._planning:
            return
        self._planning = True
        started = time.perf_counter()
        try:
            await self._plan()
            self.timing["plan_ms"] = round((time.perf_counter() - started) * 1000)
        except Exception:
            _LOGGER.exception("Vooruitblik mislukt")
            self.look = {"tekst": "De vooruitblik is mislukt; zie het logboek van Home Assistant."}
        finally:
            self._planning = False
            self._notify()

    def _pv_meter_kwh(self) -> dict[str, float]:
        """Stand van elke zonneteller die nu een waarde heeft (kWh)."""
        out = {}
        for entity_id in self.opt.get(CONF_PV_ENERGY) or []:
            state = self.hass.states.get(entity_id)
            value = _float(state)
            if value is None:
                continue
            unit = (state.attributes.get("unit_of_measurement") or "kWh").lower()
            out[entity_id] = value / 1000 if unit == "wh" else value * 1000 if unit == "mwh" else value
        return out

    def _pv_delta_kwh(self, before: dict, after: dict) -> float | None:
        """Zonopwek tussen twee standen.

        Binnen een groep (dezelfde meter onder twee namen) telt de teller die werkt.
        Een teller zonder waarde, zoals die van een verwijderde integratie, telt niet
        mee; zonder één werkende teller is de opwek onbekend.
        """
        return pv_delta(before, after, self.opt.get(CONF_PV_ENERGY) or [], self.pv_groups)

    def _close_record(self) -> None:
        """Legt vast wat er in het afgelopen kwartier werkelijk gebeurde."""
        rec, acc = self._record, self._acc
        if rec is None:
            return
        if acc["sec"] > 60:
            rec["net_w"] = round(acc["met"] / acc["sec"] * 3600 * 1000)
            rec["net_zonder_accu_w"] = round(acc["zonder"] / acc["sec"] * 3600 * 1000)
            sun = self._pv_delta_kwh(self._marks, self._pv_meter_kwh())
            if sun is not None:
                rec["werkelijk_zon_w"] = round(sun / acc["sec"] * 3600 * 1000)
                rec["werkelijk_verbruik_w"] = max(0, rec["net_zonder_accu_w"] + rec["werkelijk_zon_w"])
        names = {b.id: b.name for b in self.buffers()}
        rec["verschoven_min"] = {names.get(k, k): round(v / 60) for k, v in acc["flex"].items() if v >= 30}
        self.history.append(rec)
        self.history = self.history[-7 * 96:]
        self._record = None
        self._acc = self._empty_acc()

    async def _plan(self) -> None:
        self.opt = {**self.entry.data, **self.entry.options}
        now = dt_util.now()
        count = int(self.opt.get(CONF_HORIZON, 36) * 60 / SLOT_MIN)
        utc_starts, hours = slot_starts(dt_util.utcnow(), count, SLOT_MIN)
        starts = [dt_util.as_local(t) for t in utc_starts]
        if self._record is not None and self._record["t"] != starts[0].isoformat():
            self._close_record()

        prices, price_note, origins = self._prices(starts)
        starts, hours = starts[:len(prices)], hours[:len(prices)]
        pv = await self._pv(starts, hours)
        load, hp_part, temps = await self._load(starts, hours, now)
        slots = [Slot(h, ld, p, buy, sell) for h, ld, p, (buy, sell) in zip(hours, load, pv, prices)]
        self.prices_now = prices[0]

        units = self.units()
        if units:
            bat, soc = aggregate(units, 0.0)
        else:
            bat, soc = Battery(0, 0, 0, 0, 1, 1, 0), 0.0
        look = await self.hass.async_add_executor_job(outlook, slots, bat, soc)
        steps = look.steps
        for buf in self.buffers():
            ahead = max(1, math.ceil(buf.min_on_s / 60 / SLOT_MIN))
            self._forecast_w[buf.id] = export_ahead_w(steps, slots, ahead)

        def at(i):
            return None if i is None else starts[i].isoformat()

        has_battery = bool(self.batteries)
        text = describe(look, starts, has_battery and bool(units))
        if has_battery and not units:
            text = "De laadtoestand van de accu is niet bekend; er wordt zonder accu gerekend. " + text
        self.look = {
            "tekst": text,
            "accu_vol_om": at(look.full_at),
            "overschot_na_vol_kwh": round(look.surplus_after_full_kwh, 1),
            "accu_leeg_om": at(look.empty_at),
            "net_na_leeg_kwh": round(look.grid_after_empty_kwh, 1),
            "teruglevering_24u_kwh": round(look.surplus_kwh, 1),
            "net_24u_kwh": round(look.grid_kwh, 1),
            "net_dure_uren_kwh": round(look.grid_expensive_kwh, 1),
            "dure_uren_vanaf_eur": None if look.expensive_from is None else round(look.expensive_from, 3),
            "kosten_24u_eur": round(sum(s.cost for s in steps[:look.window]), 2),
            "bijgewerkt": now.isoformat(timespec="seconds"),
        }

        def watts(values):
            return [round(v / h * 1000) for v, h in zip(values, hours)]

        cap = sum(u.capacity_kwh for u in units) or 1.0
        day = min(96, len(slots))
        self.plan = {
            "start": starts[0].isoformat(), "stap_min": SLOT_MIN,
            "prijzen": price_note,
            "prijs_herkomst": origins[:len(slots)],
            "accu_w": watts([s.accu_ac_kwh for s in steps]),
            "net_w": watts([s.grid_kwh for s in steps]),
            "soc_pct": [round(s.soc_kwh / cap * 100) for s in steps] if units else [],
            "koop": [round(s.buy, 4) for s in slots],
            "verkoop": [round(s.sell, 4) for s in slots],
            "zon_w": watts(pv), "verbruik_w": watts(load), "warmtepomp_w": watts(hp_part),
            "buitentemp_24u": [None if t is None else round(t, 1) for t in temps],
            "verbruik_24u_kwh": round(sum(load[:day]), 1),
            "zon_24u_kwh": round(sum(pv[:day]), 1),
            "warmtepomp_24u_kwh": round(sum(hp_part[:day]), 1),
            "onderdelen_24u_kwh": {name: round(sum(values[:day]), 1)
                                   for name, values in self.load_parts.items()},
        }
        if self._record is None:
            # De verwachting zoals die bij het begin van het kwartier was.
            self._record = {
                "t": starts[0].isoformat(),
                "koop": round(slots[0].buy, 4), "verkoop": round(slots[0].sell, 4),
                "soc_pct": round(soc / cap * 100) if units else None,
                "verwacht_verbruik_w": round(load[0] / hours[0] * 1000),
                "verwacht_zon_w": round(pv[0] / hours[0] * 1000),
                "verwacht_net_w": round(steps[0].grid_kwh / hours[0] * 1000),
                "verwacht_accu_w": round(steps[0].accu_ac_kwh / hours[0] * 1000),
            }
            self._marks = self._pv_meter_kwh()

    def forecast_quality(self, days: int = 7) -> dict:
        """Hoe goed de verwachting was: verbruik en zon, over de afgelopen dagen."""
        cutoff = (dt_util.now() - timedelta(days=days)).isoformat()
        rows = [r for r in self.history if r["t"] >= cutoff]
        use = [r for r in rows if r.get("werkelijk_verbruik_w") is not None]
        out = {"dagen": days, "kwartieren": len(use)}
        if use:
            actual = sum(r["werkelijk_verbruik_w"] for r in use)
            out["verbruik_gemiddelde_afwijking_w"] = round(
                sum(abs(r["verwacht_verbruik_w"] - r["werkelijk_verbruik_w"]) for r in use) / len(use))
            out["verbruik_verschil_pct"] = round(
                (sum(r["verwacht_verbruik_w"] for r in use) - actual) / actual * 100, 1) if actual else None
        sun = [r for r in rows if r.get("werkelijk_zon_w") is not None]
        expected = sum(r["verwacht_zon_w"] for r in sun)
        if sun and expected > 0:
            out["zon_werkelijk_gedeeld_door_verwacht"] = round(sum(r["werkelijk_zon_w"] for r in sun) / expected, 2)
        return out

    # --- elke meting ------------------------------------------------------

    @callback
    def _on_sample(self, sample) -> None:
        started = time.perf_counter()
        self._handle_sample(sample)
        took = (time.perf_counter() - started) * 1000
        previous = self.timing["meting_ms"]
        self.timing["meting_ms"] = took if previous is None else previous + (took - previous) * 0.02

    def _handle_sample(self, sample) -> None:
        now = dt_util.utcnow()
        last = self._last_rt
        if last is not None and (now - last).total_seconds() < 0.5:
            return
        self._last_rt = now
        if last is None:
            return
        dt_s = min(60.0, (now - last).total_seconds())
        self.meter_stale = False
        p1 = sample.power_w
        real = self._real_battery_power()
        self.grid_without = p1 - real

        # Virtuele accu's doen wat een accu op zelfverbruik doet: de meter volgen.
        grid, virtual_w = p1, 0.0
        for u in (u for u in self.units() if u.virtual):
            p, soc = respond(ZELFVERBRUIK, 0, u, grid, dt_s)
            grid += p
            virtual_w += p
            self.virtual_soc[u.id] = soc
            self.actual_power[u.id] = p
        self.grid_with = grid
        self.battery_w = real + virtual_w

        self._run_buffers(grid, self.battery_w, now, dt_s)

        buy, sell = self.prices_now
        if buy is not None:
            day = dt_util.now().date().isoformat()
            if self.costs["dag"] != day:
                self.costs.update({"dag": day, "dag_zonder": 0.0, "dag_met": 0.0})
            for key, watt in (("zonder", self.grid_without), ("met", self.grid_with)):
                kwh = watt / 1000 * dt_s / 3600
                euro = kwh * (buy if kwh > 0 else sell)
                self.costs[key] += euro
                self.costs["dag_" + key] += euro
                self._acc[key] += kwh
            self._acc["sec"] += dt_s

        if self._last_save is None or now - self._last_save > timedelta(minutes=5):
            self._last_save = now
            self.hass.async_create_task(self._save())
        if self._last_notify is None or now - self._last_notify >= timedelta(seconds=NOTIFY_SECONDS):
            self._last_notify = now
            self._notify()

    @callback
    def _on_watchdog(self, now) -> None:
        """Bewaakt of de meter nog actuele waarden geeft."""
        if self.meter.source != DIRECT:
            # Sensoren melden alleen wijzigingen; een gelijkblijvende waarde is niet verouderd.
            state = self.hass.states.get(self.opt.get(CONF_P1_POWER) or "")
            seen = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
            if state is not None and seen is not None and \
                    (dt_util.utcnow() - seen).total_seconds() < STALE_SECONDS:
                sample = self.meter.read_entities()
                if sample is not None:
                    self.meter.latest = sample
                    self._on_sample(sample)
                    return
        age = self.meter.age_s()
        if age is not None and age < STALE_SECONDS:
            return
        if not self.meter_stale:
            _LOGGER.warning("P1-meter geeft geen actuele waarden; verschuifbaar verbruik gaat uit")
        self.meter_stale = True
        self.grid_with = self.grid_without = None
        self._run_buffers(None, 0.0, dt_util.utcnow(), WATCHDOG_SECONDS)
        self._notify()

    # --- verschuifbaar verbruik -------------------------------------------

    def buffers(self) -> list[Buffer]:
        items = sorted(self.opt.get(CONF_STAGE2) or [],
                       key=lambda i: (i.get(L_ORDER, 1), i.get(L_NAME, "")))
        out = []
        for i in items:
            kind = i.get(L_KIND, SWITCH)
            out.append(Buffer(
                i[L_ID], i[L_NAME], float(i.get(L_POWER, 2000)),
                i.get(L_ON_DELAY, 5) * 60, i.get(L_OFF_DELAY, 2) * 60,
                i.get(L_MIN_ON, 15) * 60, i.get(L_MIN_OFF, 15) * 60,
                kind=kind, min_soc_pct=float(i.get(L_MIN_SOC, 90)),
                start_hour=float(i.get(L_START, 0)), end_hour=float(i.get(L_END, 24)),
                max_temp=i.get(L_MAX_TEMP), boost_c=float(i.get(L_BOOST, 1.0)),
                need_forecast=bool(i.get(L_FORECAST, kind == THERMOSTAT)),
                rest_after_s=float(i.get(L_REST, 0)) * 60))
        return out

    def _cfg(self, buf_id: str) -> dict:
        return next((i for i in self.opt.get(CONF_STAGE2) or [] if i[L_ID] == buf_id), {})

    def stage2_enabled(self) -> bool:
        return bool(self.settings["stage2"])

    def _actuated(self, buf: Buffer) -> bool:
        """Schakelt het EMS deze buffer echt? Alleen met schakelen aan en een entiteit."""
        return self.stage2_enabled() and bool(self._cfg(buf.id).get(L_ENTITY))

    def _entity_state(self, buf: Buffer):
        return self.hass.states.get(self._cfg(buf.id).get(L_ENTITY) or "")

    def _draw(self, buf: Buffer) -> float | None:
        """Gemeten vermogen van de buffer, of None zonder vermogenssensor."""
        value = _float(self.hass.states.get(self._cfg(buf.id).get(L_POWER_SENSOR) or ""))
        return None if value is None else max(0.0, value)

    def _target(self, buf: Buffer) -> float | None:
        return _attr_float(self._entity_state(buf), "temperature")

    def _reported(self, buf: Buffer) -> bool | None:
        """Staat de buffer volgens Home Assistant aan? None als dat niet te zeggen is."""
        state = self._entity_state(buf)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        if buf.kind == THERMOSTAT:
            rec, target = self.boost.get(buf.id), self._target(buf)
            if target is None:
                return None
            return rec is not None and abs(target - rec["gezet"]) < 0.05
        if state.state not in ("on", "off"):
            return None
        return state.state == "on"

    def _temperature(self, buf: Buffer) -> float | None:
        value = _float(self.hass.states.get(self._cfg(buf.id).get(L_TEMP) or ""))
        if value is None and buf.kind == THERMOSTAT:
            value = _attr_float(self._entity_state(buf), "current_temperature")
        return value

    def _unavailable(self, buf: Buffer) -> str | None:
        until = self.hands_off.get(buf.id)
        if until and time.time() < until:
            moment = dt_util.as_local(dt_util.utc_from_timestamp(until))
            return f"met de hand gewijzigd; het EMS blijft er tot {moment:%H:%M} vanaf"
        if not self._cfg(buf.id).get(L_ENTITY):
            return None   # alleen advies; de buffer bestaat alleen als signaal
        state = self._entity_state(buf)
        what = "thermostaat" if buf.kind == THERMOSTAT else "schakelaar"
        if state is None or state.state in ("unavailable", "unknown"):
            return f"{what} niet bereikbaar"
        if buf.kind == THERMOSTAT:
            if state.state == "off":
                return "thermostaat staat uit"
            if self._target(buf) is None:
                return "thermostaat heeft geen enkele ingestelde temperatuur"
        return None

    def _counts(self, buf: Buffer, st: LoadState) -> bool:
        """Telt het verbruik van deze buffer als verschoven verbruik?

        Schakelt het EMS zelf, dan alleen wat het zelf heeft aangezet. Bij alleen
        advies telt de buffer zodra het advies 'aan' is (je eigen automatisering
        kan het volgen).
        """
        if self._actuated(buf):
            return buf.id in self.owned or (buf.id in self._pending and self._pending[buf.id][0])
        return st.on

    def _counted_draw(self, buf: Buffer, st: LoadState) -> float:
        if not self._counts(buf, st):
            return 0.0
        measured = self._draw(buf)
        if measured is not None:
            return measured
        if buf.kind == SWITCH:
            reported = self._reported(buf)
            return buf.power_w if reported or (reported is None and st.on) else 0.0
        return buf.power_w

    def _context(self, now) -> Context:
        local = dt_util.as_local(now)
        soc, problem = self.soc_summary()
        bufs = self.buffers()
        return Context(
            hour=local.hour + local.minute / 60, soc_pct=soc, soc_problem=problem,
            temps={b.id: self._temperature(b) for b in bufs},
            forecast_w=dict(self._forecast_w),
            unavailable={b.id: r for b in bufs if (r := self._unavailable(b))},
            resting=self.resting)

    def _own(self, buf_id: str, owned: bool) -> None:
        if (buf_id in self.owned) != owned:
            (self.owned.add if owned else self.owned.discard)(buf_id)
            if not owned:
                self.boost.pop(buf_id, None)
            self.hass.async_create_task(self._save())

    async def _call(self, buf: Buffer, on: bool) -> None:
        """Voert één schakeling uit."""
        entity_id = self._cfg(buf.id).get(L_ENTITY)
        if buf.kind == SWITCH:
            await self.hass.services.async_call(
                "homeassistant", "turn_on" if on else "turn_off", {"entity_id": entity_id}, blocking=True)
            return
        state = self._entity_state(buf)
        target = self._target(buf)
        rec = self.boost.get(buf.id)
        if on:
            normal = rec["normaal"] if rec else target
            if normal is None:
                raise RuntimeError("geen ingestelde temperatuur")
            step = _attr_float(state, "target_temp_step") or 0.5
            high = boost_target(normal, buf.boost_c, step)
            top = _attr_float(state, "max_temp")
            if top is not None:
                high = min(high, top)
            self.boost[buf.id] = {"normaal": normal, "gezet": high}
            await self.hass.services.async_call(
                "climate", "set_temperature", {"entity_id": entity_id, "temperature": high}, blocking=True)
        elif rec and target is not None and abs(target - rec["gezet"]) < 0.05:
            await self.hass.services.async_call(
                "climate", "set_temperature", {"entity_id": entity_id, "temperature": rec["normaal"]},
                blocking=True)

    def _switch(self, buf: Buffer, on: bool, tries: int = 1) -> None:
        """Vraagt een schakeling aan; de bevestiging volgt uit de gemelde stand."""
        self._pending[buf.id] = (on, self.hass.loop.time(), tries)

        async def run():
            try:
                await self._call(buf, on)
            except Exception as err:  # de bevestiging blijft dan uit; daar wordt op gereageerd
                _LOGGER.warning("Schakelen van %s mislukt: %s", buf.name, err)
        self.hass.async_create_task(run())

    async def _release_all(self) -> None:
        """Zet alles wat het EMS had aangezet terug naar normaal."""
        for buf in self.buffers():
            if buf.id not in self.owned or not self._cfg(buf.id).get(L_ENTITY):
                continue
            try:
                await self._call(buf, False)
            except Exception as err:
                _LOGGER.warning("Terugzetten van %s mislukt: %s", buf.name, err)
            self.owned.discard(buf.id)
            self.boost.pop(buf.id, None)
            st = self.flex.get(buf.id)
            if st:
                st.on, st.want_since = False, None
        self._pending.clear()

    def _confirm(self, buf: Buffer, st: LoadState, ts: float, mono: float) -> bool:
        """Verwerkt de gemelde stand. Geeft False als het EMS deze buffer niet beslist."""
        reported, pending = self._reported(buf), self._pending.get(buf.id)
        if pending:
            target, since, tries = pending
            if reported == target:
                del self._pending[buf.id]
                self.flex_issue.pop(buf.id, None)
                st.since = ts   # de minimale draai- en rusttijd lopen vanaf nu
                self._own(buf.id, target)
            elif mono - since > SWITCH_CONFIRM_SECONDS:
                if tries < SWITCH_TRIES:
                    self._switch(buf, target, tries + 1)
                else:
                    del self._pending[buf.id]
                    st.on, st.since, st.want_since = bool(reported), ts, None
                    self.flex_issue[buf.id] = (
                        f"{'aan' if target else 'uit'}zetten is {SWITCH_TRIES} keer geprobeerd maar "
                        "niet bevestigd; de entiteit meldt "
                        + ("geen stand" if reported is None else "aan" if reported else "uit"))
                    self._own(buf.id, bool(reported))
        elif reported and buf.id not in self.owned:
            # Door iemand anders aangezet: het EMS neemt hem niet over en zet hem niet uit.
            st.on, st.want_since = False, None
            st.note = "handmatig aan; het EMS schakelt hem niet"
            return False
        elif reported is False and buf.id in self.owned:
            # Door iemand anders uitgezet of veranderd terwijl het EMS hem aan had: respecteren,
            # en een tijd niet opnieuw beginnen.
            self._own(buf.id, False)
            st.on, st.since, st.want_since = False, ts, None
            self.hands_off[buf.id] = ts + HANDS_OFF_SECONDS
            until = dt_util.as_local(dt_util.utc_from_timestamp(ts + HANDS_OFF_SECONDS))
            st.note = (f"uit: met de hand {'anders ingesteld' if buf.kind == THERMOSTAT else 'uitgezet'}; "
                       f"het EMS blijft er tot {until:%H:%M} vanaf")
        elif reported and not st.on:
            st.on, st.since = True, ts   # na een herstart: door het EMS aangezet, weer oppakken
        return True

    def _run_buffers(self, grid_w, battery_w, now, dt_s) -> None:
        bufs = self.buffers()
        if not bufs:
            return
        ts, mono = now.timestamp(), self.hass.loop.time()
        managed = []
        for buf in bufs:
            st = self.flex.setdefault(buf.id, LoadState())
            if not self._actuated(buf):
                self._pending.pop(buf.id, None)
                managed.append(buf)
            elif self._confirm(buf, st, ts, mono):
                managed.append(buf)

        counted = {b.id: self._counted_draw(b, self.flex[b.id]) for b in bufs}
        self.flex_power = sum(counted.values())
        if grid_w is None:
            self.spare_w = None
        else:
            spare = spare_w(grid_w, battery_w, self.flex_power)
            weight = 1.0 if self.spare_w is None else 1 - math.exp(-dt_s / SPARE_SMOOTH_SECONDS)
            self.spare_w = spare if self.spare_w is None else self.spare_w + (spare - self.spare_w) * weight

        for buf in bufs:   # neemt een buffer die aan staat nog iets af?
            st = self.flex[buf.id]
            until = self._idle.update(buf, self._counts(buf, st) and st.on, self._draw(buf), ts)
            if until:
                self.resting[buf.id] = until
            if counted[buf.id] > 0 or (self._counts(buf, st) and st.on):
                self._acc["flex"][buf.id] = self._acc["flex"].get(buf.id, 0.0) + dt_s
                if buf.kind == THERMOSTAT and self._actuated(buf) and buf.id in self.owned:
                    self.boost_hours.add(int(ts // 3600 * 3600))
        ctx = self._context(now)
        for buf, on in decide(managed, self.flex, self.spare_w, ts,
                              blocked=lambda b: start_block(b, ctx, ts),
                              stop=lambda b: stop_reason(b, ctx, ts)):
            if self._actuated(buf):
                self._switch(buf, on)

    def flex_overview(self) -> list[dict]:
        ctx = self._context(dt_util.utcnow())
        out = []
        for buf in self.buffers():
            st = self.flex.get(buf.id, LoadState())
            reported, pending = self._reported(buf), self._pending.get(buf.id)
            if buf.kind == THERMOSTAT:
                target = self._target(buf)
                stand = "onbekend" if target is None else f"{target:.1f} °C ingesteld"
                if buf.id in self.boost:
                    stand += f" (normaal {self.boost[buf.id]['normaal']:.1f} °C)"
            else:
                stand = "onbekend" if reported is None else "aan" if reported else "uit"
            out.append({
                "naam": buf.name, "soort": buf.kind, "aan": st.on,
                "vermogen_w": round(buf.power_w),
                "verbruikt_w": None if self._draw(buf) is None else round(self._draw(buf)),
                "stand": stand,
                "temperatuur": ctx.temps.get(buf.id),
                "verwacht_overschot_w": None if buf.id not in self._forecast_w
                else round(self._forecast_w[buf.id]),
                "wachtend": None if not pending else
                f"{'aan' if pending[0] else 'uit'}zetten aangevraagd (poging {pending[2]})",
                "beheer": ("advies" if not self._actuated(buf) else "EMS" if buf.id in self.owned
                           else "handmatig" if reported else "geen"),
                "toelichting": self.flex_issue.get(buf.id) or st.note})
        return out

    def battery_overview(self) -> list[dict]:
        units = {u.id: u for u in self.units()}
        out = []
        for b in self.batteries:
            unit = units.get(b[B_ID])
            actual = self.actual_power.get(b[B_ID], 0.0) if b.get(B_VIRTUAL, True) else self._measured_power(b)
            out.append({
                "naam": b[B_NAME],
                "soort": {DRIVER_VIRTUAL: "virtueel", DRIVER_ZENDURE: "Zendure lokaal"}.get(
                    b.get(B_DRIVER, DRIVER_VIRTUAL), "via entiteiten"),
                "lading_pct": round(unit.soc_kwh / unit.capacity_kwh * 100) if unit else None,
                "vermogen_w": round(actual),
                "laadtoestand": self.soc_issue.get(b[B_ID], "actueel"),
                "laadtoestand_leeftijd_s": self._soc_age(b),
            })
        return out

    def status_note(self) -> str:
        notes = []
        for buf in self.buffers():
            if buf.id in self.flex_issue:
                notes.append(f"{buf.name}: {self.flex_issue[buf.id]}.")
        running = [b.name for b in self.buffers() if self.flex.get(b.id, LoadState()).on]
        if running:
            notes.append(", ".join(running) + (" neemt nu zonnestroom op." if len(running) == 1
                                               else " nemen nu zonnestroom op.")
                         + ("" if self.stage2_enabled() else " (Alleen advies: schakelen staat uit.)"))
        for battery in self.batteries:
            if battery[B_ID] in self.soc_issue:
                notes.append(f"De laadtoestand van {battery[B_NAME]} {self.soc_issue[battery[B_ID]]}.")
        if self.meter_stale:
            notes.append("De P1-meter geeft geen actuele waarden; verschuifbaar verbruik gaat uit.")
        return " ".join(notes)
