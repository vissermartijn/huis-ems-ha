"""Rooktest tegen een wegwerp-Home Assistant in Docker (zie README).

Doorloopt de eerste inrichting, stelt Huis EMS in via dezelfde stappen als de
gebruikersinterface en controleert de vooruitblik, de virtuele accu en de twee
warmtebuffers: een boilerelement (schakelaar) en een thermostaat. Een
nagebootste Zendure-accu mag alleen worden uitgelezen, nooit aangestuurd.
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
        load = 450 + 900 * math.exp(-((hour - 18.5) / 2) ** 2) + 500 * math.exp(-((hour - 7.5) / 1.2) ** 2)
        price = 0.196 + (0.12 if 17 <= hour < 21 else 0.05 if 7 <= hour < 9 else -0.05 if 11 <= hour < 15 else 0)
        net = load - sun
        accu = max(-3000, min(3000, -net))
        if (accu > 0 and soc >= 100) or (accu < 0 and soc <= 10):
            accu = 0
        soc = max(10, min(100, soc + accu / 1000 / 4 / 8 * 100))
        moved = {"Boiler": 15} if soc >= 99 and net < -2000 else {}
        actual_load = load + random.gauss(0, 150) + (2000 if moved else 0)
        actual_sun = sun * random.uniform(0.8, 1.05)
        history.append({"t": t.isoformat(), "koop": round(price, 4), "verkoop": 0.05, "soc_pct": round(soc),
                        "verwacht_verbruik_w": round(load), "verwacht_zon_w": round(sun),
                        "verwacht_net_w": round(net + accu), "verwacht_accu_w": round(accu),
                        "werkelijk_verbruik_w": round(actual_load), "werkelijk_zon_w": round(actual_sun),
                        "net_w": round(actual_load - actual_sun + accu),
                        "net_zonder_accu_w": round(actual_load - actual_sun), "verschoven_min": moved})
    store["data"]["history"] = history
    store["data"]["costs"] = {"zonder": 41.3, "met": 29.8, "dag": datetime.now().date().isoformat(),
                              "dag_zonder": 3.42, "dag_met": 2.31}
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


def flex(name):
    items = state("sensor.huis_ems_verschuifbaar_verbruik")["attributes"].get("buffers") or []
    return next((i for i in items if i["naam"] == name), {})


def show(title):
    print(f"\n--- {title} ---")
    look = state("sensor.huis_ems_vooruitblik")
    flex_state = state("sensor.huis_ems_verschuifbaar_verbruik")
    print("vooruitblik:", look["state"])
    print("accu", state("sensor.huis_ems_accu_lading")["state"], "% |",
          state("sensor.huis_ems_accu_vermogen")["state"], "W | net met accu",
          state("sensor.huis_ems_net_met_accu")["state"], "W, zonder",
          state("sensor.huis_ems_net_zonder_accu")["state"], "W")
    print("verschuiven:", flex_state["state"], "| over", flex_state["attributes"].get("overschot_w"), "W")
    for item in flex_state["attributes"].get("buffers") or []:
        print(f"  {item['naam']}: {item['stand']} | {item['beheer']} | {item['toelichting']}")


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

    options(entry_id, "tarieven", {
        "tariff_mode": "dynamisch_kaal", "salderen": False, "fixed_buy": 0.27, "fixed_sell": 0.05,
        "price_sensor": "sensor.testprijzen", "price_scale": 1, "energy_tax": 0.0916,
        "buy_fee": 0.02, "sell_fee": 0, "vat": 21})
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

    # De virtuele accu doet wat een accu op zelfverbruik doet: de meter volgen.
    meter.set(-2000)
    time.sleep(20)
    show("2000 W overschot")
    grid = num("sensor.huis_ems_net_met_accu")
    expect(grid is not None and abs(grid) < 150,
           "virtuele accu: neemt het overschot op, net met accu rond nul")
    expect((num("sensor.huis_ems_accu_vermogen") or 0) > 1800, "virtuele accu: laadt ongeveer 2000 W")

    # Boiler: element van 2000 W. Eerst moet de accu vol genoeg zijn.
    boiler = {
        "name": "Boiler", "entity": "input_boolean.boiler", "power_w": 2000,
        "power_sensor": "sensor.boilervermogen", "temp_sensor": "sensor.boiler_temperatuur",
        "max_temp": 60, "min_soc_pct": 95, "start_hour": 0, "end_hour": 24, "need_forecast": False,
        "order": 2, "on_delay_min": 0, "off_delay_min": 0, "min_on_min": 1, "min_off_min": 0,
        "rest_min": 0}
    options(entry_id, "schakelaar_toevoegen", boiler)
    meter.set(-6000)   # accu laadt 3000 W, 3000 W gaat naar het net
    time.sleep(50)
    show("6000 W overschot, accu net begonnen")
    expect(state("input_boolean.boiler")["state"] == "off", "boiler: blijft uit zolang de accu niet vol genoeg is")
    expect("95%" in (flex("Boiler").get("toelichting") or ""), "boiler: de reden noemt de minimale lading")

    options(entry_id, "buffer_kiezen", {**boiler, "min_soc_pct": 0}, pick="Boiler")
    meter.set(-6000)
    time.sleep(55)
    show("minimale lading 0%, alleen advies")
    expect(state("binary_sensor.huis_ems_boiler_vrijgegeven")["state"] == "on",
           "boiler: advies 'vrijgegeven' bij genoeg overschot")
    expect(state("input_boolean.boiler")["state"] == "off", "boiler: bij alleen advies schakelt het EMS niet")

    call("/api/services/switch/turn_on", {"entity_id": "switch.huis_ems_verbruik_verschuiven"})
    time.sleep(55)
    show("schakelen aan")
    expect(state("input_boolean.boiler")["state"] == "on", "boiler: met schakelen aan gaat het element aan")
    # Het element neemt nu zelf 2000 W; daarna een wolk: de accu zou moeten bijspringen.
    call("/api/services/input_number/set_value", {"entity_id": "input_number.boiler_w", "value": 2000})
    meter.set(-4000)
    time.sleep(30)
    expect(state("input_boolean.boiler")["state"] == "on", "boiler: blijft aan zolang de zon hem dekt")
    meter.set(500)
    time.sleep(100)
    show("wolk: de accu zou de boiler voeden")
    expect(state("input_boolean.boiler")["state"] == "off",
           "boiler: gaat uit als de accu hem zou moeten voeden")
    call("/api/services/input_number/set_value", {"entity_id": "input_number.boiler_w", "value": 0})

    # Vat al warm: niet aanzetten.
    call("/api/services/input_number/set_value", {"entity_id": "input_number.boiler_temp", "value": 61})
    meter.set(-6000)
    time.sleep(55)
    show("vat 61 °C")
    expect(state("input_boolean.boiler")["state"] == "off", "boiler: niet aan boven de maximale temperatuur")
    expect("60" in (flex("Boiler").get("toelichting") or ""), "boiler: de reden noemt de temperatuurgrens")
    call("/api/services/input_number/set_value", {"entity_id": "input_number.boiler_temp", "value": 48})

    # Thermostaat: gaat voor de boiler en zet de woonkamer een graad hoger.
    options(entry_id, "thermostaat_toevoegen", {
        "name": "Vloer", "entity": "climate.woonkamer", "boost_c": 1, "temp_sensor": "sensor.kamertemperatuur",
        "max_temp": 22, "power_w": 1000, "min_soc_pct": 0, "start_hour": 0, "end_hour": 24,
        "need_forecast": False, "order": 1, "on_delay_min": 0, "off_delay_min": 0, "min_on_min": 1,
        "min_off_min": 0, "rest_min": 0})
    meter.set(-6000)
    time.sleep(55)
    show("thermostaat erbij, 3000 W over na de accu")
    target = state("climate.woonkamer")["attributes"].get("temperature")
    expect(target == 21, f"thermostaat: bij overschot een graad hoger (nu {target})")
    expect("normaal 20" in (flex("Vloer").get("stand") or ""), "thermostaat: onthoudt de normale temperatuur")

    # Ruimte te warm: meteen terug, en weer omhoog als hij is afgekoeld.
    call("/api/services/input_number/set_value", {"entity_id": "input_number.kamer", "value": 22.2})
    time.sleep(20)
    target = state("climate.woonkamer")["attributes"].get("temperature")
    expect(target == 20, f"thermostaat: terug naar normaal als de kamer te warm wordt (nu {target})")
    call("/api/services/input_number/set_value", {"entity_id": "input_number.kamer", "value": 20.5})
    time.sleep(40)
    expect(state("climate.woonkamer")["attributes"].get("temperature") == 21, "thermostaat: weer verhoogd")

    # Iemand zet de thermostaat zelf anders: het EMS laat hem zo en wacht een tijd.
    call("/api/services/climate/set_temperature", {"entity_id": "climate.woonkamer", "temperature": 19})
    time.sleep(30)
    show("thermostaat met de hand op 19 °C")
    target = state("climate.woonkamer")["attributes"].get("temperature")
    expect(target == 19, f"thermostaat: een handmatige wijziging blijft staan (nu {target})")
    expect("met de hand" in (flex("Vloer").get("toelichting") or ""), "thermostaat: de reden noemt de handmatige wijziging")
    # Na herladen begint het EMS opnieuw, vanaf de stand van dat moment.
    call("/api/services/climate/set_temperature", {"entity_id": "climate.woonkamer", "temperature": 20})
    call(f"/api/config/config_entries/entry/{entry_id}/reload", {})
    time.sleep(40)
    target = state("climate.woonkamer")["attributes"].get("temperature")
    expect(target == 21, f"thermostaat: na herladen weer verhoogd vanaf 20 °C (nu {target})")

    # Schakelen uit: alles terug naar normaal.
    call("/api/services/switch/turn_off", {"entity_id": "switch.huis_ems_verbruik_verschuiven"})
    time.sleep(10)
    target = state("climate.woonkamer")["attributes"].get("temperature")
    expect(target == 20, f"schakelen uit: thermostaat terug naar normaal (nu {target})")

    # Herladen terwijl de thermostaat verhoogd is: eerst terugzetten.
    call("/api/services/switch/turn_on", {"entity_id": "switch.huis_ems_verbruik_verschuiven"})
    time.sleep(45)
    expect(state("climate.woonkamer")["attributes"].get("temperature") == 21, "herladen: thermostaat verhoogd")
    meter.set(2000)   # na het herladen geen overschot, zodat hij niet meteen weer omhoog gaat
    call(f"/api/config/config_entries/entry/{entry_id}/reload", {})
    time.sleep(10)
    target = state("climate.woonkamer")["attributes"].get("temperature")
    expect(target == 20, f"herladen: het EMS zet de thermostaat eerst terug (nu {target})")

    # Meter valt weg: geen overschot meer bekend.
    meter.stop()
    time.sleep(45)
    show("P1-meter onbereikbaar")
    expect(state("binary_sensor.huis_ems_meterstoring")["state"] == "on",
           "meteruitval: na 20 seconden zonder meting gaat de meterstoring aan")
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
            "hp_energy": ["sensor.warmtepomp_energie"], "outdoor_temp": "sensor.buitentemperatuur"})
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
