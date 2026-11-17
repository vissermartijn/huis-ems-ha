"""Rooktest tegen een wegwerp-Home Assistant in Docker (zie README).

Doorloopt de eerste inrichting, stelt Huis EMS in via dezelfde stappen als de
gebruikersinterface en controleert de vooruitblik per onderdeel, de virtuele accu,
tapwater van de warmtepomp en een vaatwasser met uitgestelde start (zoals Home
Connect hem meldt). Een nagebootste Zendure-accu mag alleen worden uitgelezen,
nooit aangestuurd.
Gebruik: python tests/ha_smoke.py http://localhost:8124 [--serve]
"""
import json
import secrets
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/")
CLIENT = BASE + "/"
TOKEN = None


def call(path, data=None, form=False, method=None):
    headers = {}
    body = None
    if data is not None:
        if form:
            body = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in data.items()).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode()
    except urllib.error.HTTPError as err:
        raise SystemExit(f"{path}: HTTP {err.code} {err.read().decode()[:500]}")
    return json.loads(text) if text else None


def wait_ready():
    for _ in range(90):
        try:
            urllib.request.urlopen(BASE + "/manifest.json", timeout=5)
            return
        except Exception:
            time.sleep(2)
    raise SystemExit("Home Assistant start niet")


def login():
    global TOKEN
    code = call("/api/onboarding/users", {
        "client_id": CLIENT, "name": "Test", "username": "test",
        "password": secrets.token_urlsafe(16), "language": "nl"})["auth_code"]
    TOKEN = call("/auth/token", {"grant_type": "authorization_code", "code": code,
                                 "client_id": CLIENT}, form=True)["access_token"]


def options(entry_id, step, data, pick=None):
    """Doorloopt één stap van het optiemenu; pick kiest eerst een bestaand item op naam."""
    flow = call("/api/config/config_entries/options/flow", {"handler": entry_id})
    assert flow["type"] == "menu", flow
    form = call(f"/api/config/config_entries/options/flow/{flow['flow_id']}", {"next_step_id": step})
    assert form["type"] == "form", form
    if pick is not None:
        choices = form["data_schema"][0]["selector"]["select"]["options"]
        value = next(c["value"] for c in choices if c["label"] == pick)
        form = call(f"/api/config/config_entries/options/flow/{flow['flow_id']}",
                    {form["data_schema"][0]["name"]: value})
        assert form["type"] == "form", form
    result = call(f"/api/config/config_entries/options/flow/{flow['flow_id']}", data)
    assert result["type"] == "create_entry", result
    time.sleep(4)  # de integratie herlaadt na elke wijziging


class FakeP1:
    """Bootst de lokale API van een HomeWizard P1-meter na op poort 8125."""

    def __init__(self):
        self.total, self.jitter = 600, 0
        self.start()

    def set(self, total, jitter=0):
        self.total, self.jitter = total, jitter

    def start(self):
        import http.server
        import random
        import threading
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                noise = random.gauss(0, fake.jitter) if fake.jitter else 0
                body = json.dumps({"active_power_w": fake.total + noise}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", 8125), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class FakeZendure:
    """Bootst de lokale API van een Zendure-accu na op poort 8126 en onthoudt elke schrijfpoging."""

    def __init__(self):
        import http.server
        import threading
        self.soc = 60
        self.writes = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send({"sn": "TESTSN0001", "product": "solarFlow3000MixAC", "properties": {
                    "electricLevel": fake.soc, "outputPackPower": 800, "packInputPower": 0}})

            def do_POST(self):
                fake.writes.append(self.path)
                self._send({"success": True})

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", 8126), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def serve(entry_id, meter, minutes=30):
    """Houdt de proefomgeving in de lucht om het dashboard te bekijken.

    Vult eerst een etmaal verzonnen terugblik in, zodat de tijdlijn iets te
    tonen heeft, en laat de nagebootste meter daarna rustig variëren.
    """
    import math
    import os
    import random
    import subprocess
    from datetime import datetime, timedelta, timezone
    path = os.path.join(os.environ["EMS_TEST_WORK"], ".storage", f"huis_ems.{entry_id}")
    subprocess.run(["docker", "stop", "ha-ems-test"], check=True, capture_output=True)
    store = json.load(open(path, encoding="utf-8"))
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 15)
    history, soc = [], 35.0
    for i in range(96, 0, -1):
        t = now - timedelta(minutes=15 * i)
        hour = (t.hour + 1 + t.minute / 60) % 24          # ongeveer lokale tijd
        sun = max(0.0, math.sin(math.pi * (hour - 8.5) / 8)) * 3200 if 8.5 < hour < 16.5 else 0.0
        heat = 600 + 300 * math.cos(math.pi * hour / 12)
        tap = 1800 if 3 <= hour < 4 else 0.0
        dish = 1400 if 19.5 <= hour < 20.25 else 150 if 20.25 <= hour < 22 else 0.0
        rest = 250 + 600 * math.exp(-((hour - 18.5) / 2) ** 2) + 300 * math.exp(-((hour - 7.5) / 1.2) ** 2)
        load = heat + tap + dish + rest
        net = load - sun
        accu = max(-3000, min(3000, -net))
        if (accu > 0 and soc >= 100) or (accu < 0 and soc <= 10):
            accu = 0
        soc = max(10, min(100, soc + accu / 1000 / 4 / 8 * 100))
        noise = random.gauss(0, 120)
        actual = {"warmtepomp verwarmen": round(heat * random.uniform(0.85, 1.15)),
                  "warmtepomp tapwater": round(tap), "Vaatwasser": round(dish),
                  "overig": round(max(0, rest + noise))}
        actual_load = sum(actual.values())
        actual_sun = sun * random.uniform(0.8, 1.05)
        expected = {"warmtepomp verwarmen": round(heat), "warmtepomp tapwater": round(tap * 0.8),
                    "Vaatwasser": round(dish * 0.7), "overig": round(rest)}
        history.append({"t": t.isoformat(), "soc_pct": round(soc),
                        "verwacht_verbruik_w": round(load), "verwacht_zon_w": round(sun),
                        "verwacht_net_w": round(net + accu), "verwacht_accu_w": round(accu),
                        "verwacht": expected, "basis": {"rest": round(rest + dish * 0.7)},
                        "etmaal_verbruik_w": round(load * random.uniform(0.85, 1.15)),
                        "etmaal_zon_w": round(sun * random.uniform(0.7, 1.2)),
                        "etmaal_soc_pct": round(min(100, soc * random.uniform(0.9, 1.1))),
                        "etmaal": expected,
                        "werkelijk_verbruik_w": round(actual_load), "werkelijk_zon_w": round(actual_sun),
                        "werkelijk_rest_ruw_w": actual["overig"] + actual["Vaatwasser"],
                        "werkelijk": actual,
                        "net_w": round(actual_load - actual_sun + accu),
                        "net_zonder_accu_w": round(actual_load - actual_sun)})
    store["data"]["history"] = history
    json.dump(store, open(path, "w", encoding="utf-8"))
    subprocess.run(["docker", "start", "ha-ems-test"], check=True, capture_output=True)
    wait_ready()
    print(f"\nProefomgeving draait {minutes} minuten op {BASE}/ems-test/overzicht")
    sys.stdout.flush()
    end = time.time() + minutes * 60
    while time.time() < end:
        meter.set(700 + 500 * math.sin(time.time() / 40), jitter=25)
        time.sleep(5)


def state(entity_id):
    return call(f"/api/states/{entity_id}")


def num(entity_id):
    try:
        return float(state(entity_id)["state"])
    except (TypeError, ValueError):
        return None


FAILURES = []


def expect(condition, scenario):
    """Legt een mislukte controle vast met het scenario erbij; de test loopt door."""
    print(("  OK   " if condition else "  FOUT ") + scenario)
    if not condition:
        FAILURES.append(scenario)


def show(title):
    print(f"\n--- {title} ---")
    look = state("sensor.huis_ems_vooruitblik")
    print("vooruitblik:", look["state"], "|", look["attributes"].get("status"))
    print("accu", state("sensor.huis_ems_accu_lading")["state"], "% |",
          state("sensor.huis_ems_accu_vermogen")["state"], "W | net met accu",
          state("sensor.huis_ems_net_met_accu")["state"], "W, zonder",
          state("sensor.huis_ems_net_zonder_accu")["state"], "W")
    print("onderdelen 24u:", state("sensor.huis_ems_plan")["attributes"].get("onderdelen_24u_kwh"))


def part_kwh(name, first=0, last=None):
    """Verwacht verbruik van een onderdeel in kWh tussen twee kwartieren vanaf nu."""
    plan = state("sensor.huis_ems_plan")["attributes"]
    values = (plan.get("onderdelen_w") or {}).get(name) or []
    return sum(values[first:last]) / 4 / 1000, values


def main():
    wait_ready()
    login()
    time.sleep(15)  # sjabloonsensoren laten opkomen
    flow = call("/api/config/config_entries/flow", {"handler": "huis_ems", "show_advanced_options": False})
    assert flow["type"] == "form", flow
    print("velden eerste stap:", [f["name"] for f in flow["data_schema"]])
    result = call(f"/api/config/config_entries/flow/{flow['flow_id']}", {
        "p1_power": "sensor.p1_vermogen", "import_energy": ["sensor.afname_t1"],
        # Een oude teller van een verwijderde integratie mag de meting niet blokkeren.
        "pv_energy": ["sensor.oude_zonneteller", "sensor.zon_opgewekt"],
        "use_energy_forecast": True, "pv_forecast_sensors": ["sensor.testzon"],
        "pv_forecast_unit": "kw", "horizon_hours": 36})
    assert result["type"] == "create_entry", result
    entry_id = result["result"]["entry_id"]

    meter = FakeP1()
    options(entry_id, "net", {"p1_host": "host.docker.internal:8125"})
    options(entry_id, "accu_toevoegen", {
        "name": "Zendure", "koppeling": "virtueel", "capacity_kwh": 8, "max_charge_w": 3000,
        "max_discharge_w": 3000, "min_soc": 10, "max_soc": 100, "round_trip_efficiency": 86,
        "power_invert": False})
    time.sleep(15)

    look = state("sensor.huis_ems_vooruitblik")
    plan = state("sensor.huis_ems_plan")["attributes"]
    show("na het instellen")
    expect(bool(look["attributes"].get("tekst")), "vooruitblik: er is een tekst")
    expect(len(plan.get("zon_w") or []) >= 96 and len(plan.get("soc_pct") or []) == len(plan["zon_w"]),
           "vooruitblik: plan met zon en lading voor minstens 24 uur")
    expect(list(plan.get("onderdelen_w") or {}) == ["overig"], "vooruitblik: zonder meters alleen 'overig'")
    expect(not any(s["entity_id"].startswith(("switch.huis_ems", "binary_sensor.huis_ems"))
                   for s in call("/api/states")), "opruimen: geen schakelaars of signalen van het verschuiven")

    # De virtuele accu doet wat een accu op zelfverbruik doet: de meter volgen.
    meter.set(-2000)
    time.sleep(20)
    show("2000 W overschot")
    grid = num("sensor.huis_ems_net_met_accu")
    expect(grid is not None and abs(grid) < 150,
           "virtuele accu: neemt het overschot op, net met accu rond nul")
    expect((num("sensor.huis_ems_accu_vermogen") or 0) > 1800, "virtuele accu: laadt ongeveer 2000 W")

    # Warmtepomp: verwarmen en tapwater apart.
    options(entry_id, "warmtepomp", {
        "hp_energy": ["sensor.warmtepomp_energie"], "dhw_active": "binary_sensor.tapwater_bezig",
        "outdoor_temp": "sensor.buitentemperatuur"})
    time.sleep(10)
    names = list(state("sensor.huis_ems_plan")["attributes"].get("onderdelen_w") or {})
    # Zonder historie is er nog geen stookmodel; tapwater staat er wel meteen in.
    expect("warmtepomp tapwater" in names and names[-1] == "overig",
           f"warmtepomp: tapwater als eigen onderdeel ({names})")
    call("/api/services/input_boolean/turn_on", {"entity_id": "input_boolean.tapwater"})
    time.sleep(10)
    show("tapwater bezig")
    now_kwh, _ = part_kwh("warmtepomp tapwater", 0, 8)
    expect(now_kwh > 0.5, f"tapwater: een lopende cyclus komt de eerste twee uur in de voorspelling ({now_kwh:.2f} kWh)")
    expect("tapwater" in (state("sensor.huis_ems_vooruitblik")["attributes"].get("status") or ""),
           "tapwater: de status noemt de lopende cyclus")
    call("/api/services/input_boolean/turn_off", {"entity_id": "input_boolean.tapwater"})

    # Vaatwasser via Home Connect: uitgestelde start, klaar over vier uur.
    options(entry_id, "apparaat_toevoegen", {
        "name": "Vaatwasser", "soort": "vaatwasser", "state": "sensor.vaatwasser_bedrijfstoestand",
        "finish_time": "sensor.vaatwasser_klaar", "kwh_per_cycle": 1.0, "minutes": 180})
    call("/api/services/input_number/set_value", {"entity_id": "input_number.vaat_klaar_min", "value": 240})
    call("/api/services/input_select/select_option",
         {"entity_id": "input_select.vaatwasser", "option": "delayedstart"})
    time.sleep(12)
    show("vaatwasser uitgesteld, klaar over 4 uur")
    early, _ = part_kwh("Vaatwasser", 1, 3)
    planned, values = part_kwh("Vaatwasser", 0, 20)
    expect(early < 0.02, f"vaatwasser: niet vóór de uitgestelde start ({early:.2f} kWh)")
    expect(0.9 < planned < 1.1, f"vaatwasser: één programma ingepland tussen start en eindtijd ({planned:.2f} kWh)")
    expect("uitgestelde start" in (state("sensor.huis_ems_vooruitblik")["attributes"].get("apparaten") or {})
           .get("Vaatwasser", ""), "vaatwasser: de vooruitblik noemt de uitgestelde start")
    call("/api/services/input_select/select_option", {"entity_id": "input_select.vaatwasser", "option": "run"})
    time.sleep(12)
    running, _ = part_kwh("Vaatwasser", 0, 4)
    expect(running > 0.2, f"vaatwasser: loopt nu, verbruik meteen ingepland ({running:.2f} kWh in het eerste uur)")
    model = state("sensor.huis_ems_verbruiksmodel")["attributes"]
    expect("Vaatwasser" in (model.get("apparaten") or {}), "vaatwasser: gewoonte staat in het verbruiksmodel")
    call("/api/services/input_select/select_option", {"entity_id": "input_select.vaatwasser", "option": "finished"})

    # Meter valt weg: geen overschot meer bekend.
    meter.stop()
    time.sleep(45)
    show("P1-meter onbereikbaar")
    expect("P1-meter" in (state("sensor.huis_ems_vooruitblik")["attributes"].get("status") or ""),
           "meteruitval: na 20 seconden zonder meting meldt de status het")
    meter.start()
    meter.set(600)
    time.sleep(10)

    # Zendure: alleen uitlezen.
    zendure = FakeZendure()
    options(entry_id, "accu_toevoegen", {
        "name": "Zen echt", "koppeling": "zendure", "host": "host.docker.internal:8126",
        "capacity_kwh": 8, "max_charge_w": 3000, "max_discharge_w": 3000, "min_soc": 10,
        "max_soc": 100, "round_trip_efficiency": 86, "power_invert": False})
    time.sleep(20)
    show("Zendure erbij")
    expect(num("sensor.huis_ems_zen_echt_lading") == 60, "Zendure: lading wordt lokaal uitgelezen")
    expect(zendure.writes == [], f"Zendure: het EMS stuurt de accu niet aan ({len(zendure.writes)} schrijfpogingen)")

    # Andere accu via entiteiten: de laadtoestand telt mee.
    options(entry_id, "accu_toevoegen", {
        "name": "Nep", "koppeling": "entiteiten", "capacity_kwh": 5, "max_charge_w": 2500,
        "max_discharge_w": 2500, "min_soc": 10, "max_soc": 100, "round_trip_efficiency": 88,
        "soc_sensor": "sensor.nep_accu_lading", "power_invert": False})
    time.sleep(15)
    show("drie accu's")
    accus = state("sensor.huis_ems_accu_lading")["attributes"]["accus"]
    expect(len(accus) == 3, "accu's: alle drie in het overzicht")
    expect(not any(s["entity_id"].startswith("select.huis_ems") for s in call("/api/states")),
           "opruimen: geen keuzelijsten van de oude accusturing")

    # Leert het EMS het stookgedrag uit historie? Eigen meetdata inladen als die er is.
    import ha_learn
    if ha_learn.available():
        from datetime import datetime, timedelta
        shift = timedelta(days=(datetime.now() - datetime(2026, 11, 1)).days - 1)
        ha_learn.import_all(BASE, TOKEN, shift)
        time.sleep(10)
        options(entry_id, "bronnen", {
            "p1_power": "sensor.p1_vermogen", "import_energy": ["sensor.afname_t1"],
            "export_energy": ["sensor.teruglevering"],
            "pv_energy": ["sensor.oude_zonneteller", "sensor.zon_opgewekt"],
            "use_energy_forecast": True, "pv_forecast_sensors": ["sensor.testzon"], "pv_forecast_unit": "kw",
            "horizon_hours": 36})
        options(entry_id, "warmtepomp", {
            "hp_energy": ["sensor.warmtepomp_energie"], "dhw_active": "binary_sensor.tapwater_bezig",
            "outdoor_temp": "sensor.buitentemperatuur"})
        options(entry_id, "verbruiker_toevoegen", {
            "name": "Hottub", "energy": ["sensor.hottub_energie"],
            "setpoint": "input_number.hottub_doel", "current_temp": "sensor.hottub_temperatuur"})
        time.sleep(30)
        model = state("sensor.huis_ems_verbruiksmodel")
        print("\n--- geleerd uit ingeladen meetdata ---")
        print(model["state"], "|", model["attributes"]["toelichting"])
        print("onderdelen 24u:", state("sensor.huis_ems_plan")["attributes"]["onderdelen_24u_kwh"])
        expect(model["state"] == "geleerd", "leren: model geleerd uit de ingeladen historie")

    # De sjablonen van het dashboard moeten zonder fouten renderen.
    import pathlib
    import textwrap
    import yaml
    board = [yaml.safe_load(pathlib.Path(__file__).parent.parent.joinpath(
        "homeassistant", name).read_text(encoding="utf-8")) for name in ("dashboard.yaml", "grafieken.yaml")]

    def cards(node):
        if isinstance(node, dict):
            if node.get("type") == "markdown":
                yield node
            for value in node.values():
                yield from cards(value)
        elif isinstance(node, list):
            for item in node:
                yield from cards(item)

    for number, card in enumerate(cards(board), start=1):
        req = urllib.request.Request(
            BASE + "/api/template", data=json.dumps({"template": card["content"]}).encode(),
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        try:
            text = urllib.request.urlopen(req, timeout=60).read().decode()
            ok = True
        except urllib.error.HTTPError as err:
            text, ok = f"FOUT {err.code}: {err.read().decode()[:400]}", False
        print(f"\n[kaart {number}]\n" + textwrap.shorten(text, 500))
        expect(ok, f"dashboard: markdownkaart {number} rendert")

    # Alleen kwartieren die lang genoeg gemeten zijn (na elk herladen begint de meting opnieuw).
    rows = [r for r in state("sensor.huis_ems_terugblik")["attributes"].get("kwartieren") or []
            if "net_w" in r]
    print("gemeten kwartieren, zon:", [r.get("werkelijk_zon_w") for r in rows])
    if rows:
        expect(any(r.get("werkelijk_zon_w") is not None for r in rows),
               "zonmeting: een oude teller zonder waarde blokkeert de meting per kwartier niet")
        expect(all("verwacht" in r and "basis" in r for r in rows),
               "terugblik: elk kwartier heeft de verwachting per onderdeel")
    now_q = state("sensor.huis_ems_verbruik_dit_kwartier")
    print("dit kwartier:", now_q["state"], now_q["attributes"])
    print("\nterugblik:", state("sensor.huis_ems_terugblik")["state"], "kwartieren | voorspelkwaliteit:",
          state("sensor.huis_ems_voorspelkwaliteit")["attributes"])
    if "--serve" in sys.argv:
        serve(entry_id, meter)
        return

    print("entiteiten:", sorted(s["entity_id"] for s in call("/api/states") if "huis_ems" in s["entity_id"]))
    if FAILURES:
        print(f"\n{len(FAILURES)} controle(s) mislukt:")
        for scenario in FAILURES:
            print("  -", scenario)
        sys.exit(1)
    print("\nAlle controles geslaagd.")


if __name__ == "__main__":
    main()
