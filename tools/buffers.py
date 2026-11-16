"""Wat leveren de warmtebuffers op? Vloer (thermostaat hoger) en boiler (element) op het meetjaar.

Gebruik:
  python tools/buffers.py --p1 data/p1.csv --hp data/wp_jaar.csv --prices data/prijzen.csv

Per kwartier, in volgorde:
- de accu (8 kWh / 3 kW) regelt op zelfverbruik, zoals in het echt;
- de buffers besluiten op wat het EMS dan ook ziet: de lading van de accu en het overschot
  van het vorige kwartier (meting, geen voorkennis);
- de boiler: element 2 kW zolang er na de accu genoeg over is. Het vat kan een beperkte
  hoeveelheid extra warmte bevatten; die wordt over de dag verbruikt en bespaart dan
  warmtepompstroom voor tapwater (warmte / COP);
- de vloer: tussen 10 en 16 uur draait de warmtepomp door op overschot. De extra warmte zit
  in vloer en huis en vervangt later stookwarmte; een deel lekt weg doordat het binnen warmer is.

De besparing is het verschil in netkosten met en zonder buffers, met dezelfde accu. Het rekent
op kwartiergemiddelden: wolken binnen een kwartier zijn niet te zien, dus de uitkomst is aan
de gunstige kant voor de boiler. Aannames staan bovenaan en zijn met vlaggen te variëren.
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import simulate as sim  # noqa: E402
from simulate import nl_offset  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "huis_ems"))
from core.tariff import DynamicTariff, FixedTariff  # noqa: E402

Q = 0.25  # uur per kwartier


def run(times, net, hp, prices, args, boiler=False, floor=False):
    cap, kw, eta = args.capacity, args.power, 0.93
    lo, hi = cap * 0.10, cap
    soc = lo
    stored_dhw = 0.0      # extra warmte in het vat (kWh warmte)
    stored_floor = 0.0    # extra warmte in vloer en huis (kWh warmte)
    element_on = floor_on = False
    prev_grid = 0.0       # netvermogen vorige kwartier (kW, + = afname)
    prev_battery = 0.0    # accuvermogen vorige kwartier (kW, + = laden)
    prev_counted = 0.0    # wat de buffers vorige kwartier zelf gebruikten (kW)
    cost = 0.0
    stats = defaultdict(float)
    monthly = defaultdict(float)
    hp_list = [hp.get(t, 0.0) for t in times]
    # Warmtevraag de komende 24 uur (de verbruiksvoorspelling weet dat ongeveer): alleen dan zin in de vloer.
    ahead = [0.0] * len(times)
    run_sum = sum(hp_list[:96])
    for i in range(len(times)):
        ahead[i] = run_sum
        run_sum += (hp_list[i + 96] if i + 96 < len(times) else 0.0) - hp_list[i]
    for i, t in enumerate(times):
        local = t + nl_offset(t)
        hour = local.hour + local.minute / 60
        buy, sell = prices[t]
        load_net = net[t]               # kWh, + = afname (zonder accu, met huidig gedrag)
        extra = 0.0
        pct = soc / cap * 100 if cap else 100.0   # zonder accu: geen voorwaarde op de lading

        # --- beslissen op de meting van het vorige kwartier ---
        # Zoals in het EMS: teruglevering, min wat de accu ontlaadt, plus wat de buffers zelf gebruiken.
        spare = -prev_grid + min(0.0, prev_battery) + prev_counted
        counted = 0.0
        if floor:
            room = stored_floor < args.floor_store
            want = (args.floor_start <= hour < args.floor_end and pct >= args.floor_soc and room
                    and ahead[i] >= args.floor_min_demand)
            keep = floor_on and spare >= args.hp_kw * 0.9
            start = not floor_on and spare >= args.hp_kw + 0.05
            floor_on = want and (keep or start)
            if floor_on:
                counted += args.hp_kw
                # De warmtepomp draait op vol vermogen; wat hij toch al deed telt niet als extra.
                more = max(0.0, args.hp_kw * Q - hp_list[i])
                extra += more
                stored_floor += more * args.cop_day
                stats["vloer_kwh"] += more
                stats["vloer_kwartieren"] += 1
        if boiler:
            room = stored_dhw < args.tank_kwh - args.element_kw * Q * 0.5
            used_by_floor = args.hp_kw if floor_on else 0.0
            keep = element_on and spare - used_by_floor >= args.element_kw * 0.9
            start = not element_on and spare - used_by_floor >= args.element_kw + 0.05
            element_on = pct >= args.boiler_soc and room and (keep or start)
            if element_on:
                kwh = min(args.element_kw * Q, args.tank_kwh - stored_dhw)
                counted += kwh / Q
                extra += kwh
                stored_dhw += kwh
                stats["boiler_kwh"] += kwh

        # --- opgeslagen warmte wordt gebruikt: minder warmtepompstroom ---
        saved = 0.0
        if stored_dhw > 0:
            use = min(stored_dhw, args.dhw_per_day / 96)
            stored_dhw -= use
            # Een warmer vat verliest meer; dat deel bespaart niets.
            stored_dhw -= min(stored_dhw, args.tank_loss_per_day / 96 * stored_dhw / args.tank_kwh)
            saved += use / args.cop_dhw
            stats["tapwater_bespaard_kwh"] += use / args.cop_dhw
        if stored_floor > 0:
            # Extra warmteverlies doordat het binnen warmer is (RC-model).
            loss = args.house_h * (stored_floor / args.house_c) * Q
            stored_floor = max(0.0, stored_floor - loss)
            stats["vloer_verlies_warmte"] += loss
            # Buiten de boost vervangt de opgeslagen warmte stookwarmte van de warmtepomp.
            if not floor_on and hp_list[i] > 0:
                heat_needed = hp_list[i] * args.cop_later
                take = min(stored_floor, heat_needed)
                stored_floor -= take
                saved += take / args.cop_later
                stats["stook_bespaard_kwh"] += take / args.cop_later

        # --- de accu op zelfverbruik, op het nieuwe saldo ---
        flow = load_net + extra - saved
        if flow < 0:
            ac = min(-flow, kw * Q, (hi - soc) / eta)
            soc += ac * eta
        else:
            ac = -min(flow, kw * Q, (soc - lo) * eta)
            soc += ac / eta
        grid = flow + ac
        euro = grid * buy if grid > 0 else grid * sell
        cost += euro
        monthly[local.strftime("%m")] += euro
        stats["afname"] += max(grid, 0.0)
        stats["terug"] += max(-grid, 0.0)
        # Wat het EMS het volgende kwartier ziet: overschot zonder de buffers zelf.
        prev_grid, prev_battery, prev_counted = grid / Q, ac / Q, counted
    return cost, stats, monthly


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--p1", default="data/p1.csv")
    ap.add_argument("--hp", default="data/wp_jaar.csv")
    ap.add_argument("--prices", default="data/prijzen.csv")
    ap.add_argument("--capacity", type=float, default=8.0)
    ap.add_argument("--power", type=float, default=3.0)
    ap.add_argument("--element-kw", dest="element_kw", type=float, default=2.0)
    ap.add_argument("--tank-kwh", dest="tank_kwh", type=float, default=5.0,
                    help="extra warmte die in het vat past (300 l van 50 naar 65 °C)")
    ap.add_argument("--dhw-per-day", dest="dhw_per_day", type=float, default=2.5,
                    help="tapwaterwarmte per dag (kWh)")
    ap.add_argument("--tank-loss-per-day", dest="tank_loss_per_day", type=float, default=0.3)
    ap.add_argument("--cop-dhw", dest="cop_dhw", type=float, default=2.5)
    ap.add_argument("--hp-kw", dest="hp_kw", type=float, default=1.5)
    ap.add_argument("--cop-day", dest="cop_day", type=float, default=3.5)
    ap.add_argument("--cop-later", dest="cop_later", type=float, default=3.2)
    ap.add_argument("--floor-store", dest="floor_store", type=float, default=8.0,
                    help="extra warmte in vloer en huis bij +1 °C (kWh)")
    ap.add_argument("--house-h", dest="house_h", type=float, default=0.35, help="warmteverlies (kW per K)")
    ap.add_argument("--house-c", dest="house_c", type=float, default=10.0, help="warmtecapaciteit (kWh per K)")
    ap.add_argument("--floor-min-demand", dest="floor_min_demand", type=float, default=2.0,
                    help="alleen opstoken als de warmtepomp de komende 24 uur zoveel kWh nodig heeft")
    ap.add_argument("--floor-start", dest="floor_start", type=float, default=10)
    ap.add_argument("--floor-end", dest="floor_end", type=float, default=16)
    ap.add_argument("--floor-soc", dest="floor_soc", type=float, default=90)
    ap.add_argument("--boiler-soc", dest="boiler_soc", type=float, default=95)
    args = ap.parse_args()

    net = sim.read_p1(args.p1)
    hp = sim.read_p1(args.hp)
    spot = sim.read_prices(args.prices)
    times = sorted(t for t in net if t in spot and t in hp)
    print(f"{len(times) / 96:.0f} dagen met P1, warmtepomp en prijzen "
          f"({times[0]:%d-%m-%Y} tot {times[-1]:%d-%m-%Y})")
    for label, tariff in (("dynamisch", DynamicTariff(0.0916, 0.02, 0.0)), ("vast 27/5 ct", FixedTariff(0.27, 0.05))):
        prices = {t: (tariff.buy(spot[t]), tariff.sell(spot[t])) for t in times}
        base, bs, bm = run(times, net, hp, prices, args)
        print(f"\n{label}: accu {args.capacity:g} kWh / {args.power:g} kW zonder buffers: € {base:,.0f} per jaar, "
              f"{bs['terug']:,.0f} kWh terug")
        for name, kw in (("boiler", dict(boiler=True)), ("vloer", dict(floor=True)),
                         ("vloer en boiler", dict(boiler=True, floor=True))):
            cost, st, mo = run(times, net, hp, prices, args, **kw)
            line = f"  + {name:<16} € {base - cost:>5.0f} per jaar, {bs['terug'] - st['terug']:>5.0f} kWh minder terug"
            if st["boiler_kwh"]:
                line += f" | element {st['boiler_kwh']:.0f} kWh, bespaart {st['tapwater_bespaard_kwh']:.0f} kWh wp"
            if st["vloer_kwh"]:
                line += (f" | wp extra {st['vloer_kwh']:.0f} kWh in {st['vloer_kwartieren'] / 4:.0f} uur, "
                         f"later {st['stook_bespaard_kwh']:.0f} kWh minder")
            print(line)
            if name == "vloer en boiler":
                months = sorted(mo)
                print("    per maand (€): " + " ".join(f"{m}:{bm[m] - mo[m]:.0f}" for m in months))


if __name__ == "__main__":
    main()
