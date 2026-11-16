"""Laadt eigen meetdata als statistieken in de wegwerp-HA en toont wat het EMS ervan leert.

Gebruik (na tests/ha_test.sh, zelfde container): wordt aangeroepen vanuit ha_smoke.py
als data/p1.csv, data/wp.csv en data/weer.json bestaan.
"""
import collections
import csv
import json
import pathlib
from datetime import datetime, timedelta, timezone

DATA = pathlib.Path(__file__).parent.parent / "data"


def _hourly(path, plus, minus=()):
    rows = list(csv.reader(open(path)))[1:]
    out = collections.defaultdict(float)
    for a, b in zip(rows, rows[1:]):
        t = datetime.strptime(a[0][:13], "%Y-%m-%d %H")
        out[t] += sum(float(b[i]) - float(a[i]) for i in plus) - sum(float(b[i]) - float(a[i]) for i in minus)
    return out


def available():
    return (DATA / "p1.csv").exists() and (DATA / "weer.json").exists() and _wp().exists()


def _wp():
    """Bij voorkeur het hele jaar van de warmtepomp."""
    return DATA / "wp_jaar.csv" if (DATA / "wp_jaar.csv").exists() else DATA / "wp.csv"


def payloads(shift):
    """shift: verschuiving zodat de laatste meting vlak voor nu valt."""
    def iso(t):
        return (t + shift).replace(tzinfo=timezone(timedelta(hours=1))).isoformat()

    def sums(series):
        total, out = 0.0, []
        for t in sorted(series):
            total += series[t]
            out.append({"start": iso(t), "sum": round(total, 3), "state": round(total, 3)})
        return out

    weather = json.load(open(DATA / "weer.json"))
    temps = [{"start": iso(datetime.fromisoformat(t)), "mean": v, "min": v, "max": v}
             for t, v in zip(weather["hourly"]["time"], weather["hourly"]["temperature_2m"]) if v is not None]
    energy = {"has_mean": False, "mean_type": 0, "has_sum": True, "unit_of_measurement": "kWh",
              "unit_class": "energy", "source": "recorder"}
    wp = _hourly(_wp(), [1])
    extra = []
    if (DATA / "hottub.csv").exists():
        tub = {t: v for t, v in _hourly(DATA / "hottub.csv", [1]).items() if t in wp}
        extra.append({**energy, "statistic_id": "sensor.hottub_energie", "name": None, "stats": sums(tub)})
        # Er is geen meetreeks van de watertemperatuur. Voor de test: een graad te koud in
        # het uur voordat de verwarming flink draait, anders op temperatuur.
        extra.append({"has_mean": True, "mean_type": 1, "has_sum": False, "unit_of_measurement": "°C",
                      "unit_class": "temperature", "source": "recorder",
                      "statistic_id": "sensor.hottub_temperatuur", "name": None,
                      "stats": [{"start": iso(t - timedelta(hours=1)), "mean": v, "min": v, "max": v}
                                for t, v in ((t, 36.0 if kwh > 0.3 else 37.0) for t, kwh in sorted(tub.items()))]})
    return extra + [
        {**energy, "statistic_id": "sensor.afname_t1", "name": None,
         "stats": sums({t: v for t, v in _hourly(DATA / "p1.csv", [1, 2]).items() if t in wp})},
        {**energy, "statistic_id": "sensor.teruglevering", "name": None,
         "stats": sums({t: v for t, v in _hourly(DATA / "p1.csv", [3, 4]).items() if t in wp})},
        {**energy, "statistic_id": "sensor.warmtepomp_energie", "name": None, "stats": sums(wp)},
        {"has_mean": True, "mean_type": 1, "has_sum": False, "unit_of_measurement": "°C",
         "unit_class": "temperature", "source": "recorder",
         "statistic_id": "sensor.buitentemperatuur", "name": None, "stats": temps},
    ]


def import_all(base, token, shift):
    """Statistieken importeren kan alleen via de websocket-API."""
    import websocket  # pip install websocket-client
    ws = websocket.create_connection(base.replace("http", "ws", 1) + "/api/websocket", timeout=120)
    ws.recv()
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    assert json.loads(ws.recv())["type"] == "auth_ok"
    for n, payload in enumerate(payloads(shift), start=1):
        stats = payload.pop("stats")
        ws.send(json.dumps({"id": n, "type": "recorder/import_statistics",
                            "metadata": payload, "stats": stats}))
        reply = json.loads(ws.recv())
        assert reply.get("success"), reply
    ws.close()
