"""Verschuifbaar verbruik: verbruikers die aan mogen als er na de accu nog zon over is.

De accu gaat voor. Pas wat daarna nog naar het net zou gaan is beschikbaar
voor deze verbruikers, in de volgorde waarin ze zijn ingesteld. Elke
verbruiker heeft wachttijden en een minimale draai- en rusttijd, zodat een
wolk niet meteen tot schakelen leidt en apparaten met een compressor of
laadsessie niet pendelen.
"""
from dataclasses import dataclass

ON_MARGIN_W = 50      # zoveel moet er boven het vermogen over zijn om in te schakelen
KEEP_FRACTION = 0.9   # eenmaal aan mag het overschot 10% tekortschieten


@dataclass
class Load:
    id: str
    name: str
    power_w: float
    on_delay_s: float = 300.0     # zo lang moet er genoeg overschot zijn
    off_delay_s: float = 120.0    # zo lang mag het overschot tekortschieten
    min_on_s: float = 900.0
    min_off_s: float = 900.0


@dataclass
class LoadState:
    on: bool = False
    since: float = None        # moment van de laatste schakeling
    want_since: float = None   # sinds wanneer de gewenste stand afwijkt
    note: str = "uit"


def _minutes(seconds):
    return f"{max(1, round(seconds / 60))} min"


def decide(loads, states, spare_w, now_s, blocked=None, started=None, stop=None):
    """Werkt de standen bij. Geeft de lijst van (verbruiker, nieuwe stand) die moet schakelen.

    spare_w: overschot dat na de accu overblijft als geen van deze verbruikers
    zou draaien; None als er geen betrouwbare meting is.
    blocked: {id: reden} of een functie (verbruiker) -> reden of None, voor
    verbruikers die nu niet mogen inschakelen.
    started: wordt aangeroepen zodra een verbruiker wordt ingeschakeld, vóórdat
    de volgende wordt beoordeeld.
    stop: functie (verbruiker) -> reden of None. Een verbruiker die aan staat en
    een reden krijgt, gaat meteen uit, ook binnen zijn minimale draaitijd: dit
    zijn harde grenzen zoals een eindtijd of een maximale temperatuur.
    """
    if not callable(blocked):
        table = blocked or {}
        blocked = lambda load: table.get(load.id)  # noqa: E731
    remaining = spare_w
    changes = []
    for load in loads:
        st = states.setdefault(load.id, LoadState())
        hard = stop(load) if stop and st.on else None
        if hard:
            st.on, st.since, st.want_since = False, now_s, None
            st.note = f"uit: {hard}"
            changes.append((load, False))
            continue
        if remaining is None:
            fits, why = False, "geen actuele meting"
        elif st.on:
            fits = remaining >= load.power_w * KEEP_FRACTION
            why = f"nog {max(0, remaining):.0f} W over, {load.power_w:.0f} W nodig"
        else:
            fits = remaining >= load.power_w + ON_MARGIN_W
            why = f"{max(0, remaining):.0f} W over, {load.power_w:.0f} W nodig"
            reason = blocked(load) if fits else None
            if reason:
                fits, why = False, reason
        if remaining is not None and (st.on or fits):
            remaining -= load.power_w   # ook een wachtende verbruiker houdt zijn deel vast

        if fits == st.on:
            st.want_since = None
            st.note = "aan: er is genoeg zonnestroom over" if st.on else f"uit: {why}"
            continue
        if st.want_since is None:
            st.want_since = now_s
        waited = now_s - st.want_since
        held = now_s - st.since if st.since is not None else float("inf")
        if fits:
            wait = max(load.on_delay_s - waited, load.min_off_s - held)
            if wait > 0:
                st.note = (f"uit: wacht nog {_minutes(wait)} op "
                           + ("stabiel overschot" if load.on_delay_s - waited >= load.min_off_s - held
                              else "de minimale rusttijd"))
                continue
        else:
            wait = max(load.off_delay_s - waited, load.min_on_s - held)
            if wait > 0:
                st.note = (f"aan: {why}; "
                           + (f"blijft nog {_minutes(wait)} aan voor de minimale draaitijd"
                              if load.min_on_s - held >= load.off_delay_s - waited
                              else f"gaat uit als dat {_minutes(wait)} zo blijft"))
                continue
        st.on, st.since, st.want_since = fits, now_s, None
        st.note = "aan: er is genoeg zonnestroom over" if fits else f"uit: {why}"
        changes.append((load, fits))
        if fits and started:
            started(load)
    return changes
