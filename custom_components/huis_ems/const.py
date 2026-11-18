"""Constanten voor Huis EMS."""
DOMAIN = "huis_ems"
SLOT_MIN = 15
WATCHDOG_SECONDS = 5
NOTIFY_SECONDS = 5
HISTORY_SLOTS = 192          # 48 uur terugkijken in de attributen
HISTORY_DAYS = 14            # zo lang bewaart het EMS zijn eigen kwartieren
STORE_VERSION = 1

# Opties: bronnen
CONF_P1_POWER = "p1_power"
CONF_IMPORT_ENERGY = "import_energy"
CONF_EXPORT_ENERGY = "export_energy"
CONF_PV_ENERGY = "pv_energy"
CONF_USE_ENERGY_FORECAST = "use_energy_forecast"
CONF_PV_FORECAST_SENSORS = "pv_forecast_sensors"
CONF_PV_FORECAST_UNIT = "pv_forecast_unit"
CONF_HORIZON = "horizon_hours"

# Opties: snelle meting
CONF_P1_HOST = "p1_host"
STALE_SECONDS = 20           # zo lang geen meting: de meter geldt als verouderd
SOC_STALE_SECONDS = 900      # een laadtoestand die zo lang niet gemeld is telt niet meer

# Opties: warmtepomp en weer
CONF_HP_ENERGY = "hp_energy"
CONF_DHW_ACTIVE = "dhw_active"   # binary_sensor: warmtepomp maakt tapwater
CONF_OUTDOOR_TEMP = "outdoor_temp"
CONF_WEATHER = "weather"

# Accu's: het EMS leest ze alleen uit; ze regelen zichzelf op zelfverbruik.
CONF_BATTERIES = "batteries"
B_ID, B_NAME, B_VIRTUAL = "id", "name", "virtual"
B_CAPACITY, B_MAX_CHARGE, B_MAX_DISCHARGE = "capacity_kwh", "max_charge_w", "max_discharge_w"
B_MIN_SOC, B_MAX_SOC, B_EFFICIENCY = "min_soc", "max_soc", "round_trip_efficiency"
B_SOC_SENSOR, B_POWER_SENSOR, B_POWER_INVERT = "soc_sensor", "power_sensor", "power_invert"
B_DRIVER, B_HOST = "koppeling", "host"
DRIVER_VIRTUAL, DRIVER_ZENDURE, DRIVER_ENTITIES = "virtueel", "zendure", "entiteiten"

DEFAULT_SETTINGS = {
    "pv_factor": 1.0,
}

# Verbruikers met een eigen meter en een eigen model (bijvoorbeeld een hottub)
CONF_CONSUMERS = "consumers"
C_ID, C_NAME, C_ENERGY = "id", "name", "energy"
C_SETPOINT, C_CURRENT = "setpoint", "current_temp"
THERMOSTAT_BAND = 0.3        # graden onder de ingestelde temperatuur waarbij hij warmte vraagt

# Apparaten met programma's (Home Connect): wasmachine, vaatwasser, droger
CONF_APPLIANCES = "appliances"
A_ID, A_NAME, A_KIND = "id", "name", "soort"
A_STATE, A_FINISH, A_ENERGY = "state", "finish_time", "energy"
A_KWH, A_MINUTES = "kwh_per_cycle", "minutes"
KINDS = ["wasmachine", "vaatwasser", "droger", "overig"]
RUN_STATES = {"run", "pause", "actionrequired", "on"}         # programma loopt (Home Connect, of een binary_sensor)
DELAYED_STATES = {"delayedstart"}            # programma staat op uitgestelde start
CYCLE_DAYS = 28              # zo ver terug leert het EMS de gewoontes (zover de recorder reikt)

# Hoe lang een afwijking van nu nog doorwerkt in de voorspelling (halfwaardetijd, uren)
PERSIST_REST_H = 1.0
PERSIST_HEATING_H = 3.0
MAX_ANOMALY_W = 1500
