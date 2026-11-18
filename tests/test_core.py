import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

# Achteraan het zoekpad, zodat modules van de integratie (sensor.py, number.py)
# nooit standaardmodules verdringen.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "custom_components", "huis_ems"))
from core import cycles as cy  # noqa: E402
from core import forecast as fc  # noqa: E402
from core.battery import (  # noqa: E402
    Battery, Slot, Unit, aggregate, describe, follow_meter, outlook, self_consumption)

CET = timezone(timedelta(hours=1))


class BatteryTest(unittest.TestCase):

    def units(self, soc_a=4.0, soc_b=3.5):
        return [Unit("a", "A", 8, 0.8, 8, 3.0, 3.0, 0.93, soc_a),
                Unit("b", "B", 7, 0.7, 7, 3.5, 3.5, 0.93, soc_b)]

    def test_aggregate(self):
        bat, soc = aggregate(self.units())
        self.assertAlmostEqual(bat.max_soc_kwh, 15)
        self.assertAlmostEqual(bat.max_charge_kw, 6.5)
        self.assertAlmostEqual(soc, 7.5)

    def test_virtual_battery_follows_meter(self):
        unit = self.units()[0]
        p, soc = follow_meter(unit, -5000, 60)
        self.assertEqual(p, 3000)
        self.assertGreater(soc, unit.soc_kwh)
        self.assertEqual(follow_meter(unit, 800, 60)[0], -800)
        unit.soc_kwh = 0.8
        self.assertEqual(follow_meter(unit, 800, 60)[0], 0)

    def test_self_consumption_stays_within_limits(self):
        bat = Battery(1.0, 8.0, 3.0, 3.0, 0.95, 0.95)
        slots = [Slot(0.25, 0.1, 2.0)] * 40 + [Slot(0.25, 1.0, 0.0)] * 60
        steps = self_consumption(slots, bat, 1.0)
        self.assertTrue(all(1.0 - 1e-9 <= s.soc_kwh <= 8.0 + 1e-9 for s in steps))
        self.assertTrue(all(s.accu_ac_kwh <= 3.0 * 0.25 + 1e-9 for s in steps))


class OutlookTest(unittest.TestCase):

    def day(self):
        # Nacht met verbruik, middag met veel zon.
        return [Slot(0.25, 0.15, 1.0 if 10 <= i / 4 < 15 else 0.0) for i in range(96)]

    def test_full_and_empty_moments(self):
        bat = Battery(0.8, 8.0, 3.0, 3.0, 0.95, 0.95)
        look = outlook(self.day(), bat, 0.8)
        self.assertGreaterEqual(look.full_at, 40)         # na 10:00
        self.assertLess(look.full_at, 60)                 # voor 15:00
        self.assertGreater(look.surplus_after_full_kwh, 1.0)
        self.assertEqual(look.empty_at, 0)                # leeg begonnen
        starts = [datetime(2026, 11, 16, 0, 0) + timedelta(minutes=15 * i) for i in range(96)]
        text = describe(look, starts)
        self.assertIn("Accu vol rond", text)
        self.assertIn("kWh zon over", text)

    def test_no_battery(self):
        look = outlook(self.day(), Battery(0, 0, 0, 0, 1, 1), 0.0)
        self.assertIsNone(look.full_at)
        self.assertIsNone(look.empty_at)
        self.assertAlmostEqual(look.surplus_kwh, 20 * (1.0 - 0.15), places=6)
        starts = [datetime(2026, 11, 16, 0, 0) + timedelta(minutes=15 * i) for i in range(96)]
        self.assertIn("zon over", describe(look, starts, has_battery=False))


def quarters(start, count):
    return [start + timedelta(minutes=15 * i) for i in range(count)], [0.25] * count


class CycleTest(unittest.TestCase):
    T0 = datetime(2026, 11, 2, 0, 0, tzinfo=CET)

    def runs(self, days=14, hour=19, minutes=110):
        states = []
        for d in range(days):
            start = self.T0 + timedelta(days=d, hours=hour)
            states += [(start, "run"), (start + timedelta(minutes=50), "pause"),
                       (start + timedelta(minutes=52), "run"),
                       (start + timedelta(minutes=minutes), "finished")]
        return states

    def test_cycles_from_states_merges_pauses(self):
        found = cy.cycles_from_states(self.runs(3), {"run", "pause"}, self.T0 + timedelta(days=3))
        self.assertEqual(len(found), 3)
        self.assertEqual(found[0].end - found[0].start, timedelta(minutes=110))
        ongoing = cy.cycles_from_states([(self.T0, "run")], {"run"}, self.T0 + timedelta(hours=1))
        self.assertTrue(ongoing[0].ongoing)

    def test_offline_counts_as_off(self):
        m = lambda x: self.T0 + timedelta(minutes=x)
        states = [(m(0), "unavailable"), (m(5), "ready"), (m(10), "run"), (m(100), "unavailable"),
                  (m(300), "ready"), (m(305), "run"), (m(380), "finished"), (m(390), "unavailable")]
        found = cy.cycles_from_states(states, {"run", "pause", "actionrequired"}, m(500))
        self.assertEqual([(c.start, c.end, c.ongoing) for c in found],
                         [(m(10), m(100), False), (m(305), m(380), False)])

    def test_learns_habit(self):
        found = cy.cycles_from_states(self.runs(14), {"run", "pause"}, self.T0 + timedelta(days=14))
        for c in found:
            c.kwh = 0.6
        habit = cy.learn("vaatwasser", found, 14)
        self.assertAlmostEqual(habit.rate_per_day, 1.0)
        self.assertEqual(habit.usual_hours(1), [19])
        self.assertGreater(habit.hour_p[19], 0.45)
        self.assertAlmostEqual(sum(habit.hour_p), 1.0)
        self.assertAlmostEqual(habit.kwh, (14 * 0.6 + 2 * 1.0) / 16)        # standaard 1,0 telt twee keer mee
        self.assertAlmostEqual(habit.duration_h, (14 * 110 / 60 + 2 * 3) / 16)

    def test_spread_keeps_energy(self):
        starts, hours = quarters(self.T0, 40)
        out = [0.0] * 40
        cy.spread(cy.PROFILES["wasmachine"], 0.9, self.T0 + timedelta(minutes=20), 2.0, starts, hours, out)
        self.assertAlmostEqual(sum(out), 0.9)
        self.assertEqual(out[0], 0.0)
        self.assertGreater(out[1], out[6])                 # opwarmen aan het begin

    def habit(self):
        found = cy.cycles_from_states(self.runs(14), {"run", "pause"}, self.T0 + timedelta(days=14))
        return cy.learn("vaatwasser", found, 14, default_kwh=1.0)

    def test_habit_today_and_tomorrow(self):
        habit = self.habit()
        now = self.T0 + timedelta(days=14, hours=8)
        starts, hours = quarters(now, 36 * 4)
        values, baseline = cy.forecast(habit, starts, hours, now)
        self.assertAlmostEqual(sum(baseline), sum(values), places=1)   # niets bekend: gewoonte
        self.assertAlmostEqual(sum(values[:64]), habit.kwh * 0.97, delta=0.15)  # vanavond
        # Vandaag al gedraaid: vanavond niets meer verwacht, morgen wel.
        done, _ = cy.forecast(habit, starts, hours, now, started_today=1)
        self.assertLess(sum(done[:64]), 0.05)
        self.assertGreater(sum(done[64:]), 0.3)

    def test_announced_cycle(self):
        habit = self.habit()
        now = self.T0 + timedelta(days=14, hours=8)
        starts, hours = quarters(now, 36 * 4)
        begin = now + timedelta(hours=2)                    # uitgestelde start om 10:00
        values, _ = cy.forecast(habit, starts, hours, now, [(begin, 2.0)], started_today=1)
        self.assertAlmostEqual(sum(values[8:16]), habit.kwh, places=6)
        self.assertLess(sum(values[16:64]), 0.05)           # vanavond niet nog een keer

    def test_energy_from_meter_and_residual(self):
        c = cy.Cycle(self.T0, self.T0 + timedelta(minutes=30))
        rows = [(self.T0 + timedelta(minutes=5 * i), 0.1) for i in range(-2, 9)]
        self.assertAlmostEqual(cy.cycle_energy(c, rows), 0.6)
        q = [(self.T0 + timedelta(minutes=15 * i), 300 + (2300 if i in (0, 1) else 0)) for i in range(-2, 4)]
        self.assertAlmostEqual(cy.residual_energy(c, q, {0: 300}), 1.15)
        self.assertIsNone(cy.residual_energy(c, q[:2], {0: 300}))   # cyclus niet gemeten


class ForecastTest(unittest.TestCase):
    T0 = datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc)

    def test_solcast_style_pv(self):
        attrs = {"detailedForecast": [{"period_start": (self.T0 + timedelta(minutes=30 * i)).isoformat(),
                                       "pv_estimate": 2.0} for i in range(4)]}
        series = fc.parse_series(attrs, ["detailedForecast"])
        starts = [self.T0 + timedelta(minutes=15 * i) for i in range(12)]
        kwh = fc.energy_per_slot(series, starts, [0.25] * 12, "kw")
        self.assertAlmostEqual(sum(kwh), 4.0)

    def test_wh_period_dict(self):
        attrs = {"wh_period": {(self.T0 + timedelta(hours=h)).isoformat(): 1000 for h in range(3)}}
        series = fc.parse_series(attrs)
        starts = [self.T0 + timedelta(minutes=15 * i) for i in range(12)]
        self.assertAlmostEqual(sum(fc.energy_per_slot(series, starts, [0.25] * 12, "wh_period")), 3.0)


def amsterdam(utc):
    """Lokale tijd in Nederland met de juiste offset, zonder tzdata (ontbreekt op Windows):
    zomertijd van de laatste zondag van maart tot de laatste zondag van oktober, 01:00 UTC."""
    def last_sunday(month):
        d = datetime(utc.year, month, 31, 1, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    summer = last_sunday(3) <= utc < last_sunday(10)
    return utc.astimezone(timezone(timedelta(hours=2 if summer else 1)))


class TimelineTest(unittest.TestCase):

    def check(self, now):
        from core.timeline import slot_starts
        starts, hours = slot_starts(now, 36 * 4)
        gaps = {(b - a).total_seconds() for a, b in zip(starts, starts[1:])}
        self.assertEqual(gaps, {900.0})
        self.assertAlmostEqual(hours[0], 8 / 60)          # zeven minuten van het kwartier zijn om
        self.assertTrue(all(h == 0.25 for h in hours[1:]))
        return [amsterdam(t) for t in starts]

    def test_autumn_clock_change(self):
        # 25 oktober 2026, 01:00 UTC: 03:00 zomertijd wordt 02:00 wintertijd.
        local = self.check(datetime(2026, 10, 24, 20, 7, tzinfo=timezone.utc))
        stamps = [t.strftime("%H:%M") for t in local if t.day == 25]
        self.assertEqual(stamps.count("02:30"), 2)        # het dubbele uur komt twee keer voor
        self.assertEqual(len({t.astimezone(timezone.utc) for t in local}), len(local))

    def test_spring_clock_change(self):
        # 29 maart 2026, 01:00 UTC: 02:00 wordt 03:00.
        local = self.check(datetime(2026, 3, 28, 20, 7, tzinfo=timezone.utc))
        self.assertNotIn("02:30", [t.strftime("%H:%M") for t in local if t.day == 29])


class LoadModelTest(unittest.TestCase):

    def test_recovers_temperature_effect(self):
        from core.loadmodel import fit, level_correction
        samples = []
        for day in range(60):
            temp = 15 - day * 0.25
            for hod in range(24):
                samples.append((hod, 0.3 + 0.1 * (hod == 18) + 0.05 * max(0, 14 - temp), temp))
        model = fit(samples)
        self.assertAlmostEqual(model.k, 0.05, places=3)
        self.assertAlmostEqual(model.t_limit, 14)
        self.assertAlmostEqual(model.predict(18, 4), 0.9, places=2)
        doubled = [(h, y * 2, t) for h, y, t in samples[-168:]]
        self.assertAlmostEqual(level_correction(model, doubled), 1.6)

    def test_without_temperature(self):
        from core.loadmodel import fit
        model = fit([(h, 0.4, None) for h in range(24)] * 3)
        self.assertEqual(model.k, 0)
        self.assertAlmostEqual(model.predict(5, None), 0.4)

    def test_slope_per_hour(self):
        # Een hottub die alleen overdag verwarmt: bij kou overdag meer, 's nachts niet.
        from core.loadmodel import fit
        samples = []
        for day in range(90):
            temp = 20 - day * 0.2
            for hod in range(24):
                heat = 0.04 * max(0, 21 - temp) if 9 <= hod < 18 else 0.0
                samples.append((hod, 0.02 + heat, temp))
        model = fit(samples)
        self.assertAlmostEqual(model.predict(3, 0), 0.02, places=3)
        self.assertAlmostEqual(model.predict(12, 1), 0.02 + 0.04 * 20, places=2)
        self.assertAlmostEqual(model.k * 24, 0.04 * 9, places=2)

    def _weeks(self, weekend_extra):
        import random
        rng = random.Random(1)
        samples = []
        for day in range(20 * 7):
            weekend = day % 7 >= 5
            for hod in range(24):
                y = 0.3 + (weekend_extra if weekend and 9 <= hod < 17 else 0.0) + rng.gauss(0, 0.03)
                samples.append((hod, max(0.0, y), None, weekend))
        return samples

    def test_weekend_profile_only_when_it_helps(self):
        from core.loadmodel import fit
        model = fit(self._weeks(0.5))
        self.assertTrue(model.daytype)
        self.assertAlmostEqual(model.predict(12, None, True), 0.8, places=1)
        self.assertAlmostEqual(model.predict(12, None, False), 0.3, places=1)
        self.assertFalse(fit(self._weeks(0.0)).daytype)

    def test_thermostat_factors(self):
        from core.loadmodel import LoadModel, thermostat_factors, thermostat_weight
        model = LoadModel([0.2] * 24, [0.0] * 24)
        samples = [(h % 24, 0.05, None, False, 0.0) for h in range(100)]
        samples += [(h % 24, 0.6, None, False, 1.5) for h in range(100)]
        idle, demand = thermostat_factors(model, samples)
        self.assertAlmostEqual(idle, 0.25)
        self.assertAlmostEqual(demand, 3.0)
        self.assertEqual(thermostat_factors(model, samples[:120]), (1.0, 1.0))
        self.assertAlmostEqual(thermostat_weight(4), 0.5)


class SeriesTest(unittest.TestCase):

    def hours(self, start, count, kwh):
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        return {base + timedelta(hours=start + i): kwh(i) for i in range(count)}

    def test_old_and_new_counter_of_one_inverter(self):
        from core.series import duplicate_groups, merge
        import math
        sun = lambda i: max(0.0, math.sin(math.pi * (i % 24 - 6) / 12)) * 3   # noqa: E731
        old = self.hours(0, 24 * 20, sun)               # oude integratie, 20 dagen
        new = self.hours(24 * 15, 24 * 10, lambda i: sun(i) * 1.02)  # nieuwe, 5 dagen samen
        growatt = self.hours(0, 24 * 25, lambda i: sun(i) * 0.4)
        groups = duplicate_groups([old, new, growatt])
        self.assertIn([0, 1], groups)
        self.assertIn([2], groups)
        merged = merge([old, new, growatt], groups)
        t = min(new) + timedelta(hours=12)              # beide tellers meten: één keer tellen
        self.assertAlmostEqual(merged[t], max(old[t], new[t]) + growatt[t])
        t_late = max(new)                               # alleen de nieuwe
        self.assertAlmostEqual(merged[t_late], new[t_late] + growatt[t_late])

    def test_quarter_delta(self):
        from core.series import delta
        ids = ["oud", "nieuw", "growatt"]
        before = {"nieuw": 100.0, "growatt": 50.0}             # oude teller bestaat niet meer
        after = {"nieuw": 100.4, "growatt": 50.1}
        self.assertAlmostEqual(delta(before, after, ids, []), 0.5)
        # Oud en nieuw als één meter herkend en allebei actief: één keer tellen.
        both_before = {"oud": 10.0, "nieuw": 100.0, "growatt": 50.0}
        both_after = {"oud": 10.38, "nieuw": 100.4, "growatt": 50.1}
        self.assertAlmostEqual(delta(both_before, both_after, ids, [["oud", "nieuw"]]), 0.5)
        self.assertIsNone(delta({}, {}, ids, []))
        self.assertIsNone(delta(before, {"nieuw": 99.0}, ["nieuw"], []))   # teller teruggezet

    def test_counters_after_each_other_are_simply_added(self):
        from core.series import duplicate_groups, merge
        old = self.hours(0, 48, lambda i: 1.0)
        new = self.hours(48, 48, lambda i: 1.0)
        groups = duplicate_groups([old, new])
        self.assertEqual(groups, [[0], [1]])            # geen overlap: niet te beoordelen, gewoon optellen
        self.assertEqual(len(merge([old, new], groups)), 96)


if __name__ == "__main__":
    unittest.main()
