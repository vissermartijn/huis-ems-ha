"""Warmtebuffers: zonnestroom opslaan als warmte in plaats van terugleveren.

Twee soorten:

- thermostaat: de ingestelde temperatuur tijdelijk wat hoger, zodat de
  warmtepomp bij zoninstraling blijft doordraaien en warmte in de vloer
  pompt. De stooklijn blijft ongemoeid.
- schakelaar: een verwarmingselement (boiler) aan zolang er genoeg over is;
  de eigen thermostaat van het vat blijft de harde grens.

De beslissing per meting zit in stage2.decide (wachttijden, minimale draai-
en rusttijd, volgorde). Hier staan de voorwaarden die daarbovenop gelden.
"""
from dataclasses import dataclass, field

from .stage2 import Load

THERMOSTAT, SWITCH = "thermostaat", "schakelaar"
SOC_HYSTERESIS = 3.0       # procentpunt: zo ver mag de lading zakken voordat een buffer uitgaat
FORECAST_FRACTION = 0.8    # zoveel van het vermogen moet de voorspelling aan overschot tonen
IDLE_W = 100.0             # minder dan dit verbruik telt als 'neemt niets meer af'
REST_SECONDS = 7200        # na 'neemt niets meer af' zo lang niet opnieuw proberen


@dataclass
class Buffer(Load):
    kind: str = SWITCH
    min_soc_pct: float = 90.0
    start_hour: float = 0.0
    end_hour: float = 24.0
    max_temp: float | None = None
    boost_c: float = 1.0
    need_forecast: bool = False
    rest_after_s: float = 0.0      # 0 = niet bewaken


@dataclass
class Context:
    """Wat er nu bekend is, voor alle buffers tegelijk."""
    hour: float                                   # lokale tijd als decimaal uur
    soc_pct: float | None = None                  # None: geen accu ingesteld
    soc_problem: str | None = None                # accu ingesteld maar lading onbruikbaar
    temps: dict = field(default_factory=dict)     # id -> gemeten temperatuur
    forecast_w: dict = field(default_factory=dict)  # id -> verwacht overschot (W)
    unavailable: dict = field(default_factory=dict)  # id -> reden
    resting: dict = field(default_factory=dict)   # id -> tijdstip tot wanneer


def _hhmm(hour):
    h = int(hour) % 24
    return f"{h:02d}:{int(round((hour - int(hour)) * 60)):02d}"


def _in_window(buf, hour):
    if buf.start_hour <= buf.end_hour:
        return buf.start_hour <= hour < buf.end_hour
    return hour >= buf.start_hour or hour < buf.end_hour   # venster over middernacht


def start_block(buf, ctx, now_s):
    """Waarom deze buffer nu niet aan mag, of None."""
    if buf.id in ctx.unavailable:
        return ctx.unavailable[buf.id]
    until = ctx.resting.get(buf.id)
    if until and now_s < until:
        return "nam geen stroom meer af; probeert het later opnieuw"
    if not _in_window(buf, ctx.hour):
        return f"alleen tussen {_hhmm(buf.start_hour)} en {_hhmm(buf.end_hour)}"
    if ctx.soc_problem:
        return f"laadtoestand van de accu {ctx.soc_problem}"
    if ctx.soc_pct is not None and ctx.soc_pct < buf.min_soc_pct:
        return f"accu op {ctx.soc_pct:.0f}%, eerst tot {buf.min_soc_pct:.0f}%"
    temp = ctx.temps.get(buf.id)
    if buf.max_temp is not None and temp is not None and temp >= buf.max_temp:
        return f"{temp:.1f} °C, grens {buf.max_temp:.1f} °C"
    if buf.need_forecast:
        ahead = ctx.forecast_w.get(buf.id)
        if ahead is not None and ahead < buf.power_w * FORECAST_FRACTION:
            return (f"verwacht overschot de komende {round(buf.min_on_s / 60)} min "
                    f"{ahead:.0f} W, {buf.power_w:.0f} W nodig")
    return None


def stop_reason(buf, ctx, now_s):
    """Harde grens waarop een buffer die aan staat meteen uit gaat, of None."""
    if buf.id in ctx.unavailable:
        return ctx.unavailable[buf.id]
    if not _in_window(buf, ctx.hour):
        return f"na {_hhmm(buf.end_hour)}"
    temp = ctx.temps.get(buf.id)
    if buf.max_temp is not None and temp is not None and temp >= buf.max_temp:
        return f"{temp:.1f} °C bereikt (grens {buf.max_temp:.1f} °C)"
    if ctx.soc_problem:
        return f"laadtoestand van de accu {ctx.soc_problem}"
    if ctx.soc_pct is not None and ctx.soc_pct < buf.min_soc_pct - SOC_HYSTERESIS:
        return f"accu gezakt naar {ctx.soc_pct:.0f}%"
    until = ctx.resting.get(buf.id)
    if until and now_s < until:
        return "neemt geen stroom meer af" + (" (vat op temperatuur)" if buf.kind == SWITCH
                                               else " (geen warmtevraag)")
    return None


class IdleWatch:
    """Ziet of een buffer die aan staat nog stroom afneemt.

    Een boiler op temperatuur of een warmtepomp zonder warmtevraag neemt niets
    af. Dan heeft aan laten staan geen zin en houdt hij overschot vast dat een
    volgende buffer had kunnen gebruiken.
    """

    def __init__(self):
        self.low_since: dict[str, float] = {}

    def update(self, buf, on, draw_w, now_s):
        """Geeft het tijdstip tot wanneer de buffer moet rusten, of None."""
        if not on or buf.rest_after_s <= 0 or draw_w is None or draw_w >= IDLE_W:
            self.low_since.pop(buf.id, None)
            return None
        since = self.low_since.setdefault(buf.id, now_s)
        if now_s - since >= buf.rest_after_s:
            self.low_since.pop(buf.id, None)
            return now_s + REST_SECONDS
        return None


def spare_w(grid_w, battery_w, counted_w):
    """Wat er voor de buffers over is.

    grid_w: netvermogen met accu (+ = afname); battery_w: accuvermogen
    (+ = laden); counted_w: wat de buffers die aan staan nu zelf gebruiken.
    Wat de accu laadt telt niet als overschot (de accu gaat voor); wat hij
    ontlaadt telt als tekort, zodat de accu nooit een buffer voedt.
    """
    return -grid_w + min(0.0, battery_w) + counted_w


def boost_target(normal, boost, step=0.5):
    """De verhoogde ingestelde temperatuur, afgerond op de stap van de thermostaat."""
    step = step or 0.5
    return round(round((normal + boost) / step) * step, 2)
