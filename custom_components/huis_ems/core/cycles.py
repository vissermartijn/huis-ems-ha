"""Verbruik in cycli: wasmachine, vaatwasser, droger en tapwater van de warmtepomp.

Zo'n verbruiker draait een programma van een vaste duur met een herkenbaar
verloop. Er zijn twee soorten kennis:

- **aangekondigd**: een programma loopt of staat op uitgestelde start. Dan is het
  begin bekend (en via Home Connect vaak ook het einde) en wordt het verbruik
  daar precies ingepland;
- **gewoonte**: uit de starts van de afgelopen weken volgt hoe vaak per dag en op
  welk uur hij meestal start. De verwachting voor de rest van de dag houdt
  rekening met wat er vandaag al gedraaid heeft.

De gewoonte zit ook al in de langetermijnmeting van het huis (of van de
warmtepomp, voor tapwater). Daarom levert de voorspelling ook een *basislijn*:
het gemiddelde dat de gewoonte op dat uur bijdraagt. Die gaat van het model van
het huis af, waarna de eigen voorspelling erbij komt; zo telt niets dubbel.
"""
from dataclasses import dataclass, field
from datetime import timedelta
from statistics import median

# Verdeling van het verbruik over de looptijd, in tien gelijke stukken.
PROFILES = {
    "wasmachine": [0.36, 0.26, 0.07, 0.05, 0.05, 0.04, 0.04, 0.04, 0.04, 0.05],
    "vaatwasser": [0.04, 0.30, 0.16, 0.03, 0.03, 0.03, 0.04, 0.24, 0.10, 0.03],
    "droger": [0.08, 0.12, 0.12, 0.12, 0.12, 0.12, 0.11, 0.09, 0.07, 0.05],
    "tapwater": [0.1] * 10,
    "overig": [0.1] * 10,
}
# Standaard kWh per keer en duur in minuten, tot er genoeg geleerd is.
DEFAULTS = {
    "wasmachine": (0.9, 120), "vaatwasser": (1.0, 180), "droger": (2.0, 120),
    "tapwater": (1.5, 60), "overig": (1.0, 90),
}
PRIOR_CYCLES = 2      # zo zwaar telt de standaardwaarde mee bij het leren
MERGE_GAP = timedelta(minutes=10)   # korter onderbroken telt als één keer


@dataclass
class Cycle:
    start: object          # tijdzone-bewuste datetime (lokale tijd)
    end: object
    ongoing: bool = False
    kwh: float | None = None


@dataclass
class Habit:
    kind: str
    rate_per_day: float
    hour_p: list            # kans per uur van de dag dat een start op dat uur valt (som 1)
    kwh: float
    duration_h: float
    cycles: int
    days: float
    kwh_learned: int = 0    # aantal keer waarvan het verbruik bekend was
    profile: list = field(default_factory=list)

    def usual_hours(self, count=2):
        ranked = sorted(range(24), key=lambda h: -self.hour_p[h])
        return sorted(ranked[:count])


def cycles_from_states(states, on_states, until):
    """[(tijdstip, toestand)] op tijd gesorteerd -> [Cycle]. Korte onderbrekingen tellen niet."""
    out, start = [], None
    for t, state in states:
        if state in on_states:
            if start is None:
                start = t
        elif start is not None:
            out.append(Cycle(start, t))
            start = None
    if start is not None:
        out.append(Cycle(start, until, ongoing=True))
    merged = []
    for c in out:
        if merged and c.start - merged[-1].end < MERGE_GAP:
            merged[-1] = Cycle(merged[-1].start, c.end, c.ongoing)
        else:
            merged.append(c)
    return merged


def learn(kind, cycles, days, default_kwh=None, default_min=None):
    """Leert de gewoonte uit de cycli van de afgelopen `days` dagen."""
    base_kwh, base_min = DEFAULTS.get(kind, DEFAULTS["overig"])
    base_kwh = default_kwh or base_kwh
    base_h = (default_min or base_min) / 60
    days = max(1.0, days)
    counts = [0.0] * 24
    for c in cycles:
        h = c.start.hour
        counts[h] += 0.5
        counts[(h - 1) % 24] += 0.25
        counts[(h + 1) % 24] += 0.25
    n = len(cycles)
    prior = 1.0
    hour_p = [(counts[h] + prior / 24) / (n + prior) for h in range(24)]
    finished = [(c.end - c.start).total_seconds() / 3600 for c in cycles if not c.ongoing]
    finished = [d for d in finished if d > 0.05]
    if finished:
        w = len(finished)
        duration = (median(finished) * w + base_h * PRIOR_CYCLES) / (w + PRIOR_CYCLES)
    else:
        duration = base_h
    known = [c.kwh for c in cycles if not c.ongoing and c.kwh is not None]
    kwh = (sum(known) + base_kwh * PRIOR_CYCLES) / (len(known) + PRIOR_CYCLES)
    return Habit(kind, n / days, hour_p, kwh, duration, n, days, len(known),
                 PROFILES.get(kind, PROFILES["overig"]))


def _energy(profile, kwh, a, b):
    """Energie tussen relatieve tijden a en b (0 = begin, 1 = einde van de cyclus)."""
    a, b = max(0.0, a), min(1.0, b)
    if b <= a:
        return 0.0
    total, n = 0.0, len(profile)
    for i, share in enumerate(profile):
        lo, hi = i / n, (i + 1) / n
        overlap = min(b, hi) - max(a, lo)
        if overlap > 0:
            total += share * kwh * overlap * n
    return total


def spread(profile, kwh, start, duration_h, slot_starts, slot_hours, out, weight=1.0):
    """Telt het verbruik van één cyclus op bij `out` (kWh per slot)."""
    if duration_h <= 0 or weight <= 0:
        return
    span = duration_h * 3600
    for i, (s, h) in enumerate(zip(slot_starts, slot_hours)):
        a = (s - start).total_seconds() / span
        if a >= 1:
            break
        b = a + h * 3600 / span
        if b <= 0:
            continue
        out[i] += weight * _energy(profile, kwh, a, b)


def forecast(habit, slot_starts, slot_hours, now, known=(), started_today=0):
    """Verwacht verbruik per slot (kWh), en de basislijn van de gewoonte.

    known: [(begin, duur in uren)] van aangekondigde cycli (lopend of uitgesteld).
    started_today: zoveel keer is hij vandaag al gestart (inclusief aangekondigd).
    """
    n = len(slot_starts)
    values, baseline = [0.0] * n, [0.0] * n
    if not slot_starts:
        return values, baseline
    for start, dur in known:
        spread(habit.profile, habit.kwh, start, dur, slot_starts, slot_hours, values)

    # Basislijn: elke mogelijke start, ook van vóór de eerste slot die nu nog zou lopen.
    nominal = max(slot_hours)               # het eerste slot is korter: het lopende kwartier
    step = timedelta(hours=nominal)
    first = slot_starts[0]
    back = int(habit.duration_h / nominal) + 1
    candidates = [first - step * k for k in range(back, 0, -1)] + list(slot_starts)
    busy_until = max((s + timedelta(hours=d) for s, d in known), default=None)

    # Wat er vandaag nog kan komen: de dagelijkse gewoonte min wat er al was.
    today = now.date()
    rest_today = sum(habit.hour_p[s.hour] * nominal for s in slot_starts if s.date() == today)
    expected_today = habit.rate_per_day * rest_today
    factor_today = 0.0 if expected_today <= 1e-9 else min(
        1.0, max(0.0, habit.rate_per_day - started_today) / expected_today)

    for s in candidates:
        mass = habit.rate_per_day * habit.hour_p[s.hour] * nominal
        if mass <= 0:
            continue
        mid = s + step / 2
        spread(habit.profile, habit.kwh, mid, habit.duration_h, slot_starts, slot_hours, baseline, mass)
        if s < first or (busy_until is not None and s < busy_until):
            continue
        w = mass * (factor_today if s.date() == today else 1.0)
        spread(habit.profile, habit.kwh, mid, habit.duration_h, slot_starts, slot_hours, values, w)
    return values, baseline


def cycle_energy(cycle, rows, period_s=300):
    """Energie tijdens een cyclus uit [(begin van de periode, kWh in de periode)]."""
    total, seen = 0.0, False
    for t, kwh in rows:
        end = t + timedelta(seconds=period_s)
        overlap = (min(end, cycle.end) - max(t, cycle.start)).total_seconds()
        if overlap > 0:
            total += kwh * overlap / period_s
            seen = True
    return total if seen else None


def residual_energy(cycle, rows, baseline_by_hour):
    """Schatting zonder eigen meter: meer dan gewoonlijk verbruik tijdens de cyclus.

    rows: [(begin kwartier, overig verbruik in W)]; baseline_by_hour: gewoonlijk
    overig verbruik (W) per uur van de dag, gemeten buiten de cycli.
    """
    total, seen = 0.0, 0.0
    for t, watt in rows:
        end = t + timedelta(minutes=15)
        overlap = (min(end, cycle.end) - max(t, cycle.start)).total_seconds()
        if overlap > 0 and baseline_by_hour.get(t.hour) is not None:
            total += (watt - baseline_by_hour[t.hour]) / 1000 * overlap / 3600
            seen += overlap
    span = (cycle.end - cycle.start).total_seconds()
    if span <= 0 or seen < 0.8 * span:
        return None
    return max(0.0, total)
