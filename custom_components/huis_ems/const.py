"""Constanten voor Huis EMS."""
DOMAIN = "huis_ems"
SLOT_MIN = 15
WATCHDOG_SECONDS = 5
NOTIFY_SECONDS = 5
HISTORY_SLOTS = 192          # 48 uur terugkijken in de attributen
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
STALE_SECONDS = 20           # zo lang geen meting: geen overschot meer bekend
SOC_STALE_SECONDS = 900      # een laadtoestand die zo lang niet gemeld is telt niet meer

# Opties: tarieven
CONF_TARIFF_MODE = "tariff_mode"
TARIFF_FIXED, TARIFF_SPOT, TARIFF_ALL_IN = "vast", "dynamisch_kaal", "dynamisch_all_in"
CONF_FIXED_BUY = "fixed_buy"
CONF_FIXED_SELL = "fixed_sell"
CONF_PRICE_SENSOR = "price_sensor"
CONF_PRICE_SCALE = "price_scale"
CONF_ENERGY_TAX = "energy_tax"
CONF_BUY_FEE = "buy_fee"
CONF_SELL_FEE = "sell_fee"
CONF_VAT = "vat"
CONF_NET_METERING = "salderen"

# Opties: warmtepomp en weer
CONF_HP_ENERGY = "hp_energy"
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
    "stage2": False,          # verschuifbaar verbruik echt schakelen (uit: alleen advies)
}

# Verbruikers met een eigen model (alleen voorspellen, niet regelen)
CONF_CONSUMERS = "consumers"
C_ID, C_NAME, C_ENERGY = "id", "name", "energy"
C_SETPOINT, C_CURRENT = "setpoint", "current_temp"
THERMOSTAT_BAND = 0.3        # graden onder de ingestelde temperatuur waarbij hij warmte vraagt

# Verschuifbaar verbruik: warmtebuffers die zonnestroom opnemen als de accu vol is.
# (De sleutel heet nog stage2_loads, van de tweede trap uit een eerdere versie.)
CONF_STAGE2 = "stage2_loads"
L_ID, L_NAME, L_ENTITY, L_POWER, L_KIND = "id", "name", "entity", "power_w", "soort"
L_POWER_SENSOR, L_ENERGY, L_ORDER = "power_sensor", "energy", "order"
L_ON_DELAY, L_OFF_DELAY, L_MIN_ON, L_MIN_OFF = (
    "on_delay_min", "off_delay_min", "min_on_min", "min_off_min")
L_MIN_SOC, L_TEMP, L_MAX_TEMP, L_BOOST = "min_soc_pct", "temp_sensor", "max_temp", "boost_c"
L_START, L_END, L_FORECAST, L_REST = "start_hour", "end_hour", "need_forecast", "rest_min"
SPARE_SMOOTH_SECONDS = 30    # het overschot wordt zo lang uitgemiddeld
SWITCH_CONFIRM_SECONDS = 30  # zo lang mag een schakelaar erover doen om de nieuwe stand te melden
SWITCH_TRIES = 3             # zo vaak wordt een schakelopdracht geprobeerd
HANDS_OFF_SECONDS = 7200     # na een handmatige wijziging zo lang niet opnieuw beginnen
BOOST_DAYS_KEPT = 400       # zo veel dagen onthouden in welke uren de thermostaat hoger stond
