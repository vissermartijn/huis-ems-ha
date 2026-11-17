"""Verbruiksmodel: dagprofiel per uur plus een temperatuurafhankelijk deel.

verbruik(uur, T) = basis[uur] + helling[uur] * max(0, T_grens - T_24u)

T_24u is de gemiddelde buitentemperatuur over de voorgaande 24 uur; een huis
met vloerverwarming op een stooklijn reageert traag, dus dat volgt het
stookgedrag beter dan de temperatuur van het uur zelf. Elk uur van de dag
heeft zijn eigen helling: een hottub die alleen overdag verwarmt, verbruikt
bij kou overdag meer en 's nachts niet.

Hetzelfde model dient voor de warmtepomp, voor elke andere verbruiker met een
eigen meter en voor de rest van het huis. Werkdag en weekend krijgen alleen
een eigen profiel als dat op de eigen meetgegevens aantoonbaar beter
voorspelt.
"""
from dataclasses import dataclass, field

WEEK = 7 * 24
DAYTYPE_GAIN = 0.97     # weekendprofiel alleen bij minstens 3% kleinere fout
DAYTYPE_MIN = 8 * WEEK  # en pas vanaf acht weken aan gegevens


@dataclass
class LoadModel:
    base: list = field(default_factory=lambda: [0.0] * 24)    # kWh per uur
    slope: list = field(default_factory=lambda: [0.0] * 24)   # kWh per uur per graad
    t_limit: float = 16.0
    samples: int = 0
    weekend_base: list = None      # eigen profiel voor zaterdag en zondag, als dat helpt
    weekend_slope: list = None

    @property
    def k(self):
        """Gemiddelde helling in kWh per uur per graad; maal 24 is per dag."""
        return sum(self.slope) / 24

    @property
    def daytype(self):
        return self.weekend_base is not None

    def predict(self, hour_of_day, temp24, weekend=False):
        base, slope = self.base, self.slope
        if weekend and self.weekend_base is not None:
            base, slope = self.weekend_base, self.weekend_slope
        extra = slope[hour_of_day] * max(0.0, self.t_limit - temp24) if temp24 is not None else 0.0
        return max(0.0, base[hour_of_day] + extra)


def _weekend(sample):
    return bool(sample[3]) if len(sample) > 3 else False


def _fit_groups(samples, t_limit, group):
    """Kleinste kwadraten per groep. Geeft ({groep: (basis, helling)}, kwadratensom)."""
    sums = {}
    for s in samples:
        x = max(0.0, t_limit - s[2]) if s[2] is not None else 0.0
        a = sums.setdefault(group(s), [0.0, 0.0, 0.0, 0.0, 0.0, 0])
        y = s[1]
        a[0] += y
        a[1] += x
        a[2] += x * y
        a[3] += x * x
        a[4] += y * y
        a[5] += 1
    out, sse = {}, 0.0
    for g, (sy, sx, sxy, sxx, syy, n) in sums.items():
        var = sxx - sx * sx / n
        cov = sxy - sx * sy / n
        k = max(0.0, cov / var) if var > 1e-9 else 0.0
        out[g] = (sy / n - k * sx / n, k)
        sse += syy - sy * sy / n - 2 * k * cov + k * k * var
    return out, sse


def _by_hour(s):
    return s[0]


def _by_hour_and_daytype(s):
    return s[0] + (24 if _weekend(s) else 0)


def _holdout_error(samples, t_limit, group):
    """Gemiddelde fout op weken die niet zijn meegeleerd (om en om)."""
    error = 0.0
    for fold in (0, 1):
        train = [s for i, s in enumerate(samples) if (i // WEEK) % 2 == fold]
        if len(train) < 2 * WEEK:
            return None
        params, _ = _fit_groups(train, t_limit, group)
        for i, s in enumerate(samples):
            if (i // WEEK) % 2 == fold:
                continue
            base, k = params.get(group(s)) or params.get(s[0]) or (0.0, 0.0)
            x = max(0.0, t_limit - s[2]) if s[2] is not None else 0.0
            error += abs(max(0.0, base + k * x) - s[1])
    return error


def fit(samples, limits=range(8, 22)):
    """samples: [(uur van de dag, kWh in dat uur, T_24u of None[, weekend])], op tijd gesorteerd."""
    samples = [s for s in samples if s[1] is not None and s[1] >= 0]
    if len(samples) < 48:
        return None
    with_temp = [s for s in samples if s[2] is not None]
    t_limit, use = 16.0, samples
    if len(with_temp) >= 24 * 10:
        use = with_temp
        t_limit = float(min(limits, key=lambda tl: _fit_groups(with_temp, float(tl), _by_hour)[1]))
    params, _ = _fit_groups(use, t_limit, _by_hour)
    if use is with_temp and not any(k > 0 for _, k in params.values()):
        use = samples  # geen temperatuureffect: dan tellen ook de uren zonder temperatuur mee
        params, _ = _fit_groups([(s[0], s[1], None) + tuple(s[3:]) for s in use], t_limit, _by_hour)
    # Uren zonder gegevens (bijvoorbeeld overdag zonder zonmeting) krijgen het gemiddelde.
    mean = (sum(v[0] for v in params.values()) / len(params), sum(v[1] for v in params.values()) / len(params))
    model = LoadModel([params.get(h, mean)[0] for h in range(24)],
                      [params.get(h, mean)[1] for h in range(24)], t_limit, len(use))
    if len(use) >= DAYTYPE_MIN and any(_weekend(s) for s in use):
        plain = _holdout_error(use, t_limit, _by_hour)
        split = _holdout_error(use, t_limit, _by_hour_and_daytype)
        if plain and split is not None and split < DAYTYPE_GAIN * plain:
            both, _ = _fit_groups(use, t_limit, _by_hour_and_daytype)
            if all(g in both for g in range(48)):
                model.base = [both[h][0] for h in range(24)]
                model.slope = [both[h][1] for h in range(24)]
                model.weekend_base = [both[h + 24][0] for h in range(24)]
                model.weekend_slope = [both[h + 24][1] for h in range(24)]
    return model


def rolling_mean_24(temps):
    """temps: {tijdstip (uur): temperatuur}. Geeft {tijdstip: gemiddelde vorige 24 uur}."""
    out, keys = {}, sorted(temps)
    window, total, start = [], 0.0, 0
    for t in keys:
        window.append(t)
        total += temps[t]
        while (t - window[start]).total_seconds() >= 24 * 3600:
            total -= temps[window[start]]
            start += 1
        out[t] = total / (len(window) - start)
    return out


def level_correction(model, recent, low=0.6, high=1.6):
    """Factor die het model bijstelt op de laatste dagen.

    recent: zelfde vorm als voor fit(). Vangt op wat het model niet kent:
    een gewijzigde stooklijn, vakantie, een extra verbruiker.
    """
    actual = sum(s[1] for s in recent)
    expected = sum(model.predict(s[0], s[2], _weekend(s)) for s in recent)
    if len(recent) < 24 or expected <= 0:
        return 1.0
    return min(high, max(low, actual / expected))


def thermostat_factors(model, samples, band=0.3, low=0.0, high=4.0):
    """Hoeveel een verbruiker met thermostaat afwijkt van zijn model, per toestand.

    samples: [(uur, kWh, T_24u, weekend, tekort)] met tekort = ingestelde min
    gemeten temperatuur in het uur ervoor. Geeft (factor als hij op temperatuur
    is, factor als hij warmte vraagt); (1, 1) als er te weinig gegevens zijn.
    """
    totals = {False: [0.0, 0.0, 0], True: [0.0, 0.0, 0]}
    for s in samples:
        if s[4] is None:
            continue
        a = totals[s[4] > band]
        a[0] += s[1]
        a[1] += model.predict(s[0], s[2], s[3])
        a[2] += 1
    out = []
    for demand in (False, True):
        actual, expected, n = totals[demand]
        out.append(min(high, max(low, actual / expected)) if n >= 48 and expected > 0 else 1.0)
    if totals[False][2] < 48 or totals[True][2] < 48:
        return 1.0, 1.0
    return out[0], out[1]


def thermostat_weight(hours_ahead, persistence_h=4.0):
    """Hoe zwaar de toestand van nu nog telt over een aantal uren (1 = volledig)."""
    return 0.5 ** (max(0.0, hours_ahead) / persistence_h)
