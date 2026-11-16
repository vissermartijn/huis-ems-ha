"""Energiebalans vooruit: wat de accu op eigen zelfverbruik gaat doen.

De accu regelt zichzelf op nul op de meter; het EMS stuurt hem niet. Het
rekent wel na wat hij gaat doen, met de laadtoestand van nu als begin: wanneer
hij vol raakt en hoeveel zonnestroom er daarna nog over is, wanneer hij leeg
raakt en hoeveel er daarna van het net komt, en hoeveel daarvan in dure uren.
"""
from dataclasses import dataclass

from .optimizer import self_consumption

EXPENSIVE_QUANTILE = 0.75   # de duurste kwart van de kwartieren telt als duur
MIN_SPREAD = 0.02           # kleiner prijsverschil: geen dure uren aanwijzen


@dataclass
class Outlook:
    steps: list
    window: int                 # aantal kwartieren waarover de samenvatting gaat
    full_at: int | None         # eerste kwartier waarin de accu vol raakt
    empty_at: int | None        # eerste kwartier waarin de accu leeg raakt en het net bijspringt
    surplus_kwh: float          # teruglevering binnen het venster
    surplus_after_full_kwh: float
    grid_kwh: float             # afname binnen het venster
    grid_after_empty_kwh: float
    grid_expensive_kwh: float   # afname in dure kwartieren
    expensive_from: float | None


def _threshold(prices):
    """Vanaf welke prijs een kwartier duur is: het duurste kwart, en duidelijk boven de mediaan.

    Zonder die tweede eis telt bij een vlakke dag met één dure piek bijna alles als duur.
    """
    ranked = sorted(prices)
    if not ranked or ranked[-1] - ranked[0] < MIN_SPREAD:
        return None
    quantile = ranked[min(len(ranked) - 1, int(len(ranked) * EXPENSIVE_QUANTILE))]
    floor = ranked[len(ranked) // 2] + MIN_SPREAD
    return next((p for p in ranked if p >= quantile and p >= floor - 1e-9), None)


def outlook(slots, bat, soc_kwh, window=96):
    """Rekent zelfverbruik door en vat de eerste `window` kwartieren samen."""
    steps, _ = self_consumption(slots, bat, soc_kwh)
    n = min(window, len(steps))
    has_battery = bat.max_soc_kwh - bat.min_soc_kwh > 0.01
    full_at = empty_at = None
    if has_battery:
        full_at = next((i for i in range(n) if steps[i].at_limit > 0
                        and slots[i].pv_kwh > slots[i].load_kwh), None)
        empty_at = next((i for i in range(n) if steps[i].at_limit < 0 and steps[i].grid_kwh > 0.005), None)
    limit = _threshold([s.buy for s in slots[:n]])
    surplus = sum(-s.grid_kwh for s in steps[:n] if s.grid_kwh < 0)
    grid = sum(s.grid_kwh for s in steps[:n] if s.grid_kwh > 0)
    after_full = sum(-s.grid_kwh for s in steps[full_at:n] if s.grid_kwh < 0) if full_at is not None else 0.0
    after_empty = sum(s.grid_kwh for s in steps[empty_at:n] if s.grid_kwh > 0) if empty_at is not None else 0.0
    expensive = sum(st.grid_kwh for st, sl in zip(steps[:n], slots[:n])
                    if st.grid_kwh > 0 and limit is not None and sl.buy >= limit)
    return Outlook(steps, n, full_at, empty_at, surplus, after_full, grid, after_empty, expensive, limit)


def export_ahead_w(steps, slots, count):
    """Gemiddelde verwachte teruglevering (W) over de eerstvolgende `count` kwartieren."""
    part = list(zip(steps[:count], slots[:count]))
    hours = sum(sl.hours for _, sl in part)
    if hours <= 0:
        return 0.0
    return sum(max(0.0, -st.grid_kwh) for st, _ in part) / hours * 1000


def _kwh(value):
    return f"{value:.1f}".replace(".", ",")


def describe(look, starts, has_battery=True):
    """De vooruitblik in één of twee zinnen, voor het dashboard."""
    parts = []
    fmt = "%H:%M"

    def when(i):
        same_day = starts[i].date() == starts[0].date()
        return f"{starts[i]:{fmt}}" if same_day else f"morgen {starts[i]:{fmt}}"

    if has_battery:
        if look.full_at is not None:
            text = f"Accu vol rond {when(look.full_at)}"
            parts.append(text + (f", daarna nog {_kwh(look.surplus_after_full_kwh)} kWh zon over."
                                 if look.surplus_after_full_kwh >= 0.1 else ", daarna geen overschot van betekenis."))
        if look.empty_at is not None:
            text = f"Accu leeg rond {when(look.empty_at)}, daarna {_kwh(look.grid_after_empty_kwh)} kWh van het net"
            parts.append(text + (f" ({_kwh(look.grid_expensive_kwh)} kWh in dure uren)."
                                 if look.grid_expensive_kwh >= 0.1 else "."))
        if not parts:
            parts.append("De accu raakt de komende 24 uur niet vol en niet leeg.")
    else:
        parts.append(f"Komende 24 uur {_kwh(look.surplus_kwh)} kWh zon over en "
                     f"{_kwh(look.grid_kwh)} kWh van het net.")
    return " ".join(parts)
