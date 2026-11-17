"""Voorspellingen uit Home Assistant-attributen omzetten naar kwartierslots.

Integraties leveren zonvoorspellingen elk in een eigen vorm. De
functies hier zijn bewust tolerant: ze zoeken zelf de tijd- en waardevelden.
"""
from datetime import datetime, timedelta, timezone

TIME_KEYS = ("start", "start_time", "period_start", "time", "datetime", "from", "hour", "date")
VALUE_KEYS = ("value", "pv_estimate", "total", "watts", "wh", "energy")
LIST_ATTRS = ("forecast", "detailedForecast", "detailedHourly", "wh_period", "watts", "data")


def parse_time(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, timezone.utc)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_series(attributes, attribute_names=None):
    """Haalt [(tijdstip, waarde)] uit de attributen van een sensor.

    Ondersteunt lijsten van dicts ({"start": ..., "value": ...}) en dicts
    van tijdstip naar waarde. Meerdere attributen worden samengevoegd.
    """
    points = {}
    for name in attribute_names or LIST_ATTRS:
        raw = attributes.get(name)
        if isinstance(raw, dict):
            items = [{"time": k, "value": v} for k, v in raw.items()]
        elif isinstance(raw, list):
            items = raw
        else:
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            t = next((item[k] for k in TIME_KEYS if k in item), None)
            v = next((item[k] for k in VALUE_KEYS if k in item), None)
            if t is None or v is None:
                continue
            try:
                points[parse_time(t)] = float(v)
            except (TypeError, ValueError):
                continue
    return sorted(points.items())


def _period(series):
    gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(series, series[1:])]
    return min(gaps) if gaps else 3600.0


def energy_per_slot(series, slot_starts, slot_hours, unit):
    """Zonopwek in kWh per slot.

    unit "kw": waarden zijn gemiddeld vermogen in kW over de periode.
    unit "w": idem in W. unit "wh_period"/"kwh_period": energie per periode.
    """
    if not series:
        return [0.0] * len(slot_starts)
    period_h = _period(series) / 3600.0
    out = []
    for t, hours in zip(slot_starts, slot_hours):
        val = 0.0
        for ts, v in series:
            if ts <= t < ts + timedelta(hours=period_h):
                val = v
                break
        if unit == "kw":
            kwh = val * hours
        elif unit == "w":
            kwh = val / 1000.0 * hours
        elif unit == "wh_period":
            kwh = val / 1000.0 * hours / period_h
        else:  # kwh_period
            kwh = val * hours / period_h
        out.append(max(0.0, kwh))
    return out
