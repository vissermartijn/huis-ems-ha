"""Jaarsimulatie: wat doet een thuisaccu met jouw P1-data?

Gebruik:
  python tools/simulate.py --p1 data/p1.csv --prices data/prijzen.csv
  python tools/simulate.py --p1 data/p1.csv --prices data/prijzen.csv --capacity 7 --power 3.5

De P1-export (HomeWizard Energy, kwartier- of uurwaarden) bevat alleen de
netto-uitwisseling met het net. Dat is genoeg: de accu ziet alleen het saldo
van verbruik en zon.

Twee manieren om de planner te beoordelen:

- met voorkennis: de planner plant elke dag om 13:00 met de dan bekende prijzen
  en kent verbruik en zon exact. Dat is een bovengrens.
- herhaling (--replay): de planner plant elk uur opnieuw en ziet alleen wat hij
  op dat moment had kunnen weten: de prijzen die al gepubliceerd waren (om 13:00
  die van morgen) en een voorspelling van het saldo van verbruik en zon uit het
  verleden. Daarna wordt zijn opdracht uitgevoerd tegen wat er werkelijk
  gebeurde, zoals in Home Assistant: bij zelfverbruik volgt de accu de meter.
  Dat geeft een ondergrens; een echte zon- en verbruiksvoorspelling zit ertussen.

Alle varianten worden op dezelfde manier afgerekend, met dezelfde beginlading,
en de lading die aan het eind overblijft wordt verrekend tegen de mediane
inkoopprijs maal het ontlaadrendement.
"""
import argparse
import csv
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "custom_components", "huis_ems"))
from core.optimizer import (  # noqa: E402
    LADEN, ONTLADEN, ZELFVERBRUIK, Battery, Slot, decide_mode, optimize, refine_modes,
    self_consumption)
from core.tariff import DynamicTariff, FixedTariff  # noqa: E402

UTC = timezone.utc


def _last_sunday(year, month):
    d = datetime(year, month, 31, 1, tzinfo=UTC)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def nl_offset(utc_dt):
    """UTC-offset voor Nederland, zonder tzdata (ontbreekt vaak op Windows)."""
    summer = _last_sunday(utc_dt.year, 3) <= utc_dt < _last_sunday(utc_dt.year, 10)
    return timedelta(hours=2 if summer else 1)


def local_to_utc(naive, prev_utc=None):
    guess = (naive - timedelta(hours=1)).replace(tzinfo=UTC)
    utc = naive.replace(tzinfo=UTC) - nl_offset(guess)
    if prev_utc is not None and utc <= prev_utc:  # het dubbele uur in oktober
        utc += timedelta(hours=1)
    return utc


def _num(text):
    text = (text or "").strip().replace(",", ".")
    return float(text) if text else None


def read_p1(path):
    """Leest een P1-export en geeft {utc-kwartier: netto kWh (+ = afname)}."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delim = ";" if sample.count(";") > sample.count(",") else ","
        rows = list(csv.reader(fh, delimiter=delim))
    header = [h.strip().lower() for h in rows[0]]
    imp = [i for i, h in enumerate(header) if any(k in h for k in ("import", "afname", "verbruik", "levering"))
           and "terug" not in h and "gas" not in h and "kwh" in h]
    exp = [i for i, h in enumerate(header) if any(k in h for k in ("export", "terug", "teruglevering"))
           and "kwh" in h]
    if not imp:
        raise SystemExit(f"Geen afnamekolom gevonden in: {rows[0]}")
    formats = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M", "%Y-%m-%dT%H:%M")
    points, prev = [], None
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        stamp = row[0].strip()[:19]
        when = None
        for fmt in formats:
            try:
                when = datetime.strptime(stamp, fmt)
                break
            except ValueError:
                continue
        if when is None:
            continue
        i_val = [_num(row[i]) for i in imp if i < len(row)]
        e_val = [_num(row[i]) for i in exp if i < len(row)]
        if any(v is None for v in i_val + e_val):
            continue
        prev = local_to_utc(when, prev)
        points.append((prev, sum(i_val), sum(e_val)))
    if len(points) < 3:
        raise SystemExit("Te weinig bruikbare regels in de P1-export.")
    # Oplopende meterstanden of verbruik per interval?
    cumulative = all(b[1] >= a[1] - 1e-6 for a, b in zip(points, points[1:])) and points[-1][1] > 50
    step = min((b[0] - a[0]) for a, b in zip(points, points[1:]))
    parts = max(1, int(step.total_seconds() // 900))
    out = {}
    if cumulative:
        for a, b in zip(points, points[1:]):
            n = max(1, int((b[0] - a[0]).total_seconds() // 900))
            net = ((b[1] - a[1]) - (b[2] - a[2])) / n
            for k in range(n):
                out[a[0] + timedelta(minutes=15 * k)] = net
    else:
        for t, i, e in points:
            for k in range(parts):
                out[t + timedelta(minutes=15 * k)] = (i - e) / parts
    return out


def read_prices(path):
    series = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            series.append((datetime.fromisoformat(row["utc"].replace("Z", "+00:00")),
                           float(row["eur_per_kwh"])))
    series.sort()
    out = {}
    for (t, p), nxt in zip(series, series[1:] + [(series[-1][0] + timedelta(hours=1), 0)]):
        n = max(1, min(4, int((nxt[0] - t).total_seconds() // 900)))
        for k in range(n):
            out[t + timedelta(minutes=15 * k)] = p
    return out


def build_slots(times, net, spot, tariff):
    return [Slot(0.25, max(net[t], 0.0), max(-net[t], 0.0), tariff.buy(spot[t]), tariff.sell(spot[t]))
            for t in times]


def evaluate(name, acs, slots, bat, end_soc, expensive):
    """Rekent een reeks accuvermogens (kWh per kwartier, + = laden) af tegen de werkelijkheid.

    Gelijk voor alle varianten: netkosten, slijtage op wat uit de cellen komt, en de
    eindlading verrekend tegen de waarde die de planner er zelf aan toekent.
    """
    imp = exp = cost = out = dear = 0.0
    for ac, slot, hot in zip(acs, slots, expensive):
        grid = slot.load_kwh - slot.pv_kwh + ac
        cost += grid * slot.buy if grid > 0 else grid * slot.sell
        if ac < 0:
            out += -ac
            cost += bat.wear_eur_per_kwh * -ac / max(bat.eta_discharge, 1e-6)
        if grid > 0:
            imp += grid
            dear += grid if hot else 0.0
        else:
            exp -= grid
    usable = bat.max_soc_kwh - bat.min_soc_kwh
    rest = max(0.0, end_soc - bat.min_soc_kwh)
    value = sorted(s.buy for s in slots)[len(slots) // 2] * bat.eta_discharge
    return {"naam": name, "afname": imp, "terug": exp, "kosten": cost - rest * value,
            "cycli": out / bat.eta_discharge / usable if usable > 0 else 0.0,
            "doorzet": out, "duur": dear, "rest": rest}


def from_steps(steps):
    return [s.accu_ac_kwh for s in steps], (steps[-1].soc_kwh if steps else 0.0)


def execute(mode, power_w, slot, soc, bat, allow_grid, allow_export):
    """Voert één kwartier uit tegen het werkelijke verbruik en de werkelijke zon.

    Zoals in Home Assistant: bij zelfverbruik volgt de accu de meter, bij laden
    en ontladen houdt hij een vast vermogen aan binnen zijn grenzen.
    """
    h = slot.hours
    net = slot.load_kwh - slot.pv_kwh
    max_c = max(0.0, min(bat.max_charge_kw * h, (bat.max_soc_kwh - soc) / bat.eta_charge))
    max_d = max(0.0, min(bat.max_discharge_kw * h, (soc - bat.min_soc_kwh) * bat.eta_discharge))
    if mode == ZELFVERBRUIK:
        ac = min(max_c, max(-max_d, -net))
    elif mode == LADEN:
        ac = min(max_c, power_w / 1000 * h)
        if not allow_grid:
            ac = min(ac, max(0.0, -net))
    elif mode == ONTLADEN:
        ac = -min(max_d, power_w / 1000 * h)
        if not allow_export:
            ac = max(ac, -max(0.0, net))
    else:
        ac = 0.0
    soc += ac * bat.eta_charge if ac > 0 else ac / bat.eta_discharge
    return ac, soc


def known_until(utc):
    """Tot wanneer de dag-vooruitprijzen op dit moment gepubliceerd zijn (om 13:00 die van morgen)."""
    local = utc + nl_offset(utc)
    days = 2 if local.hour >= 13 else 1
    end_local = local.replace(hour=0, minute=0) + timedelta(days=days)
    return end_local - nl_offset(end_local - timedelta(hours=2))


def run_replay(times, slots, bat, step_kwh, allow_grid, allow_export, forecast="drie-dagen",
               margin=0.0, every=4):
    """Plant elk uur opnieuw met alleen de kennis van dat moment en voert uit tegen de werkelijkheid."""
    index = {t: i for i, t in enumerate(times)}
    day = timedelta(days=1)

    def predicted(i, now_i):
        """Saldo voor kwartier i, voorspeld op moment now_i uit wat toen al gemeten was."""
        t, now = times[i], times[now_i]
        k = 1
        while t - k * day >= now:
            k += 1
        days = 1 if forecast == "gisteren" else 3
        seen = [slots[index[t - (k + n) * day]] for n in range(days) if t - (k + n) * day in index]
        if not seen:
            return slots[i].load_kwh, slots[i].pv_kwh   # eerste dag: geen historie
        net = sum(s.load_kwh - s.pv_kwh for s in seen) / len(seen)
        return max(net, 0.0), max(-net, 0.0)

    acs, soc, i = [], bat.min_soc_kwh, 0
    while i < len(times):
        end = known_until(times[i])
        k = i
        while k < len(times) and times[k] < end:
            k += 1
        view = []
        for j in range(i, k):
            load, pv = predicted(j, i)
            view.append(Slot(0.25, load, pv, slots[j].buy, slots[j].sell))
        plan, _ = optimize(view, bat, soc, step_kwh=step_kwh, allow_grid_charge=allow_grid,
                           allow_export=allow_export, grid_charge_margin=margin)
        modes = refine_modes(plan, view, [decide_mode(st, sl) for st, sl in zip(plan, view)],
                             allow_grid, allow_export)
        for j in range(i, min(i + every, k)):
            mode, power = modes[j - i]
            ac, soc = execute(mode, power, slots[j], soc, bat, allow_grid, allow_export)
            acs.append(ac)
        i = min(i + every, k)
    return acs, soc


def by_day(times, slots, acs_a, acs_b, bat):
    """Verschil tussen twee varianten per soort dag: veel/weinig zon, grote/kleine prijsverschillen."""
    days = {}
    for t, slot, a, b in zip(times, slots, acs_a, acs_b):
        key = (t + nl_offset(t)).date()
        d = days.setdefault(key, {"terug": 0.0, "prijzen": [], "a": 0.0, "b": 0.0})
        net = slot.load_kwh - slot.pv_kwh
        d["terug"] += max(0.0, -net)
        d["prijzen"].append(slot.buy)
        for name, ac in (("a", a), ("b", b)):
            grid = net + ac
            d[name] += (grid * slot.buy if grid > 0 else grid * slot.sell) \
                + (bat.wear_eur_per_kwh * -ac / bat.eta_discharge if ac < 0 else 0.0)
    rows = list(days.values())
    out = []
    for label, key in (("zonoverschot", lambda d: d["terug"]),
                       ("prijsverschil", lambda d: max(d["prijzen"]) - min(d["prijzen"]))):
        ranked = sorted(rows, key=key)
        third = max(1, len(ranked) // 3)
        for name, part in (("laag", ranked[:third]), ("hoog", ranked[-third:])):
            gain = sum(d["a"] - d["b"] for d in part) / len(part)
            out.append((f"{label} {name}", len(part), gain))
    return out


def run_optimized(times, slots, bat, step_kwh, **kw):
    """Plant elke dag om 13:00 lokale tijd tot het eind van de volgende dag."""
    steps, soc, i = [], bat.min_soc_kwh, 0
    while i < len(times):
        local = times[i] + nl_offset(times[i])
        next_plan = (local.replace(hour=13, minute=0) + timedelta(days=1 if local.hour >= 13 else 0))
        horizon_end = (next_plan + timedelta(days=1)).replace(hour=0)
        j = i
        while j < len(times) and times[j] + nl_offset(times[j]) < next_plan:
            j += 1
        k = j
        while k < len(times) and times[k] + nl_offset(times[k]) < horizon_end:
            k += 1
        plan, _ = optimize(slots[i:k], bat, soc, step_kwh=step_kwh, **kw)
        steps.extend(plan[:j - i])
        soc = steps[-1].soc_kwh
        i = j
    return steps


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--p1", required=True)
    ap.add_argument("--prices", required=True)
    ap.add_argument("--capacity", type=float, default=8.0, help="kWh (Zendure 8, Anker 7)")
    ap.add_argument("--power", type=float, default=3.0, help="kW laden/ontladen")
    ap.add_argument("--min-soc", type=float, default=10, help="procent")
    ap.add_argument("--efficiency", type=float, default=0.93, help="per richting")
    ap.add_argument("--wear", type=float, default=0.02, help="EUR per kWh uit de accu")
    ap.add_argument("--step", type=float, default=0.1, help="rekenstap in kWh")
    ap.add_argument("--fixed-buy", type=float, default=0.27)
    ap.add_argument("--fixed-sell", type=float, default=0.05)
    ap.add_argument("--energy-tax", type=float, default=0.0916)
    ap.add_argument("--buy-fee", type=float, default=0.02)
    ap.add_argument("--sell-fee", type=float, default=0.0)
    ap.add_argument("--margin", type=float, default=0.03, help="drempel bijladen, EUR/kWh")
    ap.add_argument("--replay", action="store_true",
                    help="ook de herhaling zonder voorkennis draaien (duurt enkele minuten)")
    ap.add_argument("--forecast", choices=["gisteren", "drie-dagen"], default="drie-dagen",
                    help="voorspelling in de herhaling: saldo van gisteren of gemiddelde van drie dagen")
    args = ap.parse_args()

    net, spot = read_p1(args.p1), read_prices(args.prices)
    times = sorted(t for t in net if t in spot)
    if not times:
        raise SystemExit("P1-data en prijzen overlappen niet in de tijd.")
    missing = len(net) - len(times)
    bat = Battery(args.capacity * args.min_soc / 100, args.capacity, args.power, args.power,
                  args.efficiency, args.efficiency, args.wear)
    none = Battery(0, 0, 0, 0, 1, 1, 0)
    fixed = FixedTariff(args.fixed_buy, args.fixed_sell)
    dyn = DynamicTariff(args.energy_tax, args.buy_fee, args.sell_fee)
    s_fixed, s_dyn = build_slots(times, net, spot, fixed), build_slots(times, net, spot, dyn)

    # Dure kwartieren: het duurste vijfde deel van het jaar.
    ranked = sorted(s.buy for s in s_dyn)
    dear = ranked[int(len(ranked) * 0.8)]
    expensive = [s.buy >= dear for s in s_dyn]
    zero = [0.0] * len(times)

    def ev(name, slots, acs_soc):
        return evaluate(name, acs_soc[0], slots, bat if acs_soc[0] is not zero else none,
                        acs_soc[1], expensive)

    own = from_steps(self_consumption(s_dyn, bat, bat.min_soc_kwh)[0])
    smart = from_steps(run_optimized(times, s_dyn, bat, args.step, allow_grid_charge=False,
                                     allow_export=False))
    results = [
        ev("Vast, zonder accu", s_fixed, (zero, 0.0)),
        ev("Vast, accu zelfverbruik", s_fixed, from_steps(self_consumption(s_fixed, bat, bat.min_soc_kwh)[0])),
        ev("Dynamisch, zonder accu", s_dyn, (zero, 0.0)),
        ev("Dynamisch, accu zelfverbruik", s_dyn, own),
        ev("Dyn., eigen stroom slim (voorkennis)", s_dyn, smart),
        ev("Dyn., + bijladen (voorkennis)", s_dyn, from_steps(run_optimized(
            times, s_dyn, bat, args.step, allow_grid_charge=True, allow_export=False,
            grid_charge_margin=args.margin))),
        ev("Dyn., met handelen (voorkennis)", s_dyn, from_steps(run_optimized(times, s_dyn, bat, args.step))),
    ]
    replay = None
    if args.replay:
        replay = run_replay(times, s_dyn, bat, args.step, False, False, args.forecast)
        results.append(ev(f"Dyn., eigen stroom slim (herhaling, {args.forecast})", s_dyn, replay))
        results.append(ev(f"Dyn., + bijladen (herhaling, {args.forecast})", s_dyn, run_replay(
            times, s_dyn, bat, args.step, True, False, args.forecast, args.margin)))

    days = len(times) / 96
    print(f"Periode: {times[0]:%Y-%m-%d} t/m {times[-1]:%Y-%m-%d} ({days:.0f} dagen"
          + (f", {missing} kwartieren zonder prijs overgeslagen" if missing else "") + ")")
    print(f"Accu: {args.capacity} kWh, {args.power} kW, rendement {args.efficiency ** 2:.0%} heen en terug\n")
    without = next(r for r in results if r["naam"] == "Dynamisch, zonder accu")
    print(f"{'Scenario':<46}{'Afname':>9}{'Terug':>9}{'Kosten':>10}{'Cycli':>7}{'Duur':>8}{'Overschot':>11}{'Rest':>6}")
    for r in results:
        used = 1 - r["terug"] / without["terug"] if without["terug"] else 0.0
        print(f"{r['naam']:<46}{r['afname']:>7.0f}kWh{r['terug']:>6.0f}kWh{r['kosten']:>8.0f} EUR"
              f"{r['cycli']:>7.0f}{r['duur']:>6.0f}kWh{used:>10.0%}{r['rest']:>5.1f}")
    print("\nDuur: afname in het duurste vijfde deel van de kwartieren. Overschot: deel van het\n"
          "zonoverschot dat niet naar het net ging. Rest: lading aan het eind (kWh), verrekend in de kosten.\n"
          "Kosten zijn alleen variabele stroomkosten incl. accuslijtage; vastrecht, netbeheer en\n"
          "de vermindering energiebelasting zijn in alle scenario's gelijk en dus weggelaten.")
    if replay:
        print("\nWinst van de planner op gewoon zelfverbruik, gemiddeld per dag (EUR):")
        print(f"{'Soort dag':<22}{'Dagen':>6}{'Voorkennis':>12}{'Herhaling':>11}")
        perfect = by_day(times, s_dyn, own[0], smart[0], bat)
        honest = by_day(times, s_dyn, own[0], replay[0], bat)
        for (label, n, g1), (_, _, g2) in zip(perfect, honest):
            print(f"{label:<22}{n:>6}{g1:>12.3f}{g2:>11.3f}")

if __name__ == "__main__":
    main()
