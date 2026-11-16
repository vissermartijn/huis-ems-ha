import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

# Achteraan het zoekpad, zodat modules van de integratie (switch.py, number.py)
# nooit standaardmodules verdringen.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "custom_components", "huis_ems"))
from core import forecast as fc  # noqa: E402
from core.optimizer import (  # noqa: E402
    LADEN, ONTLADEN, VASTHOUDEN, ZELFVERBRUIK, Battery, Slot, decide_mode, optimize,
    self_consumption)

BAT = Battery(min_soc_kwh=1.0, max_soc_kwh=8.0, wear_eur_per_kwh=0.0)


def flat(n, load=0.1, pv=0.0, buy=0.25, sell=0.05):
    return [Slot(0.25, load, pv, buy, sell) for _ in range(n)]


class OptimizerTest(unittest.TestCase):

    def test_soc_stays_within_limits(self):
        slots = flat(96)
        for i in range(30, 60):
            slots[i].pv_kwh = 1.0
        steps, _ = optimize(slots, BAT, 4.0, step_kwh=0.1)
        self.assertTrue(all(0.999 <= s.soc_kwh <= 8.001 for s in steps))
        self.assertTrue(all(abs(s.accu_ac_kwh) <= 0.75 + 1e-9 for s in steps))

    def test_charges_cheap_discharges_expensive(self):
        slots = flat(8, buy=0.10) + flat(8, buy=0.40)
        steps, cost = optimize(slots, BAT, 1.0, step_kwh=0.1, terminal_value=0.0)
        self.assertGreater(sum(s.accu_ac_kwh for s in steps[:8]), 0.5)
        self.assertLess(sum(s.accu_ac_kwh for s in steps[8:]), -0.5)
        self.assertLess(cost, sum(s.load_kwh * s.buy for s in slots))

    def test_no_grid_charge(self):
        slots = flat(8, buy=0.01) + flat(8, buy=0.40)
        steps, _ = optimize(slots, BAT, 1.0, step_kwh=0.1, allow_grid_charge=False)
        self.assertTrue(all(s.accu_ac_kwh <= 1e-9 for s in steps))

    def test_no_export(self):
        slots = flat(8, load=0.05, buy=0.30, sell=0.50)
        steps, _ = optimize(slots, BAT, 8.0, step_kwh=0.05, allow_export=False, terminal_value=0.0)
        self.assertTrue(all(s.grid_kwh >= -0.05 for s in steps))  # hooguit een rekenstap

    def test_never_worse_than_self_consumption(self):
        slots = flat(96)
        for i, s in enumerate(slots):
            s.pv_kwh = 0.6 if 36 <= i < 64 else 0.0
            s.buy = 0.35 if 68 <= i < 84 else 0.22
        _, greedy = self_consumption(slots, BAT, 1.0)
        _, best = optimize(slots, BAT, 1.0, step_kwh=0.05, terminal_value=0.0)
        self.assertLessEqual(best, greedy + 0.02)

    def test_modes(self):
        # Te weinig goedkope uren om alles te dekken: de lading moet bewaard
        # blijven voor de dure avond.
        slots = flat(2, buy=0.05) + flat(8, buy=0.20) + flat(8, load=0.3, buy=0.50)
        steps, _ = optimize(slots, BAT, 1.0, step_kwh=0.05, terminal_value=0.0)
        modes = [decide_mode(st, sl)[0] for st, sl in zip(steps, slots)]
        self.assertIn(LADEN, modes[:2])
        self.assertIn(VASTHOUDEN, modes[2:10])
        self.assertIn(ZELFVERBRUIK, modes[10:])
        export = flat(4, load=0.05, sell=0.60)
        steps, _ = optimize(export, BAT, 8.0, step_kwh=0.05, terminal_value=0.0)
        self.assertEqual(decide_mode(steps[0], export[0])[0], ONTLADEN)


class RefineTest(unittest.TestCase):

    def test_no_holding_for_a_cheaper_moment(self):
        from core.optimizer import refine_modes
        # Kort eerste slot met weinig verbruik op het duurste moment: niet vasthouden.
        slots = [Slot(0.05, 0.02, 0.0, 0.32, 0.05)] + flat(8, buy=0.24) + flat(8, buy=0.20)
        bat = Battery(min_soc_kwh=1.0, max_soc_kwh=8.0, wear_eur_per_kwh=0.0)
        steps, _ = optimize(slots, bat, 3.0, step_kwh=0.11, allow_export=False, terminal_value=0.0)
        modes = refine_modes(steps, slots, [decide_mode(st, sl) for st, sl in zip(steps, slots)],
                             allow_grid_charge=False, allow_export=False)
        self.assertEqual(modes[0][0], ZELFVERBRUIK)
        self.assertNotIn(ONTLADEN, [m for m, _ in modes])


class ForecastTest(unittest.TestCase):
    T0 = datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc)

    def test_nordpool_style_prices(self):
        attrs = {"raw_today": [{"start": (self.T0 + timedelta(hours=h)).isoformat(),
                                "end": "x", "value": 0.1 + h / 100} for h in range(24)]}
        series = fc.parse_series(attrs)
        self.assertEqual(len(series), 24)
        starts = [self.T0 + timedelta(minutes=15 * i) for i in range(120)]
        vals = fc.price_lookup(series, starts)
        self.assertEqual(vals[5], (0.11, fc.KNOWN))
        # Voorbij het einde: de prijs van 24 uur eerder, gemarkeerd als geschat.
        self.assertEqual(vals[100], (vals[4][0], fc.ESTIMATED))

    def _hourly(self, first_hour, hours, base=0.10):
        return [(self.T0 + timedelta(hours=h), base + h / 100) for h in range(first_hour, first_hour + hours)]

    def test_old_prices_are_not_reused(self):
        # Een reeks van zeven dagen geleden mag geen prijs voor nu opleveren.
        series = self._hourly(-7 * 24, 24)
        self.assertEqual(fc.price_lookup(series, [self.T0, self.T0 + timedelta(hours=1)]),
                         [(None, None), (None, None)])
        # Twee dagen oud: ook niet, want er wordt hooguit één etmaal herhaald.
        self.assertEqual(fc.price_lookup(self._hourly(-48, 24), [self.T0])[0], (None, None))

    def test_gap_in_today_is_not_bridged(self):
        series = self._hourly(0, 3) + self._hourly(8, 4)
        vals = fc.price_lookup(series, [self.T0 + timedelta(hours=h) for h in (2, 4, 6, 8)])
        self.assertEqual([origin for _, origin in vals], [fc.KNOWN, None, None, fc.KNOWN])
        # Vlak na een waarde geldt die nog (uurprijzen op een kwartierraster) ...
        self.assertEqual(fc.price_lookup(series, [self.T0 + timedelta(hours=2, minutes=45)])[0][1], fc.KNOWN)
        # ... maar niet het hele gat door.
        self.assertEqual(fc.price_lookup(series, [self.T0 + timedelta(hours=3, minutes=15)])[0][1], None)

    def test_missing_tomorrow_is_estimated_once(self):
        series = self._hourly(0, 24)
        vals = fc.price_lookup(series, [self.T0 + timedelta(hours=h) for h in (23, 24, 30, 47, 48)])
        self.assertEqual([origin for _, origin in vals],
                         [fc.KNOWN, fc.ESTIMATED, fc.ESTIMATED, fc.ESTIMATED, None])
        self.assertAlmostEqual(vals[2][0], 0.16)

    def test_negative_prices_are_valid(self):
        series = [(self.T0 + timedelta(hours=h), -0.05) for h in range(4)]
        self.assertEqual(fc.price_lookup(series, [self.T0 + timedelta(hours=1)])[0], (-0.05, fc.KNOWN))

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

    def test_bell_distribution_keeps_total(self):
        starts = [self.T0 + timedelta(minutes=15 * i) for i in range(96)]
        out = fc.bell_distribution(starts, [0.25] * 96, self.T0 + timedelta(hours=8),
                                   self.T0 + timedelta(hours=17), 12.0)
        self.assertAlmostEqual(sum(out), 12.0)
        self.assertEqual(out[0], 0.0)

    def test_load_profile(self):
        hist = [(self.T0 + timedelta(minutes=5 * i), 400.0 if i % 288 < 144 else 800.0)
                for i in range(288 * 3)]
        prof = fc.load_profile(hist)
        self.assertAlmostEqual(prof[10], 400.0)
        self.assertAlmostEqual(prof[80], 800.0)


class GridChargeMarginTest(unittest.TestCase):

    def test_threshold_blocks_thin_spreads(self):
        # 0,20 -> 0,26: na 86% rendement blijft 0,26 * 0,86 - 0,20 = 2 cent over.
        slots = flat(8, buy=0.20) + flat(8, load=0.3, buy=0.26)
        bat = Battery(min_soc_kwh=1.0, max_soc_kwh=8.0, wear_eur_per_kwh=0.0)
        charged = lambda margin: sum(s.accu_ac_kwh for s in optimize(
            slots, bat, 1.0, step_kwh=0.1, terminal_value=0.0, grid_charge_margin=margin)[0][:8])
        self.assertGreater(charged(0.0), 1.0)
        self.assertLess(charged(0.03), 0.01)

    def test_threshold_keeps_wide_spreads(self):
        slots = flat(8, buy=0.10) + flat(8, load=0.3, buy=0.40)
        bat = Battery(min_soc_kwh=1.0, max_soc_kwh=8.0, wear_eur_per_kwh=0.0)
        steps, _ = optimize(slots, bat, 1.0, step_kwh=0.1, terminal_value=0.0, grid_charge_margin=0.03)
        self.assertGreater(sum(s.accu_ac_kwh for s in steps[:8]), 1.0)

    def test_solar_charging_is_not_penalised(self):
        slots = flat(8, load=0.0, pv=0.5, buy=0.20, sell=0.05) + flat(8, load=0.3, buy=0.21)
        bat = Battery(min_soc_kwh=1.0, max_soc_kwh=8.0, wear_eur_per_kwh=0.0)
        steps, _ = optimize(slots, bat, 1.0, step_kwh=0.1, terminal_value=0.0,
                            allow_grid_charge=False, grid_charge_margin=0.10)
        self.assertGreater(sum(s.accu_ac_kwh for s in steps[:8]), 1.0)


class FullChargeTest(unittest.TestCase):
    BAT = Battery(min_soc_kwh=1.0, max_soc_kwh=8.0, wear_eur_per_kwh=0.0)

    def test_picks_cheapest_moment(self):
        slots = flat(8, buy=0.30) + flat(8, buy=0.10) + flat(8, buy=0.30)
        steps, _ = optimize(slots, self.BAT, 6.0, step_kwh=0.1, terminal_value=0.0, require_full=True)
        self.assertTrue(any(s.soc_kwh >= 7.999 for s in steps))
        self.assertAlmostEqual(sum(s.accu_ac_kwh for s in steps[:8] if s.accu_ac_kwh > 0), 0)
        self.assertGreater(sum(s.accu_ac_kwh for s in steps[8:16]), 2.0)

    def test_uses_solar_when_grid_charging_is_off(self):
        slots = flat(8) + flat(16, load=0.0, pv=0.5) + flat(8)
        steps, _ = optimize(slots, self.BAT, 2.0, step_kwh=0.1, terminal_value=0.0,
                            allow_grid_charge=False, require_full=True)
        self.assertTrue(any(s.soc_kwh >= 7.999 for s in steps))
        # Niet meer van het net dan het huis zelf verbruikt (op één rekenstap na).
        self.assertTrue(all(st.grid_kwh <= sl.load_kwh + 0.11 for st, sl in zip(steps, slots)))

    def test_reports_when_impossible(self):
        steps, cost = optimize(flat(8), self.BAT, 2.0, step_kwh=0.1, allow_grid_charge=False,
                               require_full=True)
        self.assertIsNone(steps)

    def test_already_full_needs_nothing(self):
        steps, _ = optimize(flat(8), self.BAT, 8.0, step_kwh=0.1, terminal_value=0.0, require_full=True)
        self.assertTrue(all(s.accu_ac_kwh <= 1e-9 for s in steps))


class FleetTest(unittest.TestCase):

    def units(self, soc_a=4.0, soc_b=3.5):
        from core.fleet import Unit
        return [Unit("a", "A", 8, 0.8, 8, 3.0, 3.0, 0.93, soc_a),
                Unit("b", "B", 7, 0.7, 7, 3.5, 3.5, 0.93, soc_b)]

    def test_aggregate(self):
        from core.fleet import aggregate
        bat, soc = aggregate(self.units(), 0.02)
        self.assertAlmostEqual(bat.max_soc_kwh, 15)
        self.assertAlmostEqual(bat.max_charge_kw, 6.5)
        self.assertAlmostEqual(soc, 7.5)

    def test_virtual_response(self):
        from core.fleet import respond
        unit = self.units()[0]
        p, soc = respond(ZELFVERBRUIK, 0, unit, -5000, 60)
        self.assertEqual(p, 3000)
        self.assertGreater(soc, unit.soc_kwh)
        p, _ = respond(ZELFVERBRUIK, 0, unit, 800, 60)
        self.assertEqual(p, -800)
        unit.soc_kwh = 0.8
        p, _ = respond(ZELFVERBRUIK, 0, unit, 800, 60)
        self.assertEqual(p, 0)
        self.assertEqual(respond(VASTHOUDEN, 0, unit, 800, 60)[0], 0)


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
        # Een unieke prijs per UTC-kwartier moet op precies dat kwartier terechtkomen,
        # ook als de bron lokale tijd met een wisselende offset geeft.
        series = [(amsterdam(t), i / 1000) for i, t in enumerate(starts)]
        local = [amsterdam(t) for t in starts]
        vals = fc.price_lookup(series, local)
        self.assertEqual([v for v, _ in vals], [i / 1000 for i in range(len(starts))])
        return local

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


class Stage2Test(unittest.TestCase):

    def setUp(self):
        from core.stage2 import Load
        self.boiler = Load("b", "Boiler", 2000, on_delay_s=300, off_delay_s=120,
                           min_on_s=600, min_off_s=600)
        self.heater = Load("h", "Kachel", 1000, on_delay_s=60, off_delay_s=60,
                           min_on_s=60, min_off_s=60)

    def run_steps(self, spare, loads=None, states=None, start=0, step=10, blocked=None):
        from core.stage2 import decide
        states = {} if states is None else states
        log = []
        for i, value in enumerate(spare):
            now = start + i * step
            for load, on in decide(loads or [self.boiler], states, value, now, blocked):
                log.append((now, load.id, on))
        return states, log

    def test_waits_for_stable_surplus(self):
        states, log = self.run_steps([2500] * 40)
        self.assertEqual(log, [(300, "b", True)])

    def test_cloud_does_not_switch(self):
        # Een wolk van een minuut tijdens het wachten begint de wachttijd opnieuw;
        # eenmaal aan gaat hij er door zo'n wolk niet af.
        states, log = self.run_steps([2500] * 20 + [0] * 6 + [2500] * 40 + [0] * 6 + [2500] * 30)
        self.assertEqual(log, [(260 + 300, "b", True)])
        self.assertTrue(states["b"].on)

    def test_minimum_run_time(self):
        states, log = self.run_steps([2500] * 31 + [0] * 100)
        self.assertEqual(log, [(300, "b", True), (900, "b", False)])
        self.assertIn("minimale draaitijd", self.run_steps(
            [2500] * 31 + [0] * 20)[0]["b"].note)

    def test_minimum_off_time(self):
        states, log = self.run_steps([2500] * 31 + [0] * 61 + [2500] * 100)
        self.assertEqual([entry[2] for entry in log], [True, False, True])
        self.assertGreaterEqual(log[2][0] - log[1][0], 600)

    def test_priority_order_and_reservation(self):
        loads = [self.boiler, self.heater]
        # 2500 W over: de boiler gaat voor en houdt zijn deel vast terwijl hij wacht.
        states, log = self.run_steps([2500] * 40, loads)
        self.assertEqual(log, [(300, "b", True)])
        # 3200 W over: allebei.
        states, log = self.run_steps([3200] * 40, loads)
        self.assertEqual(sorted(entry[1] for entry in log), ["b", "h"])
        # 1200 W over: de boiler past niet, de kleinere kachel wel.
        states, log = self.run_steps([1200] * 40, loads)
        self.assertEqual(log, [(60, "h", True)])

    def test_blocked_and_no_measurement(self):
        states, log = self.run_steps([2500] * 40, blocked={"b": "accu nog niet vol"})
        self.assertEqual(log, [])
        self.assertIn("accu nog niet vol", states["b"].note)
        states, log = self.run_steps([2500] * 31 + [None] * 100)
        self.assertEqual([entry[2] for entry in log], [True, False])


class StopTest(unittest.TestCase):

    def test_hard_stop_ignores_minimum_run_time(self):
        from core.stage2 import Load, LoadState, decide
        load = Load("b", "Boiler", 2000, on_delay_s=0, off_delay_s=600, min_on_s=3600, min_off_s=0)
        states = {}
        decide([load], states, 3000, 0)
        self.assertTrue(states["b"].on)
        changes = decide([load], states, 3000, 10, stop=lambda ld: "na 16:00")
        self.assertEqual(changes, [(load, False)])
        self.assertEqual(states["b"].note, "uit: na 16:00")
        self.assertIsInstance(states["b"], LoadState)


class OutlookTest(unittest.TestCase):

    def day(self):
        # Nacht met verbruik, middag met veel zon, avond duur.
        slots = []
        for i in range(96):
            hour = i / 4
            pv = 1.0 if 10 <= hour < 15 else 0.0
            buy = 0.40 if 17 <= hour < 21 else 0.20
            slots.append(Slot(0.25, 0.15, pv, buy, 0.05))
        return slots

    def test_full_and_empty_moments(self):
        from core.balance import describe, outlook
        bat = Battery(0.8, 8.0, 3.0, 3.0, 0.95, 0.95, 0.0)
        look = outlook(self.day(), bat, 0.8)
        self.assertIsNotNone(look.full_at)
        self.assertGreaterEqual(look.full_at, 40)         # na 10:00
        self.assertLess(look.full_at, 60)                 # voor 15:00
        self.assertGreater(look.surplus_after_full_kwh, 1.0)
        self.assertEqual(look.empty_at, 0)                # leeg begonnen
        self.assertAlmostEqual(look.expensive_from, 0.40)
        starts = [datetime(2026, 11, 16, 0, 0) + timedelta(minutes=15 * i) for i in range(96)]
        text = describe(look, starts)
        self.assertIn("Accu vol rond", text)
        self.assertIn("kWh zon over", text)

    def test_no_battery(self):
        from core.balance import describe, outlook
        look = outlook(self.day(), Battery(0, 0, 0, 0, 1, 1, 0), 0.0)
        self.assertIsNone(look.full_at)
        self.assertIsNone(look.empty_at)
        self.assertAlmostEqual(look.surplus_kwh, 20 * (1.0 - 0.15), places=6)
        starts = [datetime(2026, 11, 16, 0, 0) + timedelta(minutes=15 * i) for i in range(96)]
        self.assertIn("zon over", describe(look, starts, has_battery=False))

    def test_export_ahead(self):
        from core.balance import export_ahead_w, outlook
        slots = self.day()
        look = outlook(slots, Battery(0, 0, 0, 0, 1, 1, 0), 0.0)
        self.assertAlmostEqual(export_ahead_w(look.steps[40:], slots[40:], 4), 3400)
        self.assertEqual(export_ahead_w(look.steps, slots, 4), 0)


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


class BufferTest(unittest.TestCase):

    def setUp(self):
        from core.buffers import SWITCH, THERMOSTAT, Buffer
        self.floor = Buffer("v", "Vloer", 1500, min_on_s=3600, kind=THERMOSTAT, min_soc_pct=90,
                            start_hour=10, end_hour=16, max_temp=22, need_forecast=True)
        self.boiler = Buffer("b", "Boiler", 2000, kind=SWITCH, min_soc_pct=95, max_temp=60,
                             rest_after_s=900)

    def ctx(self, **kw):
        from core.buffers import Context
        base = dict(hour=12, soc_pct=97, temps={"v": 20.5, "b": 48}, forecast_w={"v": 2500})
        base.update(kw)
        return Context(**base)

    def test_start_conditions(self):
        from core.buffers import start_block
        self.assertIsNone(start_block(self.floor, self.ctx(), 0))
        self.assertIn("alleen tussen 10:00 en 16:00", start_block(self.floor, self.ctx(hour=9.5), 0))
        self.assertIn("eerst tot 90%", start_block(self.floor, self.ctx(soc_pct=80), 0))
        self.assertIn("grens 22.0", start_block(self.floor, self.ctx(temps={"v": 22.1}), 0))
        self.assertIn("verwacht overschot", start_block(self.floor, self.ctx(forecast_w={"v": 600}), 0))
        self.assertIn("niet bereikbaar", start_block(self.floor, self.ctx(unavailable={"v": "niet bereikbaar"}), 0))
        self.assertIn("laadtoestand", start_block(self.boiler, self.ctx(soc_pct=None, soc_problem="is onbekend"), 0))
        # Zonder accu geldt geen voorwaarde op de lading.
        self.assertIsNone(start_block(self.boiler, self.ctx(soc_pct=None), 0))

    def test_stop_conditions(self):
        from core.buffers import stop_reason
        self.assertIsNone(stop_reason(self.floor, self.ctx(), 0))
        self.assertIn("na 16:00", stop_reason(self.floor, self.ctx(hour=16), 0))
        self.assertIn("bereikt", stop_reason(self.boiler, self.ctx(temps={"b": 60}), 0))
        # Een beetje zakken mag (accu dekt een wolkje niet, maar de meting kan schommelen).
        self.assertIsNone(stop_reason(self.boiler, self.ctx(soc_pct=93), 0))
        self.assertIn("gezakt", stop_reason(self.boiler, self.ctx(soc_pct=91), 0))

    def test_idle_detection(self):
        from core.buffers import REST_SECONDS, IdleWatch, start_block, stop_reason
        watch = IdleWatch()
        self.assertIsNone(watch.update(self.boiler, True, 1950, 0))
        self.assertIsNone(watch.update(self.boiler, True, 5, 100))
        until = watch.update(self.boiler, True, 5, 1000)
        self.assertEqual(until, 1000 + REST_SECONDS)
        ctx = self.ctx(resting={"b": until})
        self.assertIn("vat op temperatuur", stop_reason(self.boiler, ctx, 1001))
        self.assertIn("later opnieuw", start_block(self.boiler, ctx, 1001))
        self.assertIsNone(start_block(self.boiler, ctx, until + 1))
        # Zonder meting of uitgeschakeld: niet bewaken.
        self.assertIsNone(watch.update(self.boiler, True, None, 5000))
        self.assertIsNone(watch.update(self.floor, True, 0, 9000))

    def test_spare_keeps_battery_first(self):
        from core.buffers import spare_w
        # Accu laadt 2 kW en er gaat niets naar het net: niets over voor buffers.
        self.assertEqual(spare_w(0, 2000, 0), 0)
        # Accu vol, 2,5 kW naar het net.
        self.assertEqual(spare_w(-2500, 0, 0), 2500)
        # Boiler draait (2 kW), wolk: de accu springt bij met 1,5 kW.
        self.assertEqual(spare_w(0, -1500, 2000), 500)

    def test_boost_target(self):
        from core.buffers import boost_target
        self.assertEqual(boost_target(20.0, 1.0), 21.0)
        self.assertEqual(boost_target(20.3, 1.0, 0.5), 21.5)
        self.assertEqual(boost_target(19.9, 0.5, 0.1), 20.4)


if __name__ == "__main__":
    unittest.main()
