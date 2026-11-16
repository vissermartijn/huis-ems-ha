"""Accu's als één geheel doorrekenen, en een virtuele accu op zelfverbruik nabootsen."""
from dataclasses import dataclass

from .optimizer import LADEN, ONTLADEN, VASTHOUDEN, ZELFVERBRUIK, Battery


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


def aggregate(units, wear_eur_per_kwh):
    """Geeft (Battery, laadtoestand) voor alle accu's samen."""
    cap = sum(u.capacity_kwh for u in units)
    eta = sum(u.eta * u.capacity_kwh for u in units) / cap if cap else 0.93
    bat = Battery(
        min_soc_kwh=sum(u.min_soc_kwh for u in units),
        max_soc_kwh=sum(u.max_soc_kwh for u in units),
        max_charge_kw=sum(u.max_charge_kw for u in units),
        max_discharge_kw=sum(u.max_discharge_kw for u in units),
        eta_charge=eta, eta_discharge=eta, wear_eur_per_kwh=wear_eur_per_kwh)
    soc = sum(min(u.max_soc_kwh, max(u.min_soc_kwh, u.soc_kwh)) for u in units)
    return bat, soc


def respond(mode, power_w, unit, grid_w, dt_s):
    """Wat een (virtuele) accu werkelijk doet: (vermogen in W, nieuwe laadtoestand).

    grid_w is het netvermogen zonder deze accu (+ = afname). Houdt rekening
    met vermogensgrenzen, rendement en wat er nog in of uit kan.
    """
    hours = dt_s / 3600.0
    if mode == ZELFVERBRUIK:
        want = -grid_w
    elif mode == LADEN:
        want = power_w
    elif mode == ONTLADEN:
        want = -power_w
    else:
        want = 0.0
    if hours <= 0:
        return 0.0, unit.soc_kwh
    if want > 0:
        p = min(want, unit.max_charge_kw * 1000, unit.headroom / unit.eta / hours * 1000)
        soc = unit.soc_kwh + p / 1000 * hours * unit.eta
    else:
        p = -min(-want, unit.max_discharge_kw * 1000, unit.available * unit.eta / hours * 1000)
        soc = unit.soc_kwh + p / 1000 * hours / unit.eta
    return p, soc
