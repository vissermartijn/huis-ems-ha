"""Het EMS: voorspelt verbruik per onderdeel, de zon en wat de accu doet.

Het verbruik wordt opgedeeld in wat het EMS apart kan voorspellen:

- **warmtepomp verwarmen**: hangt af van de buitentemperatuur (stooklijn);
- **warmtepomp tapwater**: cycli, herkend aan de melding "tapwater bezig";
- **verbruikers met een eigen meter** (zoals een hottub): eigen model per uur en
  temperatuur, eventueel bijgesteld op de thermostaat;
- **apparaten met programma's** (Home Connect): wat loopt of gepland staat, plus
  de gewoonte uit de afgelopen weken;
- **overig**: patroon per uur van de dag, de temperatuur en wat er nu gebeurt.

De accu regelt zichzelf op zelfverbruik; het EMS leest hem alleen uit en rekent
vooruit wanneer hij vol en leeg raakt. Elk kwartier legt het vast wat het
verwachtte (vlak vooraf en een etmaal vooraf) en wat er werkelijk gebeurde.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta
from statistics import median

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_state_change_event, async_track_time_change, async_track_time_interval)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import *  # noqa: F401,F403
from .core import cycles as cy
from .core import forecast as fc
from .core.battery import Battery, Slot, Unit, aggregate, describe, follow_meter, outlook
from .core.loadmodel import (
    fit, level_correction, rolling_mean_24, thermostat_factors, thermostat_weight)
from .core.series import delta as pv_delta, duplicate_groups, merge
from .core.timeline import slot_starts
from .drivers import ZendureDriver
from .meter import DIRECT, Meter

_LOGGER = logging.getLogger(__name__)
DEFAULT_LOAD_W = 400.0
DAY_AHEAD_SLOTS = 96          # de voorspelling van een etmaal vooraf wordt bewaard
REST, HP, DHW = "rest", "hp", "dhw"


def _float(state):
    try:
        value = float(state.state)
        return value if math.isfinite(value) else None
    except (AttributeError, TypeError, ValueError):
        return None


def _time(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return dt_util.utc_from_timestamp(value)


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
        self.day_ahead: dict[str, dict] = {}      # kwartier -> voorspelling van een etmaal vooraf
        self.grid_without: float | None = None
        self.grid_with: float | None = None
        self.battery_w: float = 0.0
        self.plan: dict = {}
        self.look: dict = {"tekst": "Nog geen vooruitblik berekend."}
        self.models: dict = {REST: None, HP: None}
        self.corrections: dict[str, float] = {}
        self.thermostat: dict[str, tuple[float, float]] = {}
        self.habits: dict[str, cy.Habit] = {}
        self.cycles: dict[str, list] = {}
        self.cycle_log: dict = {"since": {}, "cycles": {}}   # eigen logboek, los van de recorder
        self.model_note = "Nog niet geleerd; er wordt met een vast verbruik gerekend."
        self.model_info: dict[str, dict] = {}
        self.anomaly: dict[str, float] = {}
        self.cycle_info: dict[str, dict] = {}
        self.cycle_state: dict[str, str] = {}
        self._basis: dict[str, float] = {}       # verwachting van nu zonder bijsturing (W)
        self._long_note = self.model_note
        self.timing = {"plan_ms": None, "leren_ms": None, "meting_ms": None}
        self.soc_issue: dict[str, str] = {}
        self._recent_temps: dict[datetime, float] = {}
        self._model_at: datetime | None = None
        self._cycles_at: datetime | None = None
        self._record: dict | None = None
        self._marks: dict[str, float] = {}        # tellerstanden bij het begin van het kwartier
        self.pv_groups: list[list[str]] = []      # zonnetellers die dezelfde meter zijn
        self._acc = self._empty_acc()
        self._last_rt: datetime | None = None
        self._last_save: datetime | None = None
        self._last_notify: datetime | None = None
        self._planning = False
        self._replan = False
        self.meter = Meter(hass, self.opt, self._on_sample)
        self.drivers: dict[str, ZendureDriver] = {}
        self.meter_stale = False
        self._listeners: list = []
        self._unsubs: list = []

    @staticmethod
    def _empty_acc():
        return {"zonder": 0.0, "met": 0.0, "sec": 0.0, "aan": {}}

    # --- onderdelen ---------------------------------------------------------

    @property
    def batteries(self) -> list[dict]:
        return self.opt.get(CONF_BATTERIES, [])

    @property
    def consumers(self) -> list[dict]:
        return [c for c in self.opt.get(CONF_CONSUMERS) or [] if c.get(C_ENERGY)]

    @property
    def appliances(self) -> list[dict]:
        return [a for a in self.opt.get(CONF_APPLIANCES) or [] if a.get(A_STATE)]

    @property
    def has_dhw(self) -> bool:
        return bool(self.opt.get(CONF_HP_ENERGY) and self.opt.get(CONF_DHW_ACTIVE))

    def names(self) -> dict[str, str]:
        """Onderdeel -> naam in het dashboard, in een vaste volgorde."""
        out = {}
        if self.opt.get(CONF_HP_ENERGY):
            out[HP] = "warmtepomp verwarmen" if self.has_dhw else "warmtepomp"
            if self.has_dhw:
                out[DHW] = "warmtepomp tapwater"
        for c in self.consumers:
            out[c[C_ID]] = c[C_NAME]
        for a in self.appliances:
            out[a[A_ID]] = a[A_NAME]
        out[REST] = "overig"
        return out

    def _cycle_sources(self) -> dict[str, dict]:
        """Onderdelen in cycli: {sleutel: {soort, toestand, aan, uitgesteld, einde, meters}}."""
        out = {}
        if self.has_dhw:
            out[DHW] = {"soort": "tapwater", "toestand": self.opt[CONF_DHW_ACTIVE], "aan": {"on"},
                        "uitgesteld": set(), "einde": None, "meters": self.opt.get(CONF_HP_ENERGY) or [],
                        "kwh": None, "min": None}
        for a in self.appliances:
            out[a[A_ID]] = {"soort": a.get(A_KIND, "overig"), "toestand": a[A_STATE], "aan": RUN_STATES,
                            "uitgesteld": DELAYED_STATES, "einde": a.get(A_FINISH),
                            "meters": [a[A_ENERGY]] if a.get(A_ENERGY) else [],
                            "kwh": a.get(A_KWH), "min": a.get(A_MINUTES),
                            "programma": a.get(A_PROGRAM)}
        return out

    # --- levensloop -------------------------------------------------------

    async def async_start(self) -> None:
        data = await self.store.async_load() or {}
        self.settings.update({k: v for k, v in data.get("settings", {}).items() if k in DEFAULT_SETTINGS})
        self.virtual_soc = data.get("virtual_soc", {})
        # Kwartieren van voor versie 0.7 hebben geen onderdelen; die tellen niet meer mee.
        self.history = [r for r in data.get("history", []) if "verwacht" in r]
        self.day_ahead = data.get("day_ahead", {})
        log = data.get("cycle_log") or {}
        self.cycle_log = {"since": dict(log.get("since") or {}), "cycles": dict(log.get("cycles") or {})}
        self._unsubs.append(async_track_time_change(
            self.hass, self._on_quarter, minute=range(0, 60, SLOT_MIN), second=5))
        self._unsubs.append(async_track_time_interval(
            self.hass, self._on_watchdog, timedelta(seconds=WATCHDOG_SECONDS)))
        watched = [s["toestand"] for s in self._cycle_sources().values()]
        if watched:
            self._unsubs.append(async_track_state_change_event(self.hass, watched, self._on_cycle_state))
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
        for driver in self.drivers.values():
            await driver.async_stop()
        await self._save()

    async def _save(self) -> None:
        cutoff = (dt_util.now() - timedelta(hours=2)).isoformat()
        self.day_ahead = {k: v for k, v in self.day_ahead.items() if k >= cutoff}
        await self.store.async_save({
            "settings": self.settings, "virtual_soc": self.virtual_soc,
            "history": self.history[-HISTORY_DAYS * 96:], "day_ahead": self.day_ahead,
            "cycle_log": self.cycle_log})

    @callback
    def async_add_listener(self, update):
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    async def async_set(self, key: str, value) -> None:
        self.settings[key] = value
        await self._save()
        await self.async_plan()

    @callback
    def _on_cycle_state(self, event) -> None:
        """Een apparaat start, stopt of wordt uitgesteld: opnieuw vooruitkijken."""
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is not None and new is not None and old.state == new.state:
            return
        self._cycles_at = None
        self.hass.async_create_task(self.async_plan())

    # --- accu's: alleen uitlezen ------------------------------------------

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

    async def _statistics(self, ids, start, types, period="hour"):
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period
        if not ids:
            return {}
        return await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, None, set(ids), period, None, types)

    async def _state_history(self, entity_id, start):
        """[(tijdstip, toestand)] uit de recorder, zo ver terug als hij bewaart."""
        from homeassistant.components.recorder import get_instance, history
        rows = await get_instance(self.hass).async_add_executor_job(
            history.state_changes_during_period, self.hass, start, None, entity_id, True, False, None, True)
        return [(s.last_changed, s.state) for s in rows.get(entity_id, [])]

    @staticmethod
    def _hourly(rows, key):
        out = {}
        for row in rows or []:
            value = row.get(key)
            if value is None:
                continue
            out[_time(row["start"])] = float(value)
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

    # --- leren ------------------------------------------------------------

    async def _learn(self, now) -> None:
        """Leert het verbruik per uur uit de langetermijnstatistieken van HA."""
        o = self.opt
        imports = o.get(CONF_IMPORT_ENERGY) or []
        exports = o.get(CONF_EXPORT_ENERGY) or []
        pvs = o.get(CONF_PV_ENERGY) or []
        hps = o.get(CONF_HP_ENERGY) or []
        consumers = self.consumers
        temp_id = o.get(CONF_OUTDOOR_TEMP)
        names = self.names()
        if not imports:
            self.model_note = "Geen afnamemeters ingesteld; er wordt met een vast verbruik gerekend."
            return
        start = now - timedelta(days=365)
        ids = imports + exports + pvs + hps + [i for c in consumers for i in c[C_ENERGY]]
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

        imp, exp, pv = total(imports), total(exports), total(pvs)
        # Oude en nieuwe teller van dezelfde omvormer tellen samen maar één keer.
        groups = duplicate_groups(pv)
        self.pv_groups = [[pvs[i] for i in g] for g in groups]
        pv_hourly = merge(pv, groups)
        # Uren zonder enige zonmeting zeggen niets over het verbruik overdag.
        pv_known = set().union(*(s.keys() for s in pv)) if pv else None
        parts, short = [], []
        if hps:
            parts.append((HP, total(hps), None))
        for consumer in consumers:
            parts.append((consumer[C_ID], total(consumer[C_ENERGY]), await self._deficits(consumer, start)))
        for part in list(parts):
            if min(len(s) for s in part[1]) < 48:
                parts.remove(part)
                short.append(names.get(part[0], part[0]))
        samples = {part[0]: [] for part in parts}
        samples[REST] = []
        skipped_sun = 0
        for t in sorted(imp[0]):
            if any(t not in s for s in imp):
                continue
            house = (sum(s[t] for s in imp) - sum(s.get(t, 0.0) for s in exp)
                     + pv_hourly.get(t, 0.0) - batt.get(t, 0.0))
            local = dt_util.as_local(t)
            hod, weekend, temp = local.hour, local.weekday() >= 5, t24.get(t)
            complete = True
            for key, series, deficit in parts:
                if all(t in s for s in series):
                    kwh = sum(s[t] for s in series)
                    samples[key].append((t, hod, kwh, temp, weekend, deficit.get(t) if deficit else None))
                    house -= kwh
                else:
                    complete = False
            sunless = pv_known is not None and t not in pv_known and 7 <= hod <= 19
            skipped_sun += sunless
            if complete and not sunless:
                samples[REST].append((t, hod, max(0.0, house), temp, weekend))

        recent = now - timedelta(days=7)
        models, corrections, thermostat, info, notes = {REST: None, HP: None}, {}, {}, {}, []
        for key in [part[0] for part in parts] + [REST]:
            rows = [s[1:5] for s in samples[key]]
            model = await self.hass.async_add_executor_job(fit, rows)
            models[key] = model
            name = names.get(key, key) if key != HP else "warmtepomp"
            if not model:
                continue
            corrections[key] = level_correction(model, [s[1:5] for s in samples[key] if s[0] >= recent])
            temp_part = (f", {model.k * 24:.1f} kWh per dag per graad onder {model.t_limit:.0f}°C"
                         if model.k > 0 else ", geen temperatuureffect gevonden")
            note = (f"{name}: {len(rows) // 24} dagen{temp_part}"
                    + (", eigen weekendprofiel" if model.daytype else "")
                    + f", bijstelling laatste week ×{corrections[key]:.2f}")
            info[name] = {
                "dagen": len(rows) // 24,
                "kwh_per_dag_per_graad": round(model.k * 24, 2),
                "grens": model.t_limit, "weekendprofiel": model.daytype,
                "bijstelling": round(corrections[key], 2),
                "basis_per_uur_kwh": [round(v, 3) for v in model.base],
            }
            if key not in (REST, HP) and any(s[5] is not None for s in samples[key]):
                idle, demand = thermostat_factors(model, [s[1:] for s in samples[key]], THERMOSTAT_BAND)
                if (idle, demand) != (1.0, 1.0):
                    thermostat[key] = (idle, demand)
                    note += f", ×{idle:.1f} als hij op temperatuur is en ×{demand:.1f} als hij warmte vraagt"
                    info[name]["thermostaat"] = {"op_temperatuur": round(idle, 2),
                                                 "vraagt_warmte": round(demand, 2)}
            notes.append(note)
        self.models, self.corrections, self.thermostat = models, corrections, thermostat
        self.model_info = info
        for group in self.pv_groups:
            if len(group) > 1:
                notes.append(" en ".join(group) + " meten hetzelfde en tellen één keer mee")
        if skipped_sun:
            notes.append(f"{skipped_sun} uur overdag zonder zonmeting niet meegeteld voor het overige verbruik")
        if short:
            notes.append("nog te weinig historie voor: " + ", ".join(short))
        self._long_note = "; ".join(notes) if notes else (
            "Te weinig historie (minder dan twee dagen); er wordt met een vast verbruik gerekend.")

    def _rest_baseline_by_hour(self, busy):
        """Gewoonlijk overig verbruik (W) per uur van de dag, buiten de cycli om."""
        by_hour: dict[int, list] = {}
        for r in self.history:
            watt = r.get("werkelijk_rest_ruw_w")
            if watt is None:
                continue
            t = datetime.fromisoformat(r["t"])
            if any(c.start < t + timedelta(minutes=15) and t < c.end for c in busy):
                continue
            by_hour.setdefault(t.hour, []).append(watt)
        return {h: median(v) for h, v in by_hour.items() if len(v) >= 4}

    async def _learn_cycles(self, now) -> None:
        """Leert per apparaat (en voor tapwater) hoe vaak, wanneer en hoeveel."""
        sources = self._cycle_sources()
        if not sources:
            self.habits, self.cycles = {}, {}
            return
        start = now - timedelta(days=CYCLE_DAYS)
        # Niet op de recorder wachten: tijdens het opstarten wacht die op ons. Dat hij een paar
        # seconden achterloopt, vangt de actuele toestand hieronder op.
        found, window = {}, {}
        for key, src in sources.items():
            try:
                states = await self._state_history(src["toestand"], start)
            except Exception as err:
                _LOGGER.warning("Historie van %s niet te lezen: %s", src["toestand"], err)
                states = []
            local = [(dt_util.as_local(t), s) for t, s in states]
            live = self.hass.states.get(src["toestand"])
            if live is not None and live.state not in src["aan"]:
                # De recorder loopt soms een paar seconden achter op de echte toestand.
                local.append((dt_util.as_local(live.last_changed), live.state))
                local.sort(key=lambda ts: ts[0])
            found[key] = cy.cycles_from_states(local, src["aan"], dt_util.as_local(now))
            window[key] = next((t for t, s in local if s not in ("unavailable", "unknown")), None)
            if src.get("programma") and found[key]:
                try:
                    progs = [(dt_util.as_local(t), s) for t, s in
                             await self._state_history(src["programma"], found[key][0].start - timedelta(hours=1))]
                except Exception as err:
                    _LOGGER.warning("Historie van %s niet te lezen: %s", src["programma"], err)
                    progs = []
                for c in found[key]:
                    c.program = cy.program_at(c, progs)
        busy = [c for key, cs in found.items() if key != DHW for c in cs]
        baseline = None
        for key, src in sources.items():
            meters = src["meters"]
            if meters:
                stats = await self._statistics(meters, start, {"change"}, "5minute")
                rows: dict = {}
                for m in meters:
                    for t, v in self._hourly(stats.get(m), "change").items():
                        rows[t] = rows.get(t, 0.0) + v
                rows = sorted(rows.items())
                for c in found[key]:
                    c.kwh = cy.cycle_energy(c, rows)
            elif key != DHW:
                if baseline is None:
                    baseline = self._rest_baseline_by_hour(busy)
                quarters = [(datetime.fromisoformat(r["t"]), r["werkelijk_rest_ruw_w"]) for r in self.history
                            if r.get("werkelijk_rest_ruw_w") is not None]
                for c in found[key]:
                    if not c.ongoing:
                        c.kwh = cy.residual_energy(c, quarters, baseline)
        habits, info = {}, {}
        names = self.names()
        local_now = dt_util.as_local(now)
        for key, src in sources.items():
            cycles = self._with_log(key, found[key], window[key], local_now)
            since = self.cycle_log["since"].get(key)
            first = dt_util.as_local(datetime.fromisoformat(since)) if since else None
            first = max(first, dt_util.as_local(start)) if first else None
            days = 0.0 if first is None else (local_now - first).total_seconds() / 86400
            cycles = [c for c in cycles if first is None or c.start >= first]
            h = cy.learn(src["soort"], cycles, days, src["kwh"], src["min"],
                         span=(first, local_now) if first else None)
            habits[key] = h
            found[key] = cycles
            item = {
                "keer": h.cycles, "dagen_historie": round(h.days, 1),
                "keer_per_week": round(h.rate_per_day * 7, 1),
                "meestal_rond": [f"{u:02d}:00" for u in h.usual_hours()],
                "kwh_per_keer": round(h.kwh, 2), "duur_min": round(h.duration_h * 60),
                "verbruik_geleerd_van": h.kwh_learned,
                "verbruik_uit": "meter" if src["meters"] else "patroon van het huis",
                "in_logboek": len(self.cycle_log["cycles"].get(key, [])),
            }
            if h.week:
                item["keer_per_week_werkdagen"] = round(h.week["werkdag"][0] * 5, 1)
                item["keer_per_weekend"] = round(h.week["weekend"][0] * 2, 1)
                item["weekend_meestal_rond"] = [f"{u:02d}:00" for u in h.usual_hours(2, h.week["weekend"][1])]
            if h.programs:
                item["programmas"] = dict(sorted(h.programs.items(), key=lambda kv: -kv[1]))
            info[names.get(key, key)] = item
        self.habits, self.cycles = habits, found
        self.cycle_info = info
        await self._save()

    def _with_log(self, key, recent, window_start, local_now):
        """Vult de cycli uit de recorder aan met het logboek, en werkt het logboek bij."""
        parse = lambda s: dt_util.as_local(datetime.fromisoformat(s))
        logged = [cy.from_log(r, parse) for r in self.cycle_log["cycles"].get(key, [])]
        if window_start is not None:
            since = self.cycle_log["since"].get(key)
            if since is None or parse(since) > window_start:
                self.cycle_log["since"][key] = window_start.isoformat()
        merged = cy.merge_log(logged, recent, window_start or local_now) if logged else list(recent)
        keep_from = local_now - timedelta(days=cy.LOG_DAYS)
        self.cycle_log["cycles"][key] = [cy.to_log(c) for c in merged if not c.ongoing and c.start >= keep_from]
        return merged

    # --- voorspellen --------------------------------------------------------

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
        consumer = next((c for c in self.consumers if c[C_ID] == key), None)
        if not factors or not consumer:
            return 1.0
        wanted = _float(self.hass.states.get(consumer.get(C_SETPOINT) or ""))
        current = _float(self.hass.states.get(consumer.get(C_CURRENT) or ""))
        if wanted is None or current is None:
            return 1.0
        return factors[1] if wanted - current > THERMOSTAT_BAND else factors[0]

    def _announced(self, key, src, habit, now):
        """(aangekondigde cycli [(begin, duur in uren)], aantal starts vandaag, toelichting)."""
        state = self.hass.states.get(src["toestand"])
        local_now = dt_util.as_local(now)
        today = [c for c in self.cycles.get(key, []) if c.start.date() == local_now.date()]
        started = len(today)
        if state is None:
            return [], started, "niet bereikbaar"
        finish = None
        if src["einde"]:
            fstate = self.hass.states.get(src["einde"])
            if fstate is not None and fstate.state not in ("unknown", "unavailable", ""):
                try:
                    finish = dt_util.as_local(fc.parse_time(fstate.state))
                except ValueError:
                    finish = None
        if state.state in src["aan"]:
            ongoing = next((c for c in reversed(self.cycles.get(key, [])) if c.ongoing), None)
            begin = ongoing.start if ongoing else dt_util.as_local(state.last_changed)
            if ongoing is None:
                started += 1
            end = finish if finish and finish > local_now else max(
                begin + timedelta(hours=habit.duration_h), local_now + timedelta(minutes=SLOT_MIN))
            return [(begin, (end - begin).total_seconds() / 3600)], started, \
                f"loopt, klaar rond {end:%H:%M}"
        if state.state in src["uitgesteld"]:
            started += 1
            if finish:
                begin = max(local_now, finish - timedelta(hours=habit.duration_h))
                return [(begin, (finish - begin).total_seconds() / 3600)], started, \
                    f"uitgestelde start rond {begin:%H:%M}, klaar rond {finish:%H:%M}"
            return [], started, "uitgestelde start, eindtijd onbekend"
        return [], started, "uit"

    def _anomaly(self, key, part_name) -> float:
        """Gemiddelde afwijking (W) in het laatste half uur: werkelijk min vlak vooraf verwacht."""
        diffs = []
        recent = (dt_util.now() - timedelta(minutes=50)).isoformat()
        for r in self.history[-2:]:
            if r["t"] < recent:
                continue      # na een herstart zegt een oud kwartier niets over nu
            # Vergeleken met de verwachting zonder bijsturing, anders stuurt hij zichzelf bij.
            expected = (r.get("basis") or {}).get(key)
            if key == REST:
                actual = r.get("werkelijk_rest_ruw_w")
            else:
                actual = (r.get("werkelijk") or {}).get(part_name)
            if actual is not None and expected is not None:
                diffs.append(actual - expected)
        if not diffs:
            return 0.0
        return max(-MAX_ANOMALY_W, min(MAX_ANOMALY_W, sum(diffs) / len(diffs)))

    async def _load(self, starts, hours, now):
        if self._model_at is None or now - self._model_at > timedelta(hours=6):
            self._model_at = now
            started = time.perf_counter()
            try:
                await self._learn(now)
            except Exception as err:
                _LOGGER.warning("Verbruik leren mislukt: %s", err)
                self._long_note = f"Leren mislukt ({err}); er wordt met een vast verbruik gerekend."
            self.timing["leren_ms"] = round((time.perf_counter() - started) * 1000)
        if self._cycles_at is None or now - self._cycles_at > timedelta(hours=1):
            self._cycles_at = now
            try:
                await self._learn_cycles(now)
            except Exception as err:
                _LOGGER.warning("Gewoontes van de apparaten leren mislukt: %s", err)
        temps = await self._temperature_forecast(starts)
        names = self.names()
        n = len(starts)
        parts: dict[str, list] = {}
        for key in names:
            model = None if key == DHW else self.models.get(key)
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
            parts[key] = values
        if REST not in parts:
            parts[REST] = [DEFAULT_LOAD_W / 1000 * h for h in hours]

        # Cycli: wat loopt of gepland staat, plus de gewoonte. De basislijn zit al in het
        # model van het huis (apparaten) of van de warmtepomp (tapwater) en gaat daar af.
        self.cycle_state = {}
        for key, src in self._cycle_sources().items():
            habit = self.habits.get(key)
            if habit is None:
                continue
            known, started, note = self._announced(key, src, habit, now)
            values, baseline = cy.forecast(habit, starts, hours, dt_util.as_local(now), known, started)
            parent = HP if key == DHW else REST
            if parent in parts:
                parts[parent] = [max(0.0, a - b) for a, b in zip(parts[parent], baseline)]
            parts[key] = values
            self.cycle_state[names.get(key, key)] = note

        cycle_keys = [k for k in self._cycle_sources() if k != DHW and k in parts]
        self._basis = {REST: sum(parts[k][0] for k in cycle_keys + [REST]) / hours[0] * 1000}
        if HP in parts:
            self._basis[HP] = parts[HP][0] / hours[0] * 1000

        # Wat er nu anders gaat dan verwacht, werkt nog even door.
        self.anomaly = {}
        for key, half_life in ((REST, PERSIST_REST_H), (HP, PERSIST_HEATING_H)):
            if key not in parts:
                continue
            delta = self._anomaly(key, names[key])
            self.anomaly[names[key]] = round(delta)
            if abs(delta) < 1:
                continue
            values = parts[key]
            for i, (start, h) in enumerate(zip(starts, hours)):
                ahead = max(0.0, (dt_util.as_utc(start) - dt_util.as_utc(now)).total_seconds() / 3600)
                values[i] = max(0.0, values[i] + delta / 1000 * h * thermostat_weight(ahead, half_life))
        ordered = {names[k]: parts[k] for k in names if k in parts}
        load = [sum(column) for column in zip(*ordered.values())] if ordered else [0.0] * n
        return load, ordered, temps

    # --- vooruitblik, elk kwartier ----------------------------------------

    @callback
    def _on_quarter(self, now) -> None:
        self.hass.async_create_task(self.async_plan())

    async def async_plan(self) -> None:
        if self._planning:
            self._replan = True        # straks nog een keer, met de nieuwste toestand
            return
        self._planning = True
        self._replan = False
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
        if self._replan:
            self.hass.async_create_task(self.async_plan())

    def _meter_kwh(self, ids) -> dict[str, float]:
        """Stand van elke kWh-teller die nu een waarde heeft."""
        out = {}
        for entity_id in ids:
            state = self.hass.states.get(entity_id)
            value = _float(state)
            if value is None:
                continue
            unit = (state.attributes.get("unit_of_measurement") or "kWh").lower()
            out[entity_id] = value / 1000 if unit == "wh" else value * 1000 if unit == "mwh" else value
        return out

    def _all_meters(self) -> list[str]:
        ids = list(self.opt.get(CONF_PV_ENERGY) or []) + list(self.opt.get(CONF_HP_ENERGY) or [])
        ids += [i for c in self.consumers for i in c[C_ENERGY]]
        ids += [a[A_ENERGY] for a in self.appliances if a.get(A_ENERGY)]
        return ids

    def _delta(self, ids, after) -> float | None:
        steps = [after[i] - self._marks[i] for i in ids
                 if i in self._marks and i in after and after[i] >= self._marks[i]]
        return sum(steps) if steps else None

    def _actuals(self, sec):
        """Wat er sinds het begin van het kwartier werkelijk gebeurde (W), of None."""
        acc = self._acc
        if acc["sec"] <= 60:
            return None
        after = self._meter_kwh(self._all_meters())
        out = {"net_w": acc["met"] / acc["sec"] * 3600 * 1000,
               "net_zonder_accu_w": acc["zonder"] / acc["sec"] * 3600 * 1000}
        sun = pv_delta(self._marks, after, self.opt.get(CONF_PV_ENERGY) or [], self.pv_groups)
        if sun is None and not self.opt.get(CONF_PV_ENERGY):
            sun = 0.0
        if sun is None:
            return out
        to_w = 3600 * 1000 / sec
        out["zon_w"] = sun * to_w
        out["verbruik_w"] = max(0.0, out["net_zonder_accu_w"] + out["zon_w"])
        names, parts = self.names(), {}
        hp = self._delta(self.opt.get(CONF_HP_ENERGY) or [], after)
        if hp is not None:
            on = acc["aan"].get(DHW, 0.0) / acc["sec"] if self.has_dhw else 0.0
            dhw = hp * min(1.0, on)
            parts[names[HP]] = (hp - dhw) * to_w
            if self.has_dhw:
                parts[names[DHW]] = dhw * to_w
        for c in self.consumers:
            v = self._delta(c[C_ENERGY], after)
            if v is not None:
                parts[c[C_NAME]] = v * to_w
        raw_rest = out["verbruik_w"] - sum(parts.values())
        for a in self.appliances:
            if a.get(A_ENERGY):
                v = self._delta([a[A_ENERGY]], after)
                if v is not None:
                    parts[a[A_NAME]] = v * to_w
            else:
                habit = self.habits.get(a[A_ID])
                on = acc["aan"].get(a[A_ID], 0.0) / acc["sec"]
                if habit and habit.duration_h > 0:
                    # Geen eigen meter: geschat met het gemiddelde vermogen van een programma.
                    parts[a[A_NAME]] = min(max(0.0, raw_rest), on * habit.kwh / habit.duration_h * 1000)
        out["rest_ruw_w"] = raw_rest
        parts[names[REST]] = max(0.0, out["verbruik_w"] - sum(parts.values()))
        out["onderdelen"] = parts
        return out

    def _close_record(self) -> None:
        """Legt vast wat er in het afgelopen kwartier werkelijk gebeurde."""
        rec = self._record
        if rec is None:
            return
        actual = self._actuals(self._acc["sec"])
        if actual:
            rec["net_w"] = round(actual["net_w"])
            rec["net_zonder_accu_w"] = round(actual["net_zonder_accu_w"])
            if "zon_w" in actual:
                rec["werkelijk_zon_w"] = round(actual["zon_w"])
                rec["werkelijk_verbruik_w"] = round(actual["verbruik_w"])
                rec["werkelijk_rest_ruw_w"] = round(actual["rest_ruw_w"])
                rec["werkelijk"] = {k: round(v) for k, v in actual["onderdelen"].items()}
        self.history.append(rec)
        self.history = self.history[-HISTORY_DAYS * 96:]
        self._record = None
        self._acc = self._empty_acc()

    def current(self) -> dict:
        """Het lopende kwartier tot nu toe: gemiddeld verbruik, zon en net (W)."""
        if self._record is None:
            return {}
        started = datetime.fromisoformat(self._record["t"])
        sec = (dt_util.now() - started).total_seconds()
        actual = self._actuals(sec) if sec > 60 else None
        if not actual:
            return {}
        return {
            "sinds": self._record["t"],
            "verbruik_w": None if "verbruik_w" not in actual else round(actual["verbruik_w"]),
            "zon_w": None if "zon_w" not in actual else round(actual["zon_w"]),
            "net_w": round(actual["net_w"]),
            "accu_w": round(self.battery_w),
            "onderdelen": {k: round(v) for k, v in (actual.get("onderdelen") or {}).items()},
        }

    async def _plan(self) -> None:
        self.opt = {**self.entry.data, **self.entry.options}
        now = dt_util.now()
        count = int(self.opt.get(CONF_HORIZON, 36) * 60 / SLOT_MIN)
        utc_starts, hours = slot_starts(dt_util.utcnow(), count, SLOT_MIN)
        starts = [dt_util.as_local(t) for t in utc_starts]
        if self._record is not None and self._record["t"] != starts[0].isoformat():
            self._close_record()

        pv = await self._pv(starts, hours)
        load, parts, temps = await self._load(starts, hours, now)
        slots = [Slot(h, ld, p) for h, ld, p in zip(hours, load, pv)]

        units = self.units()
        bat, soc = aggregate(units) if units else (Battery(0, 0, 0, 0, 1, 1), 0.0)
        look = await self.hass.async_add_executor_job(outlook, slots, bat, soc)
        steps = look.steps

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
            "apparaten": dict(self.cycle_state),
            "bijgewerkt": now.isoformat(timespec="seconds"),
        }

        def watts(values):
            return [round(v / h * 1000) for v, h in zip(values, hours)]

        cap = sum(u.capacity_kwh for u in units) or 1.0
        soc_pct = [round(s.soc_kwh / cap * 100) for s in steps] if units else []
        day = min(96, len(slots))
        self.plan = {
            "start": starts[0].isoformat(), "stap_min": SLOT_MIN,
            "zon_w": watts(pv), "verbruik_w": watts(load),
            "onderdelen_w": {name: watts(values) for name, values in parts.items()},
            "accu_w": watts([s.accu_ac_kwh for s in steps]),
            "net_w": watts([s.grid_kwh for s in steps]),
            "soc_pct": soc_pct,
            "buitentemp_24u": [None if t is None else round(t, 1) for t in temps],
            "verbruik_24u_kwh": round(sum(load[:day]), 1),
            "zon_24u_kwh": round(sum(pv[:day]), 1),
            "onderdelen_24u_kwh": {name: round(sum(values[:day]), 1) for name, values in parts.items()},
        }
        # Een etmaal vooraf: zo zag dit kwartier er morgen om deze tijd uit.
        if len(starts) > DAY_AHEAD_SLOTS:
            i = DAY_AHEAD_SLOTS
            self.day_ahead[starts[i].isoformat()] = {
                "verbruik_w": self.plan["verbruik_w"][i], "zon_w": self.plan["zon_w"][i],
                "soc_pct": soc_pct[i] if soc_pct else None,
                "onderdelen": {name: values[i] for name, values in self.plan["onderdelen_w"].items()}}
        if self._record is None:
            # De verwachting zoals die bij het begin van het kwartier was.
            t0 = starts[0].isoformat()
            ahead = self.day_ahead.pop(t0, None) or {}
            self._record = {
                "t": t0,
                "soc_pct": round(soc / cap * 100) if units else None,
                "verwacht_verbruik_w": self.plan["verbruik_w"][0],
                "verwacht_zon_w": self.plan["zon_w"][0],
                "verwacht_net_w": self.plan["net_w"][0],
                "verwacht_accu_w": self.plan["accu_w"][0],
                "basis": {k: round(v) for k, v in self._basis.items()},
                "verwacht": {name: values[0] for name, values in self.plan["onderdelen_w"].items()},
                "etmaal_verbruik_w": ahead.get("verbruik_w"),
                "etmaal_zon_w": ahead.get("zon_w"),
                "etmaal_soc_pct": ahead.get("soc_pct"),
                "etmaal": ahead.get("onderdelen"),
            }
            self._marks = self._meter_kwh(self._all_meters())
            self._acc = self._empty_acc()
        self.model_note = self._long_note

    def forecast_quality(self, days: int = 7) -> dict:
        """Hoe goed de verwachting was: vlak vooraf en een etmaal vooraf, totaal en per onderdeel."""
        cutoff = (dt_util.now() - timedelta(days=days)).isoformat()
        rows = [r for r in self.history if r["t"] >= cutoff and r.get("werkelijk_verbruik_w") is not None]
        out = {"dagen": days, "kwartieren": len(rows)}

        def mae(pairs):
            pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
            return round(sum(abs(a - b) for a, b in pairs) / len(pairs)) if pairs else None

        if rows:
            out["verbruik_afwijking_w"] = mae((r["verwacht_verbruik_w"], r["werkelijk_verbruik_w"]) for r in rows)
            out["verbruik_etmaal_vooraf_afwijking_w"] = mae(
                (r.get("etmaal_verbruik_w"), r["werkelijk_verbruik_w"]) for r in rows)
            actual = sum(r["werkelijk_verbruik_w"] for r in rows)
            if actual:
                out["verbruik_verschil_pct"] = round(
                    (sum(r["verwacht_verbruik_w"] for r in rows) - actual) / actual * 100, 1)
            per = {}
            for name in self.names().values():
                per[name] = {
                    "vlak_vooraf_w": mae(((r.get("verwacht") or {}).get(name), (r.get("werkelijk") or {}).get(name))
                                         for r in rows),
                    "etmaal_vooraf_w": mae(((r.get("etmaal") or {}).get(name), (r.get("werkelijk") or {}).get(name))
                                           for r in rows)}
            out["onderdelen"] = per
            expected = sum(r["verwacht_zon_w"] for r in rows)
            if expected > 0:
                out["zon_werkelijk_gedeeld_door_verwacht"] = round(
                    sum(r["werkelijk_zon_w"] for r in rows) / expected, 2)
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
            p, soc = follow_meter(u, grid, dt_s)
            grid += p
            virtual_w += p
            self.virtual_soc[u.id] = soc
            self.actual_power[u.id] = p
        self.grid_with = grid
        self.battery_w = real + virtual_w

        for key, watt in (("zonder", self.grid_without), ("met", self.grid_with)):
            self._acc[key] += watt / 1000 * dt_s / 3600
        self._acc["sec"] += dt_s
        for key, src in self._cycle_sources().items():
            state = self.hass.states.get(src["toestand"])
            if state is not None and state.state in src["aan"]:
                self._acc["aan"][key] = self._acc["aan"].get(key, 0.0) + dt_s

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
            _LOGGER.warning("P1-meter geeft geen actuele waarden")
        self.meter_stale = True
        self.grid_with = self.grid_without = None
        self._notify()

    # --- overzichten --------------------------------------------------------

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
        for name, note in self.cycle_state.items():
            if note not in ("uit",):
                notes.append(f"{name}: {note}.")
        for battery in self.batteries:
            if battery[B_ID] in self.soc_issue:
                notes.append(f"De laadtoestand van {battery[B_NAME]} {self.soc_issue[battery[B_ID]]}.")
        if self.meter_stale:
            notes.append("De P1-meter geeft geen actuele waarden.")
        return " ".join(notes)
