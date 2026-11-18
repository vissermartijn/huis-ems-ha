"""Draait de data_generators uit homeassistant/grafieken.yaml tegen nagebootste attributen.

Gebruik: python tests/apex_generators.py   (heeft node nodig)

Controleert dat elke generator zonder fouten loopt, ook als er nog geen plan of terugblik is,
en dat hij een lijst [tijdstip, getal of null] teruggeeft binnen het venster van de grafiek.
"""
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def generators(node):
    if isinstance(node, dict):
        if "data_generator" in node:
            yield node["name"], node["entity"], node["data_generator"]
        for value in node.values():
            yield from generators(value)
    elif isinstance(node, list):
        for item in node:
            yield from generators(item)


def sample_states():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=now.minute % 15)
    n = 144
    names = ["warmtepomp verwarmen", "warmtepomp tapwater", "Hottub", "Wasmachine", "Vaatwasser", "overig"]
    plan = {
        "start": start.isoformat(), "stap_min": 15,
        "zon_w": [max(0, 2000 - abs(i - 40) * 80) for i in range(n)],
        "verbruik_w": [500 + (i % 8) * 20 for i in range(n)],
        "onderdelen_w": {name: [100 + j * 10] * n for j, name in enumerate(names)},
        "net_w": [300 - (i % 50) * 30 for i in range(n)],
        "accu_w": [0] * n,
        "soc_pct": [min(100, 20 + i) for i in range(n)],
    }
    rows = []
    for i in range(192, 0, -1):
        t = start - timedelta(minutes=15 * i)
        row = {"t": t.isoformat(), "soc_pct": 50, "verwacht_verbruik_w": 600,
               "verwacht_zon_w": 800, "verwacht_net_w": -100, "verwacht_accu_w": 0,
               "verwacht": {name: 100 for name in names}, "basis": {"rest": 300},
               "etmaal_verbruik_w": 650 if i < 120 else None, "etmaal_zon_w": 700 if i < 120 else None,
               "etmaal_soc_pct": 40 if i < 120 else None}
        if i % 7:   # niet elk kwartier is volledig gemeten
            row.update(net_w=-200, net_zonder_accu_w=-300, werkelijk_zon_w=900, werkelijk_verbruik_w=600,
                       werkelijk={name: 100 for name in names})
        rows.append(row)
    full = {
        "sensor.huis_ems_plan": {"state": "1.2", "attributes": plan},
        "sensor.huis_ems_terugblik": {"state": "192", "attributes": {"kwartieren": rows}},
    }
    empty = {
        "sensor.huis_ems_plan": {"state": "unknown", "attributes": {}},
        "sensor.huis_ems_terugblik": {"state": "0", "attributes": {}},
    }
    window = [(start - timedelta(hours=25)).timestamp() * 1000, (start + timedelta(hours=37)).timestamp() * 1000]
    return {"vol": full, "leeg": empty}, window


RUNNER = r"""
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = [];
for (const c of cases) {
  for (const [label, states] of Object.entries(c.states)) {
    try {
      const fn = new Function('entity', 'start', 'end', 'hass', 'moment', c.code);
      const r = fn(states[c.entity] || {attributes: {}}, new Date(c.window[0]), new Date(c.window[1]), {states});
      const okShape = Array.isArray(r) && r.every((p) => Array.isArray(p) && p.length === 2
        && Number.isFinite(p[0]) && (p[1] === null || Number.isFinite(p[1])));
      const inWindow = r.every((p) => p[0] >= c.window[0] && p[0] <= c.window[1]);
      out.push({name: c.name, label, n: r.length, okShape, inWindow});
    } catch (e) {
      out.push({name: c.name, label, error: String(e)});
    }
  }
}
console.log(JSON.stringify(out));
"""


def main():
    board = yaml.safe_load((ROOT / "homeassistant" / "grafieken.yaml").read_text(encoding="utf-8"))
    states, window = sample_states()
    cases = [{"name": n, "entity": e, "code": c, "states": states, "window": window}
             for n, e, c in generators(board)]
    res = subprocess.run(["node", "-e", RUNNER], input=json.dumps(cases), capture_output=True,
                         text=True, check=True)
    failed = 0
    for r in json.loads(res.stdout):
        good = "error" not in r and r["okShape"] and r["inWindow"] and (r["label"] == "leeg" or r["n"] > 0)
        failed += not good
        print(("ok  " if good else "FOUT"), f"{r['name']:<20} {r['label']:<5}", r.get("error") or f"{r['n']} punten")
    print(f"{len(cases)} generators, {failed} fout")
    # De losse kaart moet precies de grafieken van het dashboard bevatten.
    card = yaml.safe_load((ROOT / "homeassistant" / "grafieken-kaart.yaml").read_text(encoding="utf-8"))

    def stacks(node):
        if isinstance(node, dict):
            if node.get("type") == "vertical-stack":
                yield node
            for v in node.values():
                yield from stacks(v)
        elif isinstance(node, list):
            for v in node:
                yield from stacks(v)

    stack = dict(next(stacks(board)))
    stack.pop("grid_options", None)
    same = stack == card
    print("losse kaart gelijk aan het dashboard:", "ja" if same else "NEE")
    sys.exit(1 if failed or not same else 0)


if __name__ == "__main__":
    main()
