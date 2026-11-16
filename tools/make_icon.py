"""Tekent het pictogram van Huis EMS: een huis met een bliksemschicht, en de zon erachter.

Gebruik: python tools/make_icon.py
Schrijft custom_components/huis_ems/brand/icon.png (256 px) en icon@2x.png (512 px). Home Assistant
en de Marketplace tonen die bij de integratie. Getekend op 2048 px en verkleind, voor gladde randen.
"""
import math
import pathlib

from PIL import Image, ImageDraw

S = 2048
SUN = (239, 159, 39, 255)      # amber
HOUSE = (15, 110, 86, 255)     # donkergroen-blauw
BOLT = (255, 255, 255, 255)
OUT = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "huis_ems" / "brand"


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Zon rechtsboven, deels achter het dak.
    cx, cy, r = 1460, 600, 250
    for k in range(8):
        a = math.radians(k * 45 - 22.5)
        x1, y1 = cx + math.cos(a) * (r + 70), cy + math.sin(a) * (r + 70)
        x2, y2 = cx + math.cos(a) * (r + 200), cy + math.sin(a) * (r + 200)
        d.line((x1, y1, x2, y2), fill=SUN, width=84)
        for x, y in ((x1, y1), (x2, y2)):
            d.ellipse((x - 42, y - 42, x + 42, y + 42), fill=SUN)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=SUN)

    # Huis: dak en romp als één vorm. De rand wordt met een dikke lijn en een rondje op elke
    # hoek getekend, zodat alle hoeken even rond zijn.
    rr = 70
    outline = [(260, 1060), (900, 440), (1540, 1060), (1400, 1060), (1400, 1720),
               (400, 1720), (400, 1060)]
    d.polygon(outline, fill=HOUSE)
    for a, b in zip(outline, outline[1:] + outline[:1]):
        d.line((*a, *b), fill=HOUSE, width=2 * rr)
    for x, y in outline:
        d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=HOUSE)

    # Bliksemschicht in het huis.
    d.polygon([(990, 960), (640, 1420), (880, 1420), (800, 1700), (1170, 1230),
               (930, 1230)], fill=BOLT)
    return img


def main() -> None:
    img = draw()
    box = img.getbbox()
    side = max(box[2] - box[0], box[3] - box[1])
    pad = int(side * 0.02)
    square = Image.new("RGBA", (side + 2 * pad, side + 2 * pad), (0, 0, 0, 0))
    square.paste(img.crop(box), (pad + (side - (box[2] - box[0])) // 2, pad + (side - (box[3] - box[1])) // 2))
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        square.resize((size, size), Image.LANCZOS).save(OUT / name, optimize=True)
        print(OUT / name)


if __name__ == "__main__":
    main()
