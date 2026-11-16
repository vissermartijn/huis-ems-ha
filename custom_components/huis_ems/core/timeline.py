"""De tijdas van de planner: kwartieren op een UTC-as.

Kwartieren worden in UTC gemaakt en vergeleken en pas voor weergave en voor
het uur van de dag omgezet naar lokale tijd. Rekenen met lokale tijd gaat
rond de overgang naar zomer- of wintertijd een uur mis.
"""
from datetime import timedelta, timezone


def slot_starts(now_utc, count, slot_min=15):
    """Geeft ([begin van elk kwartier in UTC], [duur in uren]).

    Het eerste kwartier is het lopende; zijn duur is de tijd die er nog van
    over is (minstens een minuut).
    """
    now_utc = now_utc.astimezone(timezone.utc)
    first = now_utc.replace(minute=now_utc.minute - now_utc.minute % slot_min, second=0, microsecond=0)
    starts = [first + timedelta(minutes=slot_min * i) for i in range(count)]
    hours = [slot_min / 60] * count
    if count:
        left = (first + timedelta(minutes=slot_min) - now_utc).total_seconds() / 3600
        hours[0] = max(1 / 60, left)
    return starts, hours
