"""Accu-optimalisatie met dynamisch programmeren over de laadtoestand.

Geen externe afhankelijkheden, zodat dit zowel in AppDaemon als in de
simulator draait. Alle energie in kWh, vermogens in kW, prijzen in EUR/kWh.
Teken: accu_ac_kwh > 0 is laden, < 0 is ontladen (gemeten aan de AC-kant).
"""
from dataclasses import dataclass
from statistics import median


@dataclass
class Battery:
    min_soc_kwh: float = 0.8
    max_soc_kwh: float = 8.0
    max_charge_kw: float = 3.0
    max_discharge_kw: float = 3.0
    eta_charge: float = 0.93
    eta_discharge: float = 0.93
    wear_eur_per_kwh: float = 0.02  # per kWh die uit de cellen gehaald wordt


@dataclass
class Slot:
    hours: float
    load_kwh: float   # huisverbruik zonder accu
    pv_kwh: float     # zonopwek
    buy: float        # all-in inkoopprijs
    sell: float       # terugleverprijs


@dataclass
class Step:
    soc_kwh: float        # laadtoestand aan het einde van het slot
    accu_ac_kwh: float
    grid_kwh: float       # > 0 afname, < 0 teruglevering
    cost: float           # netkosten + slijtage in dit slot
    at_limit: int         # -1 = accu leeg na dit slot, +1 = vol, 0 = ertussen


def optimize(slots, bat, soc_start_kwh, step_kwh=0.05,
             allow_grid_charge=True, allow_export=True, terminal_value=None,
             grid_charge_margin=0.0, require_full=False):
    """Geeft (stappen, totale kosten) voor het goedkoopste accuschema.

    allow_grid_charge=False: alleen laden uit eigen overschot.
    allow_export=False: alleen ontladen voor eigen verbruik.
    terminal_value: waarde (EUR/kWh) van energie die aan het eind nog in de
    accu zit; standaard de mediane inkoopprijs maal het ontlaadrendement.
    grid_charge_margin: drempel voor bijladen uit het net, in EUR per kWh die
    van het net de accu in gaat. Het omzetverlies (laden en ontladen) zit al
    in de berekening; de drempel is de winst die er daarna minimaal moet
    overblijven voordat bijladen de moeite waard is. Telt niet mee in de
    gerapporteerde kosten.
    require_full: de accu moet binnen deze periode minstens één keer helemaal
    vol raken (voor het balanceren van de cellen). De planner kiest zelf het
    goedkoopste moment. Geeft (None, None) als dat binnen de grenzen niet kan.
    """
    if not slots:
        return [], 0.0
    n = max(0, int(round((bat.max_soc_kwh - bat.min_soc_kwh) / step_kwh)))
    if terminal_value is None:
        terminal_value = median(s.buy for s in slots) * bat.eta_discharge
    s0 = min(n, max(0, int(round((soc_start_kwh - bat.min_soc_kwh) / step_kwh))))

    inf = float("inf")
    # Twee lagen: "nog niet vol geweest" (0) en "al vol geweest" (1). Zonder de
    # eis bestaat alleen laag 1.
    done = [-terminal_value * i * step_kwh for i in range(n + 1)]
    todo = [inf] * (n + 1) if require_full else None
    value = done
    choices, choices_todo = [], []
    for slot in reversed(slots):
        net = slot.load_kwh - slot.pv_kwh
        up = int(bat.max_charge_kw * slot.hours * bat.eta_charge / step_kwh + 1e-9)
        down = int(bat.max_discharge_kw * slot.hours / bat.eta_discharge / step_kwh + 1e-9)
        moves = []
        for d in sorted(range(-down, up + 1), key=abs):  # bij gelijke kosten: minste beweging
            wear = 0.0
            if d > 0:
                ac = d * step_kwh / bat.eta_charge
                wear = grid_charge_margin * max(0.0, ac - max(0.0, -net))
                # Eén rekenstap speling, anders blijft overschot kleiner dan een stap liggen.
                if not allow_grid_charge and (net >= 0 or ac > -net + step_kwh / bat.eta_charge):
                    continue
            elif d < 0:
                ac = d * step_kwh * bat.eta_discharge
                wear = bat.wear_eur_per_kwh * -d * step_kwh
                if not allow_export and (net <= 0 or -ac > net + step_kwh * bat.eta_discharge):
                    continue
            else:
                ac = 0.0
            grid = net + ac
            moves.append((d, (grid * slot.buy if grid > 0 else grid * slot.sell) + wear))
        new_value = [inf] * (n + 1)
        choice = [0] * (n + 1)
        for s in range(n + 1):
            best, best_d = inf, 0
            for d, c in moves:
                s2 = s + d
                if 0 <= s2 <= n:
                    v = c + value[s2]
                    if v < best - 1e-12:
                        best, best_d = v, d
            new_value[s], choice[s] = best, best_d
        if require_full:
            new_todo, choice_todo = [inf] * (n + 1), [0] * (n + 1)
            for s in range(n + 1):
                best, best_d = inf, 0
                for d, c in moves:
                    s2 = s + d
                    if 0 <= s2 <= n:
                        v = c + (value[s2] if s2 == n else todo[s2])
                        if v < best - 1e-12:
                            best, best_d = v, d
                new_todo[s], choice_todo[s] = best, best_d
            todo = new_todo
            choices_todo.append(choice_todo)
        value = new_value
        choices.append(choice)
    choices.reverse()
    choices_todo.reverse()
    full_seen = not require_full or s0 == n
    if require_full and not full_seen and todo[s0] == inf:
        return None, None

    steps, total, s = [], 0.0, s0
    for i, (slot, choice) in enumerate(zip(slots, choices)):
        d = choice[s] if full_seen else choices_todo[i][s]
        s += d
        full_seen = full_seen or s == n
        if d > 0:
            ac = d * step_kwh / bat.eta_charge
        else:
            ac = d * step_kwh * bat.eta_discharge
        grid = slot.load_kwh - slot.pv_kwh + ac
        cost = (grid * slot.buy if grid > 0 else grid * slot.sell)
        if d < 0:
            cost += bat.wear_eur_per_kwh * -d * step_kwh
        total += cost
        steps.append(Step(bat.min_soc_kwh + s * step_kwh, ac, grid, cost,
                          -1 if s == 0 else 1 if s == n else 0))
    return steps, total


def self_consumption(slots, bat, soc_start_kwh):
    """Referentie: de accu regelt simpelweg op nul op de meter."""
    soc, steps, total = min(bat.max_soc_kwh, max(bat.min_soc_kwh, soc_start_kwh)), [], 0.0
    for slot in slots:
        net = slot.load_kwh - slot.pv_kwh
        if net < 0:
            ac = min(-net, bat.max_charge_kw * slot.hours,
                     (bat.max_soc_kwh - soc) / bat.eta_charge)
            soc += ac * bat.eta_charge
            wear = 0.0
        else:
            out = min(net, bat.max_discharge_kw * slot.hours,
                      (soc - bat.min_soc_kwh) * bat.eta_discharge)
            soc -= out / bat.eta_discharge
            wear = bat.wear_eur_per_kwh * out / bat.eta_discharge
            ac = -out
        grid = net + ac
        cost = (grid * slot.buy if grid > 0 else grid * slot.sell) + wear
        total += cost
        steps.append(Step(soc, ac, grid, cost,
                          -1 if soc <= bat.min_soc_kwh + 1e-6
                          else 1 if soc >= bat.max_soc_kwh - 1e-6 else 0))
    return steps, total


# Modi die de accu-adapter in Home Assistant moet kunnen uitvoeren.
ZELFVERBRUIK, LADEN, ONTLADEN, VASTHOUDEN = "zelfverbruik", "laden", "ontladen", "vasthouden"


def decide_mode(step, slot, tol_kw=0.25):
    """Vertaalt een planstap naar (modus, vermogen in W).

    Waar het plan neerkomt op 'op nul regelen' krijgt de accu de modus
    zelfverbruik: zijn eigen snelle regeling vangt voorspelfouten dan op.
    Alleen bij bewust laden van het net, ontladen naar het net of sparen
    krijgt hij een vaste opdracht.
    """
    p = step.accu_ac_kwh / slot.hours
    net = (slot.load_kwh - slot.pv_kwh) / slot.hours
    follows_meter = abs(p + net) <= tol_kw
    counteracts = (p > 0 and net < 0 and p <= -net) or (p < 0 and net > 0 and -p <= net)
    # Lege accu bij verbruik of volle accu bij overschot: op nul regelen kan geen kwaad
    # en benut meevallers in de zonopwek.
    idle_at_limit = abs(p) <= tol_kw and ((step.at_limit < 0 and net > 0) or (step.at_limit > 0 and net < 0))
    if follows_meter or (counteracts and step.at_limit) or idle_at_limit:
        return ZELFVERBRUIK, 0
    if p > tol_kw:
        return LADEN, int(round(p * 1000, -1))
    if p < -tol_kw:
        return ONTLADEN, int(round(-p * 1000, -1))
    return VASTHOUDEN, 0


def refine_modes(steps, slots, modes, allow_grid_charge=True, allow_export=True):
    """Haalt afrondingseffecten uit het plan.

    De planner rekent in stapjes lading. Is het verbruik in een (kort) slot
    kleiner dan zo'n stapje, dan kiest hij soms voor vasthouden terwijl er
    daarna geen duurder moment meer komt om de lading voor te bewaren. In dat
    geval is gewoon de meter volgen beter.

    Om dezelfde reden kan een stapje net meer zijn dan het verbruik of het
    overschot. Mag de accu niet naar het net ontladen of van het net laden,
    dan wordt zo'n vaste opdracht ook de meter volgen.
    """
    out = list(modes)
    for i, (mode, _) in enumerate(modes):
        net = slots[i].load_kwh - slots[i].pv_kwh
        if mode == ONTLADEN and not allow_export:
            out[i] = (ZELFVERBRUIK, 0)
        if mode == LADEN and not allow_grid_charge and steps[i].accu_ac_kwh > -net + 1e-6:
            out[i] = (ZELFVERBRUIK, 0)
        if mode != VASTHOUDEN or slots[i].load_kwh <= slots[i].pv_kwh or steps[i].at_limit < 0:
            continue
        later = [slots[j].buy for j in range(i + 1, len(steps)) if steps[j].accu_ac_kwh < -1e-3]
        if later and max(later) <= slots[i].buy + 1e-6:
            out[i] = (ZELFVERBRUIK, 0)
    return out
