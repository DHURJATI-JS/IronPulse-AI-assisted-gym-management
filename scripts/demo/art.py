"""Synthetic images for the demo dataset (Pillow + numpy) - avatars, gym interiors, SPECIMEN ID cards, gym logos and
studio-style product shots. Everything is drawn from shapes and labelled DEMO / SPECIMEN; no real people or brands.

Only scripts/demo/loader.py uses this module; the app never imports it.
"""

from __future__ import annotations

import io
from datetime import date
from functools import lru_cache
from typing import Any

PALETTES = [
    ((10, 132, 255), (94, 92, 230)), ((191, 90, 242), (255, 55, 95)), ((48, 209, 88), (10, 132, 255)),
    ((255, 159, 10), (255, 69, 58)), ((100, 210, 255), (94, 92, 230)), ((255, 214, 10), (255, 159, 10)),
]
NUTRITION_FIELDS = (  # same rows as the product page's nutrition table
    ("calories", "Energy", "kcal"), ("protein_g", "Protein", "g"), ("carbs_g", "Carbohydrates", "g"),
    ("sugar_g", "of which sugars", "g"), ("fat_g", "Fat", "g"), ("caffeine_mg", "Caffeine", "mg"),
)
_FONT_FILES = (
    "/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf", "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)


def _png(im: Any) -> bytes:
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


@lru_cache(maxsize=64)
def _font(size: int) -> Any:
    from PIL import ImageFont

    for name in _FONT_FILES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _gradient(size: tuple[int, int], c1: tuple[int, int, int], c2: tuple[int, int, int], diagonal: bool = True) -> Any:
    import numpy as np
    from PIL import Image

    w, h = size
    x = np.linspace(0, 1, w)[None, :]
    y = np.linspace(0, 1, h)[:, None]
    t = (x * 0.6 + y * 0.4) if diagonal else np.broadcast_to(y, (h, w))
    t = np.clip(t, 0, 1)[..., None]
    arr = (np.array(c1)[None, None, :] * (1 - t) + np.array(c2)[None, None, :] * t).astype("uint8")
    return Image.fromarray(arr, "RGB")


def _soft_ellipse(size: int, box: tuple[float, float, float, float], color: tuple[int, int, int], shade: float = 0.22) -> Any:
    """RGBA layer with an ellipse lit from the top-left (radial shading) - gives a soft 3-D look."""
    import numpy as np
    from PIL import Image

    x0, y0, x1, y1 = box
    yy, xx = np.mgrid[0:size, 0:size].astype("float32")
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx, ry = (x1 - x0) / 2, (y1 - y0) / 2
    inside = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1.0
    lx, ly = cx - rx * 0.35, cy - ry * 0.45  # light source
    dist = np.sqrt(((xx - lx) / (rx * 1.6)) ** 2 + ((yy - ly) / (ry * 1.6)) ** 2)
    factor = np.clip(1.08 - shade * dist, 0.55, 1.12)[..., None]
    rgb = np.clip(np.array(color, dtype="float32")[None, None, :] * factor, 0, 255).astype("uint8")
    alpha = (inside * 255).astype("uint8")
    return Image.fromarray(np.dstack([rgb, alpha]), "RGBA")


# ------------------------------------------------------------------------------------------------ people
def portrait_png(name: str, idx: int) -> bytes:
    """Memoji-style synthetic portrait (soft shading, friendly face) - clearly not a real photo."""
    from PIL import Image, ImageDraw, ImageFilter

    c1, c2 = PALETTES[idx % len(PALETTES)]
    s = 768
    im = _gradient((s, s), c1, c2).convert("RGBA")
    skin = [(241, 194, 167), (224, 172, 105), (198, 134, 66), (141, 85, 36), (255, 219, 172)][idx % 5]
    hair = [(32, 30, 34), (78, 50, 28), (126, 84, 42), (22, 22, 44), (170, 120, 60)][(idx // 5) % 5]
    shirt = [(28, 28, 32), (245, 245, 247), (10, 132, 255), (255, 159, 10), (48, 209, 88)][(idx + 2) % 5]
    shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse((s * 0.2, s * 0.7, s * 0.8, s * 1.3), fill=(0, 0, 0, 90))
    im.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(28)))
    im.alpha_composite(_soft_ellipse(s, (s * 0.17, s * 0.68, s * 0.83, s * 1.30), shirt, 0.18))  # shoulders
    neck = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(neck).rounded_rectangle((s * 0.42, s * 0.52, s * 0.58, s * 0.72), radius=30,
                                           fill=tuple(int(c * 0.88) for c in skin) + (255,))
    im.alpha_composite(neck)
    for ex in (0.285, 0.715):  # ears
        im.alpha_composite(_soft_ellipse(s, (s * (ex - 0.045), s * 0.36, s * (ex + 0.045), s * 0.46), skin, 0.2))
    im.alpha_composite(_soft_ellipse(s, (s * 0.29, s * 0.16, s * 0.71, s * 0.64), skin, 0.26))  # head
    d = ImageDraw.Draw(im)
    style = (idx // 3) % 3  # hair styles
    if style == 0:
        d.pieslice((s * 0.27, s * 0.11, s * 0.73, s * 0.50), 180, 360, fill=hair)
    elif style == 1:
        d.pieslice((s * 0.26, s * 0.10, s * 0.74, s * 0.56), 170, 370, fill=hair)
        d.ellipse((s * 0.42, s * 0.05, s * 0.60, s * 0.20), fill=hair)  # bun
    else:
        d.pieslice((s * 0.26, s * 0.12, s * 0.74, s * 0.52), 180, 360, fill=hair)
        d.rounded_rectangle((s * 0.25, s * 0.30, s * 0.32, s * 0.62), radius=20, fill=hair)
        d.rounded_rectangle((s * 0.68, s * 0.30, s * 0.75, s * 0.62), radius=20, fill=hair)
    eye = (40, 34, 40)
    for ex in (0.42, 0.58):
        d.ellipse((s * (ex - 0.022), s * 0.39, s * (ex + 0.022), s * 0.435), fill=eye)
        d.ellipse((s * (ex - 0.006), s * 0.395, s * (ex + 0.006), s * 0.408), fill=(255, 255, 255))
    d.arc((s * 0.43, s * 0.44, s * 0.57, s * 0.54), 20, 160, fill=(150, 70, 70), width=6)  # smile
    blush = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    bd = ImageDraw.Draw(blush)
    for ex in (0.37, 0.63):
        bd.ellipse((s * (ex - 0.04), s * 0.46, s * (ex + 0.04), s * 0.5), fill=(255, 120, 120, 60))
    im.alpha_composite(blush.filter(ImageFilter.GaussianBlur(6)))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((s * 0.04, s * 0.90, s * 0.40, s * 0.97), radius=18, fill=(0, 0, 0, 110))
    d.text((s * 0.065, s * 0.912), "DEMO AVATAR", fill=(255, 255, 255), font=_font(26))
    return _png(im.convert("RGB").resize((512, 512), Image.LANCZOS))


def specimen_id_png(name: str, dob: date, number: str, country: str = "IN") -> bytes:
    from PIL import ImageDraw

    im = _gradient((1000, 630), (232, 240, 255), (205, 222, 255))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((20, 20, 980, 610), radius=36, outline=(10, 132, 255), width=6)
    d.text((60, 50), "SPECIMEN - DEMO IDENTITY CARD", fill=(10, 60, 140), font=_font(40))
    d.rounded_rectangle((60, 140, 300, 440), radius=24, fill=(160, 180, 210))
    d.text((340, 150), f"Name: {name}", fill=(20, 20, 30), font=_font(38))
    d.text((340, 220), f"DOB: {dob:%d/%m/%Y}", fill=(20, 20, 30), font=_font(38))
    d.text((340, 290), f"ID No: {number}", fill=(20, 20, 30), font=_font(38))
    d.text((340, 360), f"Country: {country}", fill=(20, 20, 30), font=_font(38))
    d.text((60, 520), "NOT A REAL DOCUMENT", fill=(255, 69, 58), font=_font(44))
    return _png(im)


# ------------------------------------------------------------------------------------------------ gyms
def gym_photo_png(kind: str, idx: int, title: str) -> bytes:
    """Illustrated gym interior: floor, windows, racks / treadmills / benches."""
    from PIL import ImageDraw

    w, h = 1600, 1000
    wall_top, wall_bottom = [((28, 28, 34), (52, 52, 64)), ((235, 236, 240), (205, 208, 216))][idx % 2]
    im = _gradient((w, h), wall_top, wall_bottom, diagonal=False)
    d = ImageDraw.Draw(im)
    floor_y = int(h * 0.68)
    d.rectangle((0, floor_y, w, h), fill=(40, 40, 46) if idx % 2 == 0 else (120, 110, 100))
    for i in range(0, w, 160):  # floor tiles
        d.line((i, floor_y, i - 200, h), fill=(60, 60, 66) if idx % 2 == 0 else (140, 130, 120), width=3)
    for i in range(5):  # windows with light
        x0 = 80 + i * 300
        d.rounded_rectangle((x0, 90, x0 + 220, 330), radius=18, fill=(150, 200, 255) if idx % 2 == 0 else (190, 225, 255))
    accent = PALETTES[idx % len(PALETTES)][0]
    if kind == "machines":
        for i in range(4):  # treadmills
            x = 140 + i * 360
            d.polygon([(x, floor_y + 70), (x + 260, floor_y + 40), (x + 270, floor_y + 70), (x + 10, floor_y + 100)], fill=(25, 25, 28))
            d.line((x + 220, floor_y + 45, x + 240, floor_y - 120), fill=(90, 90, 96), width=12)
            d.rounded_rectangle((x + 200, floor_y - 160, x + 290, floor_y - 110), radius=10, fill=accent)
    else:
        for i in range(6):  # dumbbell rack
            x = 180 + i * 210
            d.rectangle((x, floor_y - 40, x + 150, floor_y - 25), fill=(80, 80, 88))
            for j in range(3):
                cx = x + 25 + j * 50
                d.ellipse((cx - 18, floor_y - 75, cx + 18, floor_y - 39), fill=(20, 20, 24))
        d.rounded_rectangle((520, floor_y + 60, 1080, floor_y + 110), radius=20, fill=accent)  # bench
        d.rectangle((560, floor_y + 110, 590, floor_y + 190), fill=(70, 70, 76))
        d.rectangle((1010, floor_y + 110, 1040, floor_y + 190), fill=(70, 70, 76))
        d.line((300, floor_y - 260, 1300, floor_y - 260), fill=(160, 160, 170), width=14)  # barbell rack
        for x in (300, 1300):
            d.ellipse((x - 70, floor_y - 330, x + 70, floor_y - 190), fill=(25, 25, 30))
    d.rounded_rectangle((40, h - 120, 760, h - 40), radius=24, fill=(0, 0, 0))
    d.text((70, h - 105), f"{title[:40]} · demo illustration", fill=(255, 255, 255), font=_font(40))
    return _png(im)


def logo_png(name: str, idx: int) -> bytes:
    """Monogram logo on a gradient tile."""
    from PIL import ImageDraw

    words = [w for w in name.replace("&", " ").replace(".", " ").split() if w[:1].isalpha()]
    initials = "".join(w[0] for w in words[:2]).upper() or "IP"
    c1, c2 = PALETTES[idx % len(PALETTES)]
    im = _gradient((512, 512), c1, c2)
    d = ImageDraw.Draw(im)
    d.ellipse((96, 96, 416, 416), outline=(255, 255, 255), width=14)
    d.text((256, 262), initials, font=_font(150), fill=(255, 255, 255), anchor="mm")
    return _png(im)


# ------------------------------------------------------------------------------------------------ products
def product_png(art: dict[str, Any], brand: str | None, nutrition: dict[str, Any] | None, variant: int = 0) -> bytes:
    """Studio-style product shot: white sweep, soft shadow, simple 3-D-ish shapes. ``art`` comes from dataset.json:
    {shape, colors, label, sub}. Variant 0 = hero shot, 1 = tinted close-up, 2+ = nutrition panel (if any)."""
    from PIL import Image, ImageDraw, ImageFilter

    size = 900
    colors = art.get("colors") or ["#1d1d1f", "#0a84ff"]
    bg = (255, 255, 255) if variant == 0 else tuple(min(255, int(v * 0.12 + 255 * 0.88)) for v in _hex(colors[-1]))
    im = Image.new("RGB", (size, size), bg)
    shadow = Image.new("L", (size, size), 0)
    ImageDraw.Draw(shadow).ellipse((230, 720, 670, 790), fill=90)
    im.paste((40, 40, 45), mask=shadow.filter(ImageFilter.GaussianBlur(26)))
    d = ImageDraw.Draw(im)
    c1, c2 = _hex(colors[0]), _hex(colors[1] if len(colors) > 1 else colors[0])
    light = tuple(min(255, v + 60) for v in c1)
    dark = tuple(max(0, v - 35) for v in c1)
    shape = art.get("shape", "box")
    label, sub, brand = art.get("label") or "", art.get("sub") or "", (brand or "").upper()
    scale = 1.0 if variant == 0 else 1.18
    cx = size // 2

    def box(w: float, h: float, top: float) -> tuple[int, int, int, int]:
        w, h = w * scale, h * scale
        return int(cx - w / 2), int(top), int(cx + w / 2), int(top + h)

    def on(fill: tuple[int, int, int]) -> tuple[int, int, int]:  # readable text colour on ``fill``
        return (255, 255, 255) if sum(fill) < 600 else (29, 29, 31)

    if variant >= 2 and nutrition:
        d.rounded_rectangle((170, 130, 730, 770), radius=36, fill=(255, 255, 255), outline=(29, 29, 31), width=6)
        d.text((210, 175), "Nutrition Facts", font=_font(52), fill=(29, 29, 31))
        d.text((210, 245), f"Serving size {nutrition.get('serving_size', '')}", font=_font(28), fill=(90, 90, 96))
        d.line((210, 300, 690, 300), fill=(29, 29, 31), width=10)
        y = 330
        for key, name, unit in NUTRITION_FIELDS:
            if nutrition.get(key) in (None, ""):
                continue
            d.text((210, y), name, font=_font(34), fill=(29, 29, 31))
            d.text((690, y), f"{nutrition[key]} {unit}", font=_font(34), fill=(29, 29, 31), anchor="ra")
            d.line((210, y + 56, 690, y + 56), fill=(210, 210, 214), width=3)
            y += 74
        d.rounded_rectangle((210, 690, 690, 735), radius=20, fill=c2)
        d.text((450, 712), f"{label} · {brand}".upper(), font=_font(26), fill=(255, 255, 255), anchor="mm")
    elif shape in ("tub", "jar"):
        w, h = (380, 470) if shape == "tub" else (300, 380)
        body = box(w, h, 745 - h * scale)
        d.rounded_rectangle(body, radius=int(46 * scale), fill=c1)
        d.rounded_rectangle((body[0] + 18, body[1] + 30, body[0] + 46, body[3] - 30), radius=14, fill=light)
        lid = (body[0] - 12, body[1] - int(70 * scale), body[2] + 12, body[1] + 18)
        d.rounded_rectangle(lid, radius=24, fill=dark if shape == "tub" else c2)
        band = (body[0], body[1] + int(h * scale * 0.34), body[2], body[1] + int(h * scale * 0.70))
        d.rectangle(band, fill=c2)
        f1 = _font(int((86 if len(label) <= 5 else 54) * scale))
        d.text((cx, (band[1] + band[3]) // 2 - int(14 * scale)), label, font=f1, fill=on(c2), anchor="mm")
        d.text((cx, (band[1] + band[3]) // 2 + int(48 * scale)), sub, font=_font(int(30 * scale)), fill=on(c2), anchor="mm")
        d.text((cx, band[1] - int(46 * scale)), brand, font=_font(int(34 * scale)), fill=on(c1), anchor="mm")
    elif shape == "box":  # carton with a front label band
        front = box(470, 330, 745 - 330 * scale)
        depth = int(70 * scale)
        d.polygon([(front[0], front[1]), (front[0] + depth, front[1] - depth), (front[2] + depth, front[1] - depth),
                   (front[2], front[1])], fill=light)
        d.polygon([(front[2], front[1]), (front[2] + depth, front[1] - depth), (front[2] + depth, front[3] - depth),
                   (front[2], front[3])], fill=dark)
        d.rectangle(front, fill=c1)
        band = (front[0], front[1] + int(90 * scale), front[2], front[1] + int(250 * scale))
        d.rectangle(band, fill=c2)
        f1 = _font(int((70 if len(label) <= 8 else 52) * scale))
        d.text((cx, (band[1] + band[3]) // 2 - int(18 * scale)), label, font=f1, fill=on(c2), anchor="mm")
        d.text((cx, (band[1] + band[3]) // 2 + int(42 * scale)), sub, font=_font(int(28 * scale)), fill=on(c2), anchor="mm")
        d.text((cx, front[1] + int(48 * scale)), brand, font=_font(int(32 * scale)), fill=on(c1), anchor="mm")
    elif shape in ("shaker", "bottle"):
        w, h = (230, 520) if shape == "shaker" else (210, 540)
        body = box(w, h, 745 - h * scale)
        if shape == "shaker":
            d.rounded_rectangle(body, radius=int(40 * scale), fill=tuple(min(255, v + 150) for v in c1), outline=c1, width=8)
            for k in range(1, 6):
                y = body[3] - k * int(80 * scale)
                d.line((body[0] + 30, y, body[0] + 90, y), fill=c1, width=5)
        else:
            d.rounded_rectangle(body, radius=int(70 * scale), fill=c1)
            d.rounded_rectangle((body[0] + 22, body[1] + 60, body[0] + 50, body[3] - 60), radius=14, fill=light)
            d.text((cx + 20, (body[1] + body[3]) // 2), brand, font=_font(int(30 * scale)), fill=on(c1), anchor="mm")
        cap = (body[0] - 6, body[1] - int(90 * scale), body[2] + 6, body[1] + 20)
        d.rounded_rectangle(cap, radius=26, fill=c2)
        d.rounded_rectangle((cx - 40, cap[1] - 46, cx + 40, cap[1] + 10), radius=18, fill=c2)
    elif shape == "bag":
        body = box(600, 300, 745 - 300 * scale)
        d.arc((cx - 170, body[1] - 120, cx + 170, body[1] + 170), 190, 350, fill=c1, width=26)
        d.rounded_rectangle(body, radius=int(110 * scale), fill=c1)
        d.line((body[0] + 90, body[1] + 46, body[2] - 90, body[1] + 46), fill=(120, 120, 128), width=6)
        d.rounded_rectangle((body[0] + 40, body[1] + 120, body[0] + 190, body[3] - 40), radius=40, fill=c2)
        d.text((cx + 90, (body[1] + body[3]) // 2 + 20), brand, font=_font(int(40 * scale)), fill=(255, 255, 255), anchor="mm")
    elif shape == "watch":
        strap_w = 170 * scale
        d.rounded_rectangle((cx - strap_w / 2, 120, cx + strap_w / 2, 780), radius=60, fill=(60, 60, 66))
        face = box(330, 380, 450 - 190 * scale)
        mid = (face[1] + face[3]) // 2
        d.rounded_rectangle(face, radius=int(90 * scale), fill=(20, 20, 22), outline=(150, 150, 156), width=10)
        d.arc((cx - 120, mid - 120, cx + 120, mid + 120), -90, 180, fill=c2, width=22)
        d.text((cx, mid - 10), "10:09", font=_font(int(64 * scale)), fill=(255, 255, 255), anchor="mm")
        d.text((cx, mid + 52), "8,412 steps", font=_font(int(26 * scale)), fill=c2, anchor="mm")
    elif shape == "hrm":  # chest strap with a sensor pod
        d.ellipse((cx - 330 * scale, 360, cx + 330 * scale, 640), outline=c1, width=int(46 * scale))
        pod = box(250, 130, 435)
        d.rounded_rectangle(pod, radius=60, fill=(30, 30, 32), outline=(110, 110, 116), width=6)
        hx, hy = cx, (pod[1] + pod[3]) // 2
        d.ellipse((hx - 34, hy - 30, hx + 2, hy + 4), fill=c2)
        d.ellipse((hx - 2, hy - 30, hx + 34, hy + 4), fill=c2)
        d.polygon([(hx - 33, hy - 6), (hx + 33, hy - 6), (hx, hy + 32)], fill=c2)
    elif shape == "belt":
        d.ellipse((cx - 300 * scale, 330, cx + 300 * scale, 660), outline=c1, width=int(84 * scale))
        d.ellipse((cx - 300 * scale + 10, 340, cx + 300 * scale - 10, 650), outline=light, width=4)
        buckle = box(150, 100, 590)
        d.rounded_rectangle(buckle, radius=14, fill=(190, 190, 198), outline=(120, 120, 128), width=6)
        d.rectangle((buckle[0] + 26, buckle[1] + 30, buckle[2] - 26, buckle[1] + 46), fill=c2)
        d.text((cx, 360 + 14), brand, font=_font(int(30 * scale)), fill=on(c1), anchor="mm")
    elif shape == "gloves":
        for k, off in enumerate((-150, 150)):
            gx = cx + off * scale
            palm = (gx - 105 * scale, 400, gx + 105 * scale, 650)
            for f in range(4):  # finger stubs
                fx = palm[0] + 12 + f * (palm[2] - palm[0] - 24) / 4
                d.rounded_rectangle((fx, 330 - (f % 3) * 12, fx + 40 * scale, 440), radius=18, fill=c1)
            d.rounded_rectangle(palm, radius=int(60 * scale), fill=c1)
            d.rounded_rectangle((palm[0] - 6, 600, palm[2] + 6, 690), radius=20, fill=c2)
            d.ellipse((palm[0] + 40, 460, palm[2] - 40, 560), fill=light if k == 0 else dark)
    elif shape == "wraps":
        for off in (-140, 140):
            wx, wy, r = cx + off * scale, 520, 120 * scale
            for ring in range(5):
                rr = r - ring * 20
                d.ellipse((wx - rr, wy - rr, wx + rr, wy + rr), outline=c1 if ring % 2 == 0 else light, width=18)
            d.rounded_rectangle((wx - 30, wy + r - 10, wx + 30, wy + r + 120), radius=12, fill=c1)
            d.ellipse((wx - 40, wy - 40, wx + 40, wy + 40), fill=c2)
    elif shape == "sleeves":
        for off in (-130, 130):
            sx = cx + off * scale
            d.polygon([(sx - 95 * scale, 260), (sx + 95 * scale, 260), (sx + 80 * scale, 720), (sx - 80 * scale, 720)], fill=c1)
            d.ellipse((sx - 95 * scale, 240, sx + 95 * scale, 285), fill=light)
            d.rectangle((sx - 88 * scale, 450, sx + 88 * scale, 500), fill=c2)
    elif shape == "mat":  # rolled mat lying on the floor
        body = (cx - 300 * scale, 480, cx + 220 * scale, 700)
        d.rectangle(body, fill=c1)
        end = (body[2] - 110, body[1], body[2] + 110, body[3])
        d.ellipse(end, fill=light)
        for ring in range(1, 5):
            inset = ring * 22
            d.ellipse((end[0] + inset, end[1] + inset, end[2] - inset, end[3] - inset), outline=c2, width=6)
        d.rectangle((cx - 150, 470, cx - 110, 710), fill=c2)  # carry strap
    elif shape == "roller":
        body = (cx - 300 * scale, 470, cx + 240 * scale, 690)
        d.rectangle(body, fill=c1)
        for k in range(1, 9):
            x = body[0] + k * (body[2] - body[0]) / 9
            d.line((x, body[1] + 8, x, body[3] - 8), fill=c2, width=10)
        d.ellipse((body[2] - 80, body[1], body[2] + 80, body[3]), fill=light)
        d.ellipse((body[2] - 34, body[1] + 70, body[2] + 34, body[3] - 70), fill=dark)
    elif shape == "rope":
        d.arc((cx - 300 * scale, 180, cx + 300 * scale, 900), 180, 360, fill=c1, width=12)
        for hx in (cx - 300 * scale, cx + 300 * scale):
            d.rounded_rectangle((hx - 32, 540, hx + 32, 760), radius=28, fill=c2)
            d.rectangle((hx - 32, 600, hx + 32, 616), fill=c1)
    elif shape == "tee":
        s = scale
        pts = [(cx - 110 * s, 230), (cx - 40 * s, 230), (cx, 268), (cx + 40 * s, 230), (cx + 110 * s, 230),
               (cx + 260 * s, 330), (cx + 200 * s, 430), (cx + 150 * s, 395), (cx + 150 * s, 745),
               (cx - 150 * s, 745), (cx - 150 * s, 395), (cx - 200 * s, 430), (cx - 260 * s, 330)]
        d.polygon(pts, fill=c1)
        d.text((cx, 400), label or brand, font=_font(int(40 * s)), fill=c2, anchor="mm")
    else:  # bands: three loops
        for k, col in enumerate(colors[:3]):
            off = (k - 1) * 120 * scale
            d.ellipse((cx - 230 + off, 300 + k * 40, cx + 230 + off, 700 + k * 10), outline=_hex(col), width=int(34 * scale))
    d.text((size - 24, size - 22), "DEMO PRODUCT IMAGE", font=_font(20), fill=(150, 150, 156), anchor="rd")
    return _png(im)
