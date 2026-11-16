"""Haalt Nederlandse day-ahead-prijzen op (energy-charts.info, CC BY 4.0).

Gebruik: python tools/fetch_prices.py 2025-01-01 2025-12-31 data/prijzen.csv
Uitvoer: CSV met utc (ISO) en eur_per_kwh, per kwartier of uur zoals geleverd.
"""
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone


def fetch(start, end):
    url = f"https://api.energy-charts.info/price?bzn=NL&start={start}&end={end}"
    req = urllib.request.Request(url, headers={"User-Agent": "huis-ems"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as err:  # de dienst begrenst het aantal verzoeken
            if err.code != 429 or attempt == 5:
                raise
            time.sleep(20 * (attempt + 1))
    return [(t, p) for t, p in zip(data["unix_seconds"], data["price"]) if p is not None]


def main():
    start, end, out = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2]), sys.argv[3]
    rows = {}
    cur = start
    while cur <= end:  # per kwartaal ophalen houdt de antwoorden klein
        nxt = min(end, cur + timedelta(days=90))
        rows.update(fetch(cur.isoformat(), nxt.isoformat()))
        cur = nxt + timedelta(days=1)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["utc", "eur_per_kwh"])
        for ts in sorted(rows):
            writer.writerow([datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             round(rows[ts] / 1000.0, 5)])
    print(f"{len(rows)} prijzen naar {out}")


if __name__ == "__main__":
    main()
