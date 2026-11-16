"""Voorspellingen uit Home Assistant-attributen omzetten naar kwartierslots.

Integraties leveren prijzen en zonvoorspellingen elk in een eigen vorm. De
functies hier zijn bewust tolerant: ze zoeken zelf de tijd- en waardevelden.
"""
import math
from datetime import datetime, timedelta, timezone

TIME_KEYS = ("start", "start_time", "period_start", "time", "datetime", "from", "hour", "date")
VALUE_KEYS = ("value", "price", "pv_estimate", "electricity_price", "total", "watts", "wh", "energy")
LIST_ATTRS = ("raw_today", "raw_tomorrow", "prices_today", "prices_tomorrow", "prices",
              "forecast", "detailedForecast", "detailedHourly", "wh_period", "watts", "data")


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


KNOWN, ESTIMATED = "bekend", "geschat"
MAX_VALID = timedelta(hours=1)   # langer dan dit zonder nieuwe waarde is een gat in de reeks


def _valid_at(series, period, t):
    """Waarde die op tijdstip t echt geldt, of None (voor de reeks, erna, of in een gat)."""
    value, until = None, None
    for i, (ts, v) in enumerate(series):
        if ts > t:
            break
        nxt = series[i + 1][0] if i + 1 < len(series) else ts + period
        value, until = v, min(nxt, ts + max(period, MAX_VALID))
    return value if until is not None and t < until else None


def price_lookup(series, slot_starts, repeat_hours=24, max_repeats=1):
    """Prijs per slot met herkomst: [(waarde, "bekend" | "geschat")] of (None, None).

    Bekend: de reeks dekt dit tijdstip. Geschat: de reeks dekt het niet, maar
    wel hetzelfde tijdstip een etmaal eerder; alleen voor slots na de laatste
    bekende prijs. Een oude reeks levert dus geen prijs voor nu op.
    """
    if not series:
        return [(None, None)] * len(slot_starts)
    period = timedelta(seconds=_period(series))
    end = series[-1][0] + period
    out = []
    for t in slot_starts:
        value = _valid_at(series, period, t)
        if value is not None:
            out.append((value, KNOWN))
            continue
        found = None
        if t >= end:
            for n in range(1, max_repeats + 1):
                found = _valid_at(series, period, t - timedelta(hours=repeat_hours * n))
                if found is not None:
                    break
        out.append((found, ESTIMATED) if found is not None else (None, None))
    return out


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


def bell_distribution(slot_starts, slot_hours, sunrise, sunset, total_kwh):
    """Verdeelt een dagtotaal over de slots tussen zonsopkomst en -ondergang.

    Noodoplossing voor integraties die alleen dagtotalen geven. Het totaal
    wordt verdeeld over de slots die nog in het venster vallen.
    """
    span = (sunset - sunrise).total_seconds()
    weights = []
    for t, hours in zip(slot_starts, slot_hours):
        mid = t + timedelta(hours=hours / 2)
        x = (mid - sunrise).total_seconds() / span if span > 0 else -1
        weights.append(math.sin(math.pi * x) ** 1.5 * hours if 0 < x < 1 else 0.0)
    total_w = sum(weights)
    if total_w <= 0:
        return [0.0] * len(slot_starts)
    return [total_kwh * w / total_w for w in weights]


def load_profile(history, slots_per_day=96):
    """Gemiddeld vermogen (W) per kwartier van de dag uit sensorhistorie.

    history: [(tijdstip in lokale tijd, vermogen in W)], oplopend. Weegt
    naar tijdsduur; gaten langer dan een uur tellen niet mee.
    """
    sums = [0.0] * slots_per_day
    weights = [0.0] * slots_per_day
    minutes = 1440 // slots_per_day
    for (t0, v0), (t1, _) in zip(history, history[1:]):
        dur = (t1 - t0).total_seconds()
        if dur <= 0 or dur > 3600:
            continue
        bucket = (t0.hour * 60 + t0.minute) // minutes
        sums[bucket] += v0 * dur
        weights[bucket] += dur
    known = [s / w for s, w in zip(sums, weights) if w > 0]
    if not known:
        return None
    fallback = sum(known) / len(known)
    return [s / w if w > 0 else fallback for s, w in zip(sums, weights)]
