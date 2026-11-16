"""Maakt een verzonnen P1-jaarexport om de simulator te testen.

Dit zijn GEEN meetgegevens. Gebruik je eigen HomeWizard-export voor echte cijfers.
Gebruik: python tools/make_demo_p1.py 2025 data/demo_p1.csv
"""
import csv
import math
import random
import sys
from datetime import datetime, timedelta


def main():
    year, out = int(sys.argv[1]), sys.argv[2]
    random.seed(1)
    t, end = datetime(year, 1, 1), datetime(year + 1, 1, 1)
    imp = exp = 0.0
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "Import T1 kWh", "Import T2 kWh", "Export T1 kWh", "Export T2 kWh"])
        while t < end:
            doy, hour = t.timetuple().tm_yday, t.hour + t.minute / 60
            season = math.cos(2 * math.pi * (doy - 15) / 365)        # +1 winter, -1 zomer
            base = 0.25 + 0.25 * math.exp(-((hour - 18.5) / 2.5) ** 2) + 0.1 * math.exp(-((hour - 7.5) / 1.5) ** 2)
            heatpump = max(0.0, 0.9 * season + 0.3) * (1.1 - 0.3 * math.sin(math.pi * hour / 24))
            daylen = 12 - 4.3 * season
            x = (hour - (12.7 - daylen / 2)) / daylen
            cloud = 0.25 + 0.75 * random.Random(doy).random()
            pv = 6.0 * (0.55 - 0.4 * season) * cloud * math.sin(math.pi * x) ** 1.5 if 0 < x < 1 else 0.0
            net = (base + heatpump) * random.uniform(0.7, 1.3) - pv
            if net > 0:
                imp += net / 4
            else:
                exp -= net / 4
            w.writerow([t.strftime("%Y-%m-%d %H:%M"), round(imp, 3), 0, round(exp, 3), 0])
            t += timedelta(minutes=15)
    print(f"Demo-export naar {out}: afname {imp:.0f} kWh, teruglevering {exp:.0f} kWh")


if __name__ == "__main__":
    main()
