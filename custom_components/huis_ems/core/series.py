"""kWh-tellers per uur samenvoegen, ook als dezelfde meter onder twee namen staat.

Bij een overstap naar een andere integratie (bijvoorbeeld een nieuwe Modbus-koppeling
voor de omvormer) krijgt de teller een nieuwe naam en begint zijn historie opnieuw.
Laat je de oude én de nieuwe teller allebei staan, dan loopt de historie door. Zolang
ze tegelijk meten, zouden ze dubbel tellen; daarom worden tellers die in dezelfde uren
vrijwel hetzelfde meten als één meter behandeld.
"""

MIN_KWH = 0.05        # uren met minder opwek zeggen niets over gelijkheid
TOLERANCE = 0.15      # zoveel mogen twee tellers van dezelfde meter verschillen
MIN_HOURS = 24        # zo veel gezamenlijke uren met opwek zijn nodig om te oordelen
MATCH_SHARE = 0.9     # in zo'n deel van die uren moeten ze gelijk zijn


def _same(a, b):
    shared = [(a[t], b[t]) for t in a.keys() & b.keys() if a[t] >= MIN_KWH and b[t] >= MIN_KWH]
    if len(shared) < MIN_HOURS:
        return False
    close = sum(1 for x, y in shared if abs(x - y) <= TOLERANCE * max(x, y))
    return close >= MATCH_SHARE * len(shared)


def duplicate_groups(series):
    """Groepen van indexen die dezelfde meter zijn; een losse teller is een groep van één."""
    parent = list(range(len(series)))

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i

    for i in range(len(series)):
        for j in range(i + 1, len(series)):
            if root(i) != root(j) and _same(series[i], series[j]):
                parent[root(j)] = root(i)
    groups = {}
    for i in range(len(series)):
        groups.setdefault(root(i), []).append(i)
    return list(groups.values())


def delta(before, after, ids, groups):
    """Opwek tussen twee standen van de tellers ({id: kWh}).

    groups: lijsten van ids die dezelfde meter zijn; daarbinnen telt de teller die
    werkt. Een teller zonder waarde (bijvoorbeeld van een verwijderde integratie) telt
    niet mee. Zonder één werkende teller: None.
    """
    known = {i for g in groups for i in g}
    todo = [g for g in groups if any(i in ids for i in g)] + [[i] for i in ids if i not in known]
    total, measured = 0.0, False
    for group in todo:
        steps = [after[i] - before[i] for i in group if i in before and i in after and after[i] >= before[i]]
        if steps:
            total += max(steps)
            measured = True
    return total if measured else None


def merge(series, groups=None):
    """Per uur de som over de meters; binnen een groep telt de hoogste waarde één keer."""
    groups = groups if groups is not None else duplicate_groups(series)
    out = {}
    for group in groups:
        hours = set().union(*(series[i].keys() for i in group))
        for t in hours:
            out[t] = out.get(t, 0.0) + max(series[i].get(t, 0.0) for i in group)
    return out
