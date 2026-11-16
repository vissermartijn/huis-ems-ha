"""Meet de rekenlast van het EMS: vooruitblik, leren en de stap per meting.

Gebruik: python tools/benchmark.py
Draai dit op het apparaat waar Home Assistant op staat om te zien wat het
daar kost; op een pc is het een ondergrens.
"""
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "custom_components", "huis_ems"))
from core.balance import outlook  # noqa: E402
from core.buffers import SWITCH, THERMOSTAT, Buffer, Context, start_block, stop_reason  # noqa: E402
from core.loadmodel import fit  # noqa: E402
from core.optimizer import Battery, Slot  # noqa: E402
from core.stage2 import decide  # noqa: E402


def timed(fn, repeat=3):
    best = None
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        took = time.perf_counter() - start
        best = took if best is None else min(best, took)
    return best


def slots(hours=36):
    out = []
    for i in range(hours * 4):
        h = (i / 4) % 24
        pv = max(0.0, math.sin(math.pi * (h - 8) / 8)) * 0.8 if 8 < h < 16 else 0.0
        buy = 0.25 + (0.12 if 17 <= h < 21 else -0.05 if 11 <= h < 15 else 0)
        out.append(Slot(0.25, 0.15 + 0.2 * (17 <= h < 22), pv, buy, buy - 0.15))
    return out


def main():
    rng = random.Random(1)
    print(f"Python {sys.version.split()[0]} op {os.uname().machine if hasattr(os, 'uname') else os.environ.get('PROCESSOR_ARCHITECTURE')}")
    bat = Battery(min_soc_kwh=0.8, max_soc_kwh=8.0, max_charge_kw=3.0, max_discharge_kw=3.0)
    print(f"vooruitblik 36 uur: {timed(lambda: outlook(slots(), bat, 2.4)) * 1000:.1f} ms")

    year = [(i % 24, max(0.0, 0.3 + 0.03 * max(0, 14 - (10 - 8 * math.cos(i / 8760 * 6.283)))
                         + rng.gauss(0, 0.1)), 10 - 8 * math.cos(i / 8760 * 6.283), (i // 24) % 7 >= 5)
            for i in range(8760)]
    print(f"leren uit een jaar uurwaarden, per onderdeel: {timed(lambda: fit(year)) * 1000:.0f} ms")

    states = {}
    bufs = [Buffer("v", "Vloer", 1500, kind=THERMOSTAT, start_hour=10, end_hour=16, need_forecast=True),
            Buffer("b", "Boiler", 2000, kind=SWITCH, max_temp=60)]
    ctx = Context(hour=12, soc_pct=97, temps={"v": 20.5, "b": 50}, forecast_w={"v": 2000})

    def tick():
        for i in range(1000):
            decide(bufs, states, rng.gauss(1500, 800), float(i),
                   blocked=lambda b: start_block(b, ctx, float(i)),
                   stop=lambda b: stop_reason(b, ctx, float(i)))
    print(f"stap per meting (twee buffers): {timed(tick) * 1000:.0f} microseconde")


if __name__ == "__main__":
    main()
