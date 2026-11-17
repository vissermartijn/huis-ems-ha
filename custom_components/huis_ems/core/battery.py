"""De accu op zelfverbruik: nabootsen per meting en vooruitrekenen per kwartier.

De accu regelt zichzelf op nul op de meter; het EMS stuurt hem niet. Het rekent
na wat hij gaat doen, met de laadtoestand van nu als begin: wanneer hij vol raakt
en hoeveel zon er daarna nog over is, wanneer hij leeg raakt en hoeveel er daarna
van het net komt. Alle energie in kWh, vermogen in kW. accu_ac_kwh > 0 is laden.
"""
from dataclasses import dataclass


@dataclass
class Battery:
    min_soc_kwh: float = 0.8
    max_soc_kwh: float = 8.0
    max_charge_kw: float = 3.0
    max_discharge_kw: float = 3.0
    eta_charge: float = 0.93
    eta_discharge: float = 0.93


@dataclass
class Slot:
    hours: float
    load_kwh: float   # huisverbruik zonder accu
    pv_kwh: float     # zonopwek


@dataclass
class Step:
    soc_kwh: float        # laadtoestand aan het einde van het slot
    accu_ac_kwh: float
    grid_kwh: float       # > 0 afname, < 0 teruglevering
    at_limit: int         # -1 = accu leeg na dit slot, +1 = vol, 0 = ertussen


def self_consumption(slots, bat, soc_start_kwh):
    """De accu volgt de meter: overschot gaat erin, tekort komt eruit."""
    soc, steps = min(bat.max_soc_kwh, max(bat.min_soc_kwh, soc_start_kwh)), []
    for slot in slots:
        net = slot.load_kwh - slot.pv_kwh
        if net < 0:
            ac = min(-net, bat.max_charge_kw * slot.hours, (bat.max_soc_kwh - soc) / bat.eta_charge)
            soc += ac * bat.eta_charge
        else:
            out = min(net, bat.max_discharge_kw * slot.hours, (soc - bat.min_soc_kwh) * bat.eta_discharge)
            soc -= out / bat.eta_discharge
            ac = -out
        steps.append(Step(soc, ac, net + ac,
                          -1 if soc <= bat.min_soc_kwh + 1e-6
                          else 1 if soc >= bat.max_soc_kwh - 1e-6 else 0))
    return steps


@dataclass
class Unit:
    id: str
    name: str
    capacity_kwh: float
    min_soc_kwh: float
    max_soc_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    eta: float            # rendement per richting
    soc_kwh: float
    virtual: bool = False

    @property
    def headroom(self):
        return max(0.0, self.max_soc_kwh - self.soc_kwh)

    @property
    def available(self):
        return max(0.0, self.soc_kwh - self.min_soc_kwh)


def aggregate(units):
    """Geeft (Battery, laadtoestand) voor alle accu's samen."""
    cap = sum(u.capacity_kwh for u in units)
    eta = sum(u.eta * u.capacity_kwh for u in units) / cap if cap else 0.93
    bat = Battery(
        min_soc_kwh=sum(u.min_soc_kwh for u in units),
        max_soc_kwh=sum(u.max_soc_kwh for u in units),
        max_charge_kw=sum(u.max_charge_kw for u in units),
        max_discharge_kw=sum(u.max_discharge_kw for u in units),
        eta_charge=eta, eta_discharge=eta)
    soc = sum(min(u.max_soc_kwh, max(u.min_soc_kwh, u.soc_kwh)) for u in units)
    return bat, soc


def follow_meter(unit, grid_w, dt_s):
    """Wat een virtuele accu op zelfverbruik doet: (vermogen in W, nieuwe laadtoestand).

    grid_w is het netvermogen zonder deze accu (+ = afname).
    """
    hours = dt_s / 3600.0
    if hours <= 0:
        return 0.0, unit.soc_kwh
    want = -grid_w
    if want > 0:
        p = min(want, unit.max_charge_kw * 1000, unit.headroom / unit.eta / hours * 1000)
        soc = unit.soc_kwh + p / 1000 * hours * unit.eta
    else:
        p = -min(-want, unit.max_discharge_kw * 1000, unit.available * unit.eta / hours * 1000)
        soc = unit.soc_kwh + p / 1000 * hours / unit.eta
    return p, soc


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


def outlook(slots, bat, soc_kwh, window=96):
    """Rekent zelfverbruik door en vat de eerste `window` kwartieren samen."""
    steps = self_consumption(slots, bat, soc_kwh)
    n = min(window, len(steps))
    has_battery = bat.max_soc_kwh - bat.min_soc_kwh > 0.01
    full_at = empty_at = None
    if has_battery:
        full_at = next((i for i in range(n) if steps[i].at_limit > 0
                        and slots[i].pv_kwh > slots[i].load_kwh), None)
        empty_at = next((i for i in range(n) if steps[i].at_limit < 0 and steps[i].grid_kwh > 0.005), None)
    surplus = sum(-s.grid_kwh for s in steps[:n] if s.grid_kwh < 0)
    grid = sum(s.grid_kwh for s in steps[:n] if s.grid_kwh > 0)
    after_full = sum(-s.grid_kwh for s in steps[full_at:n] if s.grid_kwh < 0) if full_at is not None else 0.0
    after_empty = sum(s.grid_kwh for s in steps[empty_at:n] if s.grid_kwh > 0) if empty_at is not None else 0.0
    return Outlook(steps, n, full_at, empty_at, surplus, after_full, grid, after_empty)


def _kwh(value):
    return f"{value:.1f}".replace(".", ",")


def describe(look, starts, has_battery=True):
    """De vooruitblik in één of twee zinnen, voor het dashboard."""
    parts = []

    def when(i):
        same_day = starts[i].date() == starts[0].date()
        return f"{starts[i]:%H:%M}" if same_day else f"morgen {starts[i]:%H:%M}"

    if has_battery:
        if look.full_at is not None:
            text = f"Accu vol rond {when(look.full_at)}"
            parts.append(text + (f", daarna nog {_kwh(look.surplus_after_full_kwh)} kWh zon over."
                                 if look.surplus_after_full_kwh >= 0.1 else ", daarna geen overschot van betekenis."))
        if look.empty_at is not None:
            parts.append(f"Accu leeg rond {when(look.empty_at)}, daarna "
                         f"{_kwh(look.grid_after_empty_kwh)} kWh van het net.")
        if not parts:
            parts.append("De accu raakt de komende 24 uur niet vol en niet leeg.")
    else:
        parts.append(f"Komende 24 uur {_kwh(look.surplus_kwh)} kWh zon over en "
                     f"{_kwh(look.grid_kwh)} kWh van het net.")
    return " ".join(parts)
