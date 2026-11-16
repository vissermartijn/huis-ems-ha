"""Sensoren: vooruitblik, plan, terugblik, voorspelkwaliteit, verschuiven en kosten."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass

from .const import B_DRIVER, B_ID, B_NAME, B_VIRTUAL, DOMAIN, DRIVER_ZENDURE, HISTORY_SLOTS
from .entity import EmsEntity

PLAN_LISTS = ("prijs_herkomst", "accu_w", "net_w", "soc_pct", "koop", "verkoop", "zon_w",
              "verbruik_w", "warmtepomp_w", "buitentemp_24u")


class EmsSensor(EmsEntity, SensorEntity):

    def __init__(self, coordinator, key, name, value, attrs=None, unit=None,
                 device_class=None, state_class=None, icon=None):
        super().__init__(coordinator, key, name)
        self._value, self._attrs = value, attrs
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_icon = icon

    @property
    def native_value(self):
        return self._value(self.coordinator)

    @property
    def extra_state_attributes(self):
        return self._attrs(self.coordinator) if self._attrs else None


class PlanSensor(EmsSensor):
    # Grote lijsten horen niet in de database; ze worden elk kwartier opnieuw berekend.
    _unrecorded_attributes = frozenset(PLAN_LISTS)


class HistorySensor(EmsSensor):
    _unrecorded_attributes = frozenset({"kwartieren"})


def _round(value, digits=0):
    return None if value is None else round(value, digits)


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    power = dict(unit="W", device_class=SensorDeviceClass.POWER,
                 state_class=SensorStateClass.MEASUREMENT)
    money = dict(unit="EUR", device_class=SensorDeviceClass.MONETARY,
                 state_class=SensorStateClass.TOTAL)
    price = dict(unit="EUR/kWh", icon="mdi:currency-eur")
    energy = dict(unit="kWh", icon="mdi:crystal-ball")

    def flex_state(co):
        items = co.flex_overview()
        if not items:
            return "geen buffers"
        return f"{sum(1 for i in items if i['aan'])} van {len(items)} aan"

    entities = [
        EmsSensor(c, "vooruitblik", "Vooruitblik", lambda co: co.look.get("tekst", "")[:255],
                  lambda co: {**co.look, "status": co.status_note(), "accus": co.battery_overview(),
                              "meter_leeftijd_s": _round(co.meter.age_s()),
                              "meterbron": co.meter.source},
                  icon="mdi:crystal-ball"),
        PlanSensor(c, "plan", "Plan", lambda co: co.look.get("kosten_24u_eur"),
                   lambda co: co.plan, unit="EUR", icon="mdi:chart-timeline-variant"),
        HistorySensor(c, "terugblik", "Terugblik", lambda co: len(co.history[-HISTORY_SLOTS:]),
                      lambda co: {"kwartieren": co.history[-HISTORY_SLOTS:]}, icon="mdi:history"),
        EmsSensor(c, "voorspelkwaliteit", "Voorspelkwaliteit",
                  lambda co: co.forecast_quality().get("verbruik_gemiddelde_afwijking_w"),
                  lambda co: co.forecast_quality(), unit="W", icon="mdi:bullseye-arrow"),
        EmsSensor(c, "verbruiksmodel", "Verbruiksmodel",
                  lambda co: "geleerd" if co.models["rest"] else "standaard",
                  lambda co: {
                      "toelichting": co.model_note,
                      "warmtepomp_kwh_per_dag_per_graad":
                          _round(co.models["hp"].k * 24, 2) if co.models["hp"] else None,
                      "stookgrens": co.models["hp"].t_limit if co.models["hp"] else None,
                      "basis_per_uur_kwh":
                          [round(v, 2) for v in co.models["rest"].base] if co.models["rest"] else None,
                      "warmtepomp_basis_per_uur_kwh":
                          [round(v, 2) for v in co.models["hp"].base] if co.models["hp"] else None,
                      "onderdelen": co.model_info,
                      "rekentijd_plan_ms": co.timing["plan_ms"],
                      "rekentijd_leren_ms": co.timing["leren_ms"],
                      "rekentijd_per_meting_ms": _round(co.timing["meting_ms"], 2),
                  }, icon="mdi:brain"),
        EmsSensor(c, "verschuiven", "Verschuifbaar verbruik", flex_state,
                  lambda co: {"overschot_w": _round(co.spare_w),
                              "verschoven_w": _round(co.flex_power),
                              "schakelt": co.stage2_enabled(),
                              "buffers": co.flex_overview()},
                  icon="mdi:water-boiler"),
        EmsSensor(c, "accu_lading", "Accu lading", lambda co: _round(co.soc_summary()[0], 1),
                  lambda co: {"probleem": co.soc_summary()[1], "accus": co.battery_overview()},
                  unit="%", device_class=SensorDeviceClass.BATTERY,
                  state_class=SensorStateClass.MEASUREMENT),
        EmsSensor(c, "accu_vermogen", "Accu vermogen", lambda co: _round(co.battery_w), **power),
        EmsSensor(c, "verbruik_24u", "Verwacht verbruik komende 24 uur",
                  lambda co: co.plan.get("verbruik_24u_kwh"), **energy),
        EmsSensor(c, "zon_24u", "Verwachte zon komende 24 uur",
                  lambda co: co.plan.get("zon_24u_kwh"), **energy),
        EmsSensor(c, "warmtepomp_24u", "Verwacht warmtepompverbruik komende 24 uur",
                  lambda co: co.plan.get("warmtepomp_24u_kwh"), **energy),
        EmsSensor(c, "net_24u", "Verwacht van het net komende 24 uur",
                  lambda co: co.look.get("net_24u_kwh"), **energy),
        EmsSensor(c, "teruglevering_24u", "Verwachte teruglevering komende 24 uur",
                  lambda co: co.look.get("teruglevering_24u_kwh"), **energy),
        EmsSensor(c, "koopprijs", "Inkoopprijs nu", lambda co: _round(co.prices_now[0], 4), **price),
        EmsSensor(c, "terugleverprijs", "Terugleverprijs nu",
                  lambda co: _round(co.prices_now[1], 4), **price),
        EmsSensor(c, "net_zonder_accu", "Net zonder accu", lambda co: _round(co.grid_without), **power),
        EmsSensor(c, "net_met_accu", "Net met accu", lambda co: _round(co.grid_with), **power),
        EmsSensor(c, "kosten_zonder_accu", "Kosten zonder accu",
                  lambda co: round(co.costs["zonder"], 2), **money),
        EmsSensor(c, "kosten_met_accu", "Kosten met accu",
                  lambda co: round(co.costs["met"], 2), **money),
        EmsSensor(c, "besparing_totaal", "Besparing totaal",
                  lambda co: round(co.costs["zonder"] - co.costs["met"], 2), **money),
        EmsSensor(c, "besparing_vandaag", "Besparing vandaag",
                  lambda co: round(co.costs["dag_zonder"] - co.costs["dag_met"], 2), **money),
    ]
    for battery in c.batteries:
        bid, name = battery[B_ID], battery[B_NAME]
        if battery.get(B_VIRTUAL, True):
            cap = battery["capacity_kwh"]
            entities.append(EmsSensor(
                c, f"soc_{bid}", f"{name} lading",
                lambda co, bid=bid, cap=cap: _round(co.virtual_soc.get(bid, 0) / cap * 100, 1),
                unit="%", device_class=SensorDeviceClass.BATTERY,
                state_class=SensorStateClass.MEASUREMENT))
            entities.append(EmsSensor(
                c, f"vermogen_{bid}", f"{name} vermogen",
                lambda co, bid=bid: _round(co.actual_power.get(bid, 0)), **power))
        elif battery.get(B_DRIVER) == DRIVER_ZENDURE:
            entities.append(EmsSensor(
                c, f"soc_{bid}", f"{name} lading",
                lambda co, bid=bid: co.drivers[bid].soc_pct if bid in co.drivers else None,
                lambda co, bid=bid: {"verbonden": co.drivers[bid].connected,
                                     "serienummer": co.drivers[bid].sn} if bid in co.drivers else None,
                unit="%", device_class=SensorDeviceClass.BATTERY,
                state_class=SensorStateClass.MEASUREMENT))
            entities.append(EmsSensor(
                c, f"vermogen_{bid}", f"{name} vermogen",
                lambda co, bid=bid: _round(co.drivers[bid].power_w) if bid in co.drivers else None,
                **power))
    async_add_entities(entities)
