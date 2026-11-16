"""Instellen van Huis EMS via de gebruikersinterface."""
from __future__ import annotations

import uuid

try:  # nieuwere Home Assistant-versies gebruiken probatio in plaats van voluptuous
    import probatio as vol
except ImportError:  # pragma: no cover
    import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector as sel

from .const import *  # noqa: F401,F403


def _entity(domain, device_class=None, multiple=False):
    cfg = {"domain": domain, "multiple": multiple}
    if device_class:
        cfg["device_class"] = device_class
    return sel.EntitySelector(sel.EntitySelectorConfig(**cfg))


def _number(low, high, step, unit=None):
    cfg = {"min": low, "max": high, "step": step, "mode": sel.NumberSelectorMode.BOX}
    if unit:
        cfg["unit_of_measurement"] = unit
    return sel.NumberSelector(sel.NumberSelectorConfig(**cfg))


SOURCES = vol.Schema({
    vol.Required(CONF_P1_POWER): _entity("sensor", "power"),
    vol.Required(CONF_IMPORT_ENERGY): _entity("sensor", "energy", True),
    vol.Optional(CONF_EXPORT_ENERGY): _entity("sensor", "energy", True),
    vol.Optional(CONF_PV_ENERGY): _entity("sensor", "energy", True),
    vol.Optional(CONF_USE_ENERGY_FORECAST, default=True): sel.BooleanSelector(),
    vol.Optional(CONF_PV_FORECAST_SENSORS): _entity("sensor", None, True),
    vol.Optional(CONF_PV_FORECAST_UNIT, default="kw"): sel.SelectSelector(sel.SelectSelectorConfig(
        options=["kw", "w", "wh_period", "kwh_period"], mode=sel.SelectSelectorMode.DROPDOWN)),
    vol.Optional(CONF_HORIZON, default=36): _number(12, 48, 1, "h"),
})

GRID = vol.Schema({
    vol.Optional(CONF_P1_HOST): sel.TextSelector(),
})

TARIFFS = vol.Schema({
    vol.Required(CONF_TARIFF_MODE, default=TARIFF_FIXED): sel.SelectSelector(sel.SelectSelectorConfig(
        options=[TARIFF_FIXED, TARIFF_SPOT, TARIFF_ALL_IN], translation_key="tariff_mode",
        mode=sel.SelectSelectorMode.LIST)),
    vol.Optional(CONF_NET_METERING, default=False): sel.BooleanSelector(),
    vol.Optional(CONF_FIXED_BUY, default=0.27): _number(0, 2, 0.001, "EUR/kWh"),
    vol.Optional(CONF_FIXED_SELL, default=0.05): _number(-1, 2, 0.001, "EUR/kWh"),
    vol.Optional(CONF_PRICE_SENSOR): _entity("sensor"),
    vol.Optional(CONF_PRICE_SCALE, default=1.0): _number(0.0001, 1000, "any"),
    vol.Optional(CONF_ENERGY_TAX, default=0.0916): _number(0, 1, "any", "EUR/kWh"),
    vol.Optional(CONF_BUY_FEE, default=0.02): _number(0, 1, "any", "EUR/kWh"),
    vol.Optional(CONF_SELL_FEE, default=0.0): _number(0, 1, "any", "EUR/kWh"),
    vol.Optional(CONF_VAT, default=21): _number(0, 30, 1, "%"),
})

HEATPUMP = vol.Schema({
    vol.Optional(CONF_HP_ENERGY): _entity("sensor", "energy", True),
    vol.Optional(CONF_OUTDOOR_TEMP): _entity("sensor"),
    vol.Optional(CONF_WEATHER): _entity("weather"),
})


def _battery_schema(editing):
    fields = {
        vol.Required(B_NAME): sel.TextSelector(),
        vol.Required(B_DRIVER, default=DRIVER_VIRTUAL): sel.SelectSelector(sel.SelectSelectorConfig(
            options=[DRIVER_VIRTUAL, DRIVER_ZENDURE, DRIVER_ENTITIES], translation_key="koppeling",
            mode=sel.SelectSelectorMode.LIST)),
        vol.Optional(B_HOST): sel.TextSelector(),
        vol.Required(B_CAPACITY, default=8.0): _number(0.5, 100, 0.1, "kWh"),
        vol.Required(B_MAX_CHARGE, default=3000): _number(100, 25000, 50, "W"),
        vol.Required(B_MAX_DISCHARGE, default=3000): _number(100, 25000, 50, "W"),
        vol.Required(B_MIN_SOC, default=10): _number(0, 50, 1, "%"),
        vol.Required(B_MAX_SOC, default=100): _number(50, 100, 1, "%"),
        vol.Required(B_EFFICIENCY, default=86): _number(50, 100, 1, "%"),
        vol.Optional(B_SOC_SENSOR): _entity("sensor"),
        vol.Optional(B_POWER_SENSOR): _entity("sensor", "power"),
        vol.Optional(B_POWER_INVERT, default=False): sel.BooleanSelector(),
    }
    if editing:
        fields[vol.Optional("delete", default=False)] = sel.BooleanSelector()
    return vol.Schema(fields)


def _consumer_schema(editing):
    fields = {
        vol.Required(C_NAME): sel.TextSelector(),
        vol.Required(C_ENERGY): _entity("sensor", "energy", True),
        vol.Optional(C_SETPOINT): sel.EntitySelector(sel.EntitySelectorConfig(
            domain=["sensor", "number", "input_number"])),
        vol.Optional(C_CURRENT): _entity("sensor"),
    }
    if editing:
        fields[vol.Optional("delete", default=False)] = sel.BooleanSelector()
    return vol.Schema(fields)


def _common(fields, d):
    """Velden die elke buffer heeft; de standaardwaarden verschillen per soort."""
    fields.update({
        vol.Required(L_MIN_SOC, default=d["soc"]): _number(0, 100, 1, "%"),
        vol.Required(L_START, default=d["start"]): _number(0, 24, 0.25, "h"),
        vol.Required(L_END, default=d["end"]): _number(0, 24, 0.25, "h"),
        vol.Required(L_FORECAST, default=d["forecast"]): sel.BooleanSelector(),
        vol.Required(L_ORDER, default=d["order"]): _number(1, 20, 1),
        vol.Required(L_ON_DELAY, default=d["on"]): _number(0, 120, 1, "min"),
        vol.Required(L_OFF_DELAY, default=d["off"]): _number(0, 120, 1, "min"),
        vol.Required(L_MIN_ON, default=d["min_on"]): _number(0, 480, 1, "min"),
        vol.Required(L_MIN_OFF, default=d["min_off"]): _number(0, 480, 1, "min"),
        vol.Required(L_REST, default=d["rest"]): _number(0, 240, 1, "min"),
    })
    return fields


def _thermostat_schema(editing):
    fields = {
        vol.Required(L_NAME, default="Vloerverwarming"): sel.TextSelector(),
        vol.Optional(L_ENTITY): _entity("climate"),
        vol.Required(L_BOOST, default=1.0): _number(0.5, 5, 0.5, "°C"),
        vol.Optional(L_TEMP): _entity("sensor"),
        vol.Required(L_MAX_TEMP, default=22.0): _number(15, 28, 0.5, "°C"),
        vol.Required(L_POWER, default=1500): _number(100, 15000, 50, "W"),
        vol.Optional(L_POWER_SENSOR): _entity("sensor", "power"),
    }
    _common(fields, {"soc": 90, "start": 10, "end": 16, "forecast": True, "order": 1, "on": 10,
                     "off": 10, "min_on": 60, "min_off": 30, "rest": 30})
    if editing:
        fields[vol.Optional("delete", default=False)] = sel.BooleanSelector()
    return vol.Schema(fields)


def _switch_schema(editing):
    fields = {
        vol.Required(L_NAME, default="Boiler"): sel.TextSelector(),
        vol.Optional(L_ENTITY): sel.EntitySelector(sel.EntitySelectorConfig(
            domain=["switch", "input_boolean", "light"])),
        vol.Required(L_POWER, default=2000): _number(50, 25000, 50, "W"),
        vol.Optional(L_POWER_SENSOR): _entity("sensor", "power"),
        vol.Optional(L_ENERGY): _entity("sensor", "energy"),
        vol.Optional(L_TEMP): _entity("sensor"),
        vol.Optional(L_MAX_TEMP, default=60.0): _number(20, 95, 0.5, "°C"),
    }
    _common(fields, {"soc": 95, "start": 0, "end": 24, "forecast": False, "order": 2, "on": 5,
                     "off": 3, "min_on": 15, "min_off": 10, "rest": 15})
    if editing:
        fields[vol.Optional("delete", default=False)] = sel.BooleanSelector()
    return vol.Schema(fields)


def _keys(schema):
    return [str(getattr(k, "schema", k)) for k in schema.schema]


def _merge(current, schema, user_input):
    """Nieuwe waarden overnemen; leeggemaakte optionele velden echt verwijderen."""
    out = {k: v for k, v in current.items() if k not in _keys(schema)}
    out.update({k: v for k, v in user_input.items() if v not in (None, "", [])})
    return out


async def _energy_defaults(hass):
    """Stelt de meters voor die al in het Energie-dashboard staan."""
    found = {CONF_IMPORT_ENERGY: [], CONF_EXPORT_ENERGY: [], CONF_PV_ENERGY: []}
    try:
        from homeassistant.components.energy.data import async_get_manager
        manager = await async_get_manager(hass)

        def walk(node, kind):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "stat_energy_from" and isinstance(value, str):
                        found[CONF_PV_ENERGY if kind == "solar" else CONF_IMPORT_ENERGY].append(value)
                    elif key == "stat_energy_to" and isinstance(value, str):
                        found[CONF_EXPORT_ENERGY].append(value)
                    else:
                        walk(value, kind)
            elif isinstance(node, list):
                for item in node:
                    walk(item, kind)

        for source in (manager.data or {}).get("energy_sources", []):
            if source.get("type") in ("grid", "solar"):
                walk(source, source["type"])
    except Exception:  # het Energie-dashboard is geen voorwaarde
        pass
    return {k: [e for e in v if hass.states.get(e)] for k, v in found.items() if v}


class HuisEmsFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                title="Huis EMS", data={},
                options={**_merge({}, SOURCES, user_input), CONF_BATTERIES: []})
        defaults = await _energy_defaults(self.hass)
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(SOURCES, defaults))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HuisEmsOptions()


class HuisEmsOptions(OptionsFlow):

    def __init__(self) -> None:
        self._edit_id = None

    @property
    def _options(self):
        return dict(self.config_entry.options)

    async def async_step_init(self, user_input=None):
        menu = ["bronnen", "net", "tarieven", "warmtepomp", "accu_toevoegen"]
        if self._options.get(CONF_BATTERIES):
            menu.append("accu_kiezen")
        menu.append("verbruiker_toevoegen")
        if self._options.get(CONF_CONSUMERS):
            menu.append("verbruiker_kiezen")
        menu += ["thermostaat_toevoegen", "schakelaar_toevoegen"]
        if self._options.get(CONF_STAGE2):
            menu.append("buffer_kiezen")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def _simple(self, step, schema, user_input):
        if user_input is not None:
            return self.async_create_entry(data=_merge(self._options, schema, user_input))
        return self.async_show_form(
            step_id=step, data_schema=self.add_suggested_values_to_schema(schema, self._options))

    async def async_step_bronnen(self, user_input=None):
        return await self._simple("bronnen", SOURCES, user_input)

    async def async_step_net(self, user_input=None):
        return await self._simple("net", GRID, user_input)

    async def async_step_tarieven(self, user_input=None):
        return await self._simple("tarieven", TARIFFS, user_input)

    async def async_step_warmtepomp(self, user_input=None):
        return await self._simple("warmtepomp", HEATPUMP, user_input)

    # Verbruikers met een eigen model en verbruikers van de tweede trap: lijsten
    # die op dezelfde manier worden bewerkt.

    async def _pick(self, step, key, next_step, user_input):
        items = self._options.get(key, [])
        if user_input is not None:
            self._edit_id = user_input["item"]
            return await next_step()
        schema = vol.Schema({vol.Required("item"): sel.SelectSelector(sel.SelectSelectorConfig(
            options=[sel.SelectOptionDict(value=i["id"], label=i["name"]) for i in items]))})
        return self.async_show_form(step_id=step, data_schema=schema)

    async def _item(self, step, key, make_schema, user_input, extra=None):
        options = self._options
        items = list(options.get(key, []))
        current = next((i for i in items if i["id"] == self._edit_id), None)
        schema = make_schema(current is not None)
        if user_input is not None:
            delete = user_input.pop("delete", False)
            items = [i for i in items if i["id"] != self._edit_id]
            if not delete:
                new = _merge({}, schema, user_input)
                new["id"] = self._edit_id or uuid.uuid4().hex[:8]
                new.update(extra or {})
                items.append(new)
            return self.async_create_entry(data={**options, key: items})
        return self.async_show_form(
            step_id=step, data_schema=self.add_suggested_values_to_schema(schema, current or {}))

    async def async_step_verbruiker_toevoegen(self, user_input=None):
        self._edit_id = None
        return await self.async_step_verbruiker(user_input)

    async def async_step_verbruiker_kiezen(self, user_input=None):
        return await self._pick("verbruiker_kiezen", CONF_CONSUMERS, self.async_step_verbruiker,
                                user_input)

    async def async_step_verbruiker(self, user_input=None):
        return await self._item("verbruiker", CONF_CONSUMERS, _consumer_schema, user_input)

    # Verschuifbaar verbruik: twee soorten warmtebuffers met elk hun eigen velden.

    async def async_step_thermostaat_toevoegen(self, user_input=None):
        self._edit_id = None
        return await self.async_step_thermostaat(user_input)

    async def async_step_schakelaar_toevoegen(self, user_input=None):
        self._edit_id = None
        return await self.async_step_schakelaar(user_input)

    async def async_step_buffer_kiezen(self, user_input=None):
        async def route():
            item = next(i for i in self._options.get(CONF_STAGE2, []) if i["id"] == self._edit_id)
            if item.get(L_KIND, "schakelaar") == "thermostaat":
                return await self.async_step_thermostaat()
            return await self.async_step_schakelaar()
        return await self._pick("buffer_kiezen", CONF_STAGE2, route, user_input)

    async def async_step_thermostaat(self, user_input=None):
        return await self._item("thermostaat", CONF_STAGE2, _thermostat_schema, user_input,
                                {L_KIND: "thermostaat"})

    async def async_step_schakelaar(self, user_input=None):
        return await self._item("schakelaar", CONF_STAGE2, _switch_schema, user_input,
                                {L_KIND: "schakelaar"})

    async def async_step_accu_toevoegen(self, user_input=None):
        self._edit_id = None
        return await self.async_step_accu(user_input)

    async def async_step_accu_kiezen(self, user_input=None):
        batteries = self._options.get(CONF_BATTERIES, [])
        if user_input is not None:
            self._edit_id = user_input["accu"]
            return await self.async_step_accu()
        schema = vol.Schema({vol.Required("accu"): sel.SelectSelector(sel.SelectSelectorConfig(
            options=[sel.SelectOptionDict(value=b[B_ID], label=b[B_NAME]) for b in batteries]))})
        return self.async_show_form(step_id="accu_kiezen", data_schema=schema)

    async def async_step_accu(self, user_input=None):
        options = self._options
        batteries = list(options.get(CONF_BATTERIES, []))
        current = next((b for b in batteries if b[B_ID] == self._edit_id), None)
        schema = _battery_schema(current is not None)
        errors = {}
        if user_input is not None:
            delete = user_input.pop("delete", False)
            driver = user_input.get(B_DRIVER, DRIVER_VIRTUAL)
            real = driver == DRIVER_ENTITIES
            if not delete and driver == DRIVER_ZENDURE:
                try:
                    from .drivers import zendure_report
                    report = await zendure_report(self.hass, user_input.get(B_HOST) or "")
                    if not report.get("sn"):
                        raise ValueError("geen serienummer")
                except Exception:
                    errors[B_HOST] = "cannot_connect"
            if errors:
                pass
            elif not delete and real and not user_input.get(B_SOC_SENSOR):
                errors[B_SOC_SENSOR] = "soc_required"
            else:
                batteries = [b for b in batteries if b[B_ID] != self._edit_id]
                if not delete:
                    new = _merge({}, schema, user_input)
                    new[B_ID] = self._edit_id or uuid.uuid4().hex[:8]
                    new[B_VIRTUAL] = driver == DRIVER_VIRTUAL
                    batteries.append(new)
                return self.async_create_entry(data={**options, CONF_BATTERIES: batteries})
        return self.async_show_form(
            step_id="accu", errors=errors,
            data_schema=self.add_suggested_values_to_schema(schema, user_input or current or {}))
