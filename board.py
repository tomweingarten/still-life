"""Drawing helpers for the Nest Hub board: art placement, washes, ink colours,
the clock, the plate label, icons and the graph cells.
"""

import colorsys
import functools
import json
import math
import random
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

import settings

BLACK, WHITE = (0, 0, 0), (255, 255, 255)
FONT_DIR = "/usr/share/fonts/truetype/noto"
W, H = 1280, 800               # Google Nest Hub Max, native and exact (16:10)


def font(size, bold=False):
    name = "NotoSans-Bold.ttf" if bold else "NotoSans-Regular.ttf"
    for candidate in (
        f"{FONT_DIR}/{name}",
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def tracked(draw, xy, text, fnt, fill, spacing=3):
    """Letter-spaced small caps, for the section captions."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += draw.textlength(ch, font=fnt) + spacing
    return x


SAFE = 34                      # keep content off the rounded corners


LGRAY = (205, 207, 211)        # secondary text on dark


# The overlay's ground. Warm, not white: pure #FFF against a botanical plate's
# own aged paper reads as a hole punched in the picture, and the near-black type
# it wants is harsh on a screen you glance at rather than read.
CREAM = (251, 247, 239)


CREAM_VEIL = 0.12              # cast over the whole frame, so the plate's own


                               # paper-white is not brighter than the column
NIGHT_GROUND = (18, 18, 20)


# Minutes from door to platform. 0 = off; set it and the board stops shouting
# about trains you cannot physically reach.
WALK_MIN = int((settings.TRANSIT or {}).get("walk_min", 0))   # trains you cannot reach in time are hidden


def cover(path, size):
    """Cover-crop the artwork to fill `size`, full colour, no dithering."""
    w, h = size
    if path is None or not Path(path).exists():
        return Image.new("RGB", size, (32, 32, 36))
    src = Image.open(path)
    if src.mode in ("RGBA", "LA", "P"):
        flat = Image.new("RGB", src.size, WHITE)
        src = src.convert("RGBA")
        flat.paste(src, mask=src.split()[-1])
        src = flat
    else:
        src = src.convert("RGB")
    scale = max(w / src.width, h / src.height)
    resized = src.resize(
        (max(w, round(src.width * scale)), max(h, round(src.height * scale))),
        Image.LANCZOS,
    )
    left = (resized.width - w) // 2
    top = (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


CREDITS_PATH = settings.ART_DIR / "credits.json"


LABEL_CX = 348                      # bottom-middle of the art area, left of the column


def credit_for(path):
    """(title, collection, common name) for an artwork, from credits.json or
    the filename. `common` is the English name; empty when credits.json has
    none, which drops that line from the label rather than guessing."""
    if path is None:
        return "", "", ""
    try:
        c = json.loads(CREDITS_PATH.read_text())[Path(path).name]
        return c.get("title", ""), c.get("plate", ""), c.get("common", "")
    except (OSError, ValueError, KeyError):
        return Path(path).stem.replace("-", " ").replace("_", " "), "", ""


def art_for_day(when=None, art_dir=None):
    """Deterministic daily pick: one plate all day, the next one tomorrow.

    The board renders every minute, so a random pick would change the picture
    every minute. Keying off the ordinal date makes the choice stable within a
    day and cycles the whole set before repeating.

    The pool is whatever `credits.json` lists, which makes that file the single
    definition of the curated set; anything else in the art folder is ignored.
    """
    when = when or datetime.now()
    d = Path(art_dir or settings.ART_DIR)
    if not d.is_dir():
        return None
    try:
        curated = json.loads(CREDITS_PATH.read_text())
        files = sorted(d / n for n in curated if (d / n).exists())
    except (OSError, ValueError):
        files = []
    if not files:                        # no credits yet: fall back to everything
        files = sorted(p for p in d.iterdir() if p.suffix.lower()
                       in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"))
    return files[when.toordinal() % len(files)] if files else None


MOUNT = (23, 23, 25)        # fallback ground when there is no artwork at all


INK_TARGET = 0.30           # minimum non-paper fraction for an acceptable crop


INK_DISTANCE = 46           # L1 distance from paper tone before a pixel is "ink"


def _ink_map(im, work_w=260):
    """Small 0/1 map of where a plate carries ink rather than blank paper.

    Paper tone comes from the border median, and a pixel counts as ink by its
    *distance from that tone* rather than by any absolute test. That distinction
    is load-bearing: several Köhler sheets are printed on tinted stock (peach,
    tan, blue-grey), and an absolute "is it coloured?" test scores the paper
    itself as ink, marking the whole plate as content and making the crop search
    meaningless. Distance-from-paper catches both a dark leaf and a pale yellow
    flower while ignoring whatever colour the sheet happens to be.
    """
    small = im.convert("RGB")
    small.thumbnail((work_w, work_w * 4), Image.LANCZOS)
    w, h = small.size
    p = small.load()

    border = []
    for x in range(0, w, 2):
        border += [p[x, 0], p[x, h - 1]]
    for y in range(0, h, 2):
        border += [p[0, y], p[w - 1, y]]
    border.sort(key=sum)
    pr, pg, pb = border[len(border) // 2]

    mask = []
    for y in range(h):
        row = bytearray(w)
        for x in range(w):
            r, g, b = p[x, y]
            if abs(r - pr) + abs(g - pg) + abs(b - pb) > INK_DISTANCE:
                row[x] = 1
        mask.append(row)
    return mask, w, h


def content_crop(im, size, seed, tries=140, zoom=(0.58, 1.0)):
    """A landscape window of a portrait plate that actually contains the plant.

    Botanical plates are portrait with wide blank margins, so a uniformly random
    landscape window lands on empty paper a good fraction of the time. Candidate
    windows are scored by ink coverage using a summed-area table, and the first
    one over INK_TARGET wins. The candidate *order* is fixed by `seed`, so the
    crop holds steady all day and moves somewhere else tomorrow -- taking the
    single best-scoring window instead would pick the same dense spot every time.
    """
    aspect = size[0] / size[1]
    mask, mw, mh = _ink_map(im)

    ii = [[0] * (mw + 1) for _ in range(mh + 1)]
    for y in range(mh):
        row, prev, cur = mask[y], ii[y], ii[y + 1]
        run = 0
        for x in range(mw):
            run += row[x]
            cur[x + 1] = prev[x + 1] + run

    def coverage(x0, y0, x1, y1):
        n = (x1 - x0) * (y1 - y0)
        if not n:
            return 0.0
        return (ii[y1][x1] - ii[y0][x1] - ii[y1][x0] + ii[y0][x0]) / n

    rng = random.Random(seed)
    best = None
    for _ in range(tries):
        cw = max(16, min(mw, int(mw * rng.uniform(*zoom))))
        ch = int(round(cw / aspect))
        if ch > mh:                       # window taller than the plate allows
            ch = mh
            cw = min(mw, int(round(ch * aspect)))
        x0 = rng.randint(0, mw - cw)
        y0 = rng.randint(0, mh - ch)
        got = coverage(x0, y0, x0 + cw, y0 + ch)
        if best is None or got > best[0]:
            best = (got, x0, y0, cw, ch)
        if got >= INK_TARGET:
            best = (got, x0, y0, cw, ch)
            break

    got, x0, y0, cw, ch = best
    sx, sy = im.width / mw, im.height / mh
    box = (int(x0 * sx), int(y0 * sy), int((x0 + cw) * sx), int((y0 + ch) * sy))
    return im.crop(box).resize(size, Image.LANCZOS), got


def place_art(path, seed=0):
    """Full-bleed backdrop for the frame, plus the ink coverage achieved.

    Everything is full-bleed now: a portrait plate is cropped to a landscape
    window that contains the plant rather than being mounted whole, so each day
    shows a different part of the print.
    """
    if path is None or not Path(path).exists():
        return Image.new("RGB", (W, H), MOUNT), 0.0
    src = Image.open(path)
    if src.mode in ("RGBA", "LA", "P"):
        flat = Image.new("RGB", src.size, WHITE)
        src = src.convert("RGBA")
        flat.paste(src, mask=src.split()[-1])
        src = flat
    else:
        src = src.convert("RGB")

    if src.width / src.height >= 1.30:
        return cover(path, (W, H)), 1.0   # already landscape: plain cover-crop
    return content_crop(src, (W, H), seed)


# The frosted panels, not the type, are what respect SAFE: on a rounded-corner
# display the panel is the shape whose clipped corner would show, so the glass
# sits on the margin and the text is inset within it.
CLOCK_PAD = (26, 12, 26, 14)     # left, top, right, bottom


LABEL_PAD = (26, 12, 26, 14)


def draw_clock(img, now, ink, sub, hour24=False, tint=CREAM, alpha=237):
    """The wall clock, big, on frosted glass over the artwork's top-left.

    It goes on the art rather than in the data column because the column has no
    spare vertical room and the art's corner is free -- and at this size it is
    readable from across the kitchen, which a 19px stamp was not.

    It also replaces the old "as of" freshness stamp, and is strictly better at
    that job: if the loop ever stops casting, a wrong clock at 104px is instantly
    obvious, whereas a stale timestamp in 19px grey is not.

    No AM/PM. The suffix hung off one side and left the block visibly
    off-centre on its panel, and it earns nothing: nobody misreads a kitchen
    clock by twelve hours.

    Both lines are centred on the panel, because which of the two is wider
    changes with the time ("3:19" is narrower than the date, "12:38" is not) and
    a flush-left block would make the panel's right margin jump around.
    """
    draw = ImageDraw.Draw(img)
    t = now.strftime("%H:%M") if hour24 else now.strftime("%-I:%M")
    date = now.strftime("%A %-d %B")
    f_big, f_date = font(104, bold=True), font(25)

    l, tp, r, b = CLOCK_PAD
    w = max(draw.textlength(t, font=f_big), draw.textlength(date, font=f_date))
    # -18 trims the 104px font's internal leading, which is dead space the panel
    # would otherwise wear as a lopsided top margin.
    x0, y0 = SAFE, SAFE
    tx, ty = x0 + l + w / 2, y0 + tp - 18
    frost(img, (x0, y0, x0 + l + w + r, ty + 148 + b), tint=tint, alpha=alpha)

    draw.text((tx, ty), t, font=f_big, fill=ink, anchor="ma")
    draw.text((tx, ty + 116), date, font=f_date, fill=sub, anchor="ma")


def draw_species(img, centre_x, title, collection, common, ink, sub,
                 tint=CREAM, alpha=237):
    """Species name on frosted glass, bottom-middle of the art area.

    This used to sample the pixels underneath and pick black or white type
    backed by a contrasting stroke, because a random crop can put it over dark
    leaf or bare paper. The panel removes that problem rather than compensating
    for it: the label now brings its own ground, so the ink is simply the
    frame's ink and the halo stroke is gone.
    """
    if not title:
        return
    draw = ImageDraw.Draw(img)
    f1, f_c, f2 = font(34, bold=True), font(24), font(18)
    w = max(draw.textlength(title, font=f1),
            draw.textlength(common or "", font=f_c),
            draw.textlength(collection or "", font=f2))
    l, tp, r, b = LABEL_PAD
    # 62 = title + collection block; the English name adds a 32px line between.
    block = 62 + (32 if common else 0)
    y = H - SAFE - b - block
    frost(img, (centre_x - w / 2 - l, y - tp, centre_x + w / 2 + r, H - SAFE),
          tint=tint, alpha=alpha)

    draw.text((centre_x, y), title, font=f1, fill=ink, anchor="ma")
    y += 42
    if common:
        draw.text((centre_x, y), common, font=f_c, fill=ink, anchor="ma")
        y += 32
    if collection:
        draw.text((centre_x, y), collection, font=f2, fill=sub, anchor="ma")


def scrim(img, box, direction, max_alpha=185, ramp_px=None, ease=True,
          colour=(0, 0, 0)):
    """Fade a gradient over `box` so text on it stays legible.

    `colour` is the ground being laid down: black at night, CREAM by day. The
    daytime frame is a cream wash with dark type rather than a dark wash with
    white type -- same geometry, same ramp, inverted.

    direction 'right' darkens toward the right edge, 'up' toward the bottom.
    `ramp_px` is how far alpha takes to climb to max; past that it holds.

    Two things make this read as a soft edge rather than a hard one:

    * **Width.** The ramp has to be generous. At 93px the falloff was over
      before the eye registered it as a gradient at all.
    * **Easing.** A linear ramp has a corner at each end -- alpha is changing at
      full rate and then abruptly isn't -- and the eye picks that discontinuity
      out as a visible line (Mach banding), which is what read as "drops
      suddenly then slowly fades". Smoothstep has zero slope at both ends, so
      the gradient begins and ends invisibly.
    """
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    layer = Image.new("L", (w, h), 0)
    ld = ImageDraw.Draw(layer)
    n = w if direction == "right" else h
    lim = max(1, (ramp_px if ramp_px is not None else n) - 1)
    for i in range(n):
        t = min(1.0, i / lim)
        if ease:
            t = t * t * (3 - 2 * t)              # smoothstep
        a = int(max_alpha * t)
        if direction == "right":
            ld.line([(i, 0), (i, h)], fill=a)
        else:
            ld.line([(0, i), (w, i)], fill=a)
    img.paste(Image.new("RGB", layer.size, colour), (x0, y0), layer)


def frost(img, box, radius=20, tint=CREAM, alpha=237, soft=30):
    """A patch of the column's own material behind the clock and the label.

    Same recipe as the column wash: a flat tint over the art, which stays crisp
    underneath, with an eased edge. It used to be frosted glass -- the art
    blurred, near-opaque, 4px feather -- and next to the column that read as a
    different material (2026-09-27): the blur turned the plate into a grey
    smudge and the tight feather left a dusty rim, so the panels looked like
    stickers. The docstring then warned a wide feather "reads as a smudge"; that
    was the blur talking. Over crisp art the soft edge just reads as the wash.

    The rounded box is grown by soft/2 before blurring so the text area stays at
    full `alpha`, and the mask is smoothstepped so the falloff has no Mach band.
    Callers pass the column's alpha, so the two grounds are one strength.
    """
    x0, y0, x1, y1 = (int(v) for v in box)
    g = soft // 2
    pad = g + soft * 2
    rx0, ry0 = max(0, x0 - pad), max(0, y0 - pad)
    rx1, ry1 = min(W, x1 + pad), min(H, y1 + pad)
    mask = Image.new("L", (rx1 - rx0, ry1 - ry0), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [x0 - g - rx0, y0 - g - ry0, x1 + g - rx0, y1 + g - ry0],
        radius=radius + g, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(soft / 2.2))
    mask = mask.point([int(alpha * (v / 255) ** 2 * (3 - 2 * v / 255))
                       for v in range(256)])
    img.paste(Image.new("RGB", mask.size, tint), (rx0, ry0), mask)


WARM_BROWN = (84, 74, 62)      # neutral fallback ink when a plate is too pale


INK_CONTRAST = 4.5             # primary type against the ground


DIM_CONTRAST = 3.0             # secondary type: lows, precipitation, sublabels


def _relative_luminance(c):
    f = [(v / 255) / 12.92 if v / 255 <= 0.03928
         else (((v / 255) + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]


def contrast(a, b):
    hi, lo = sorted((_relative_luminance(a), _relative_luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _to_contrast(colour, ground, target, steps=20):
    """Darken `colour` just enough to clear `target` against `ground`.

    Necessary because HLS lightness is not perceptual: a yellow-green set to the
    same nominal lightness as an indigo is far brighter, and the belladonna's
    ink landed at 4.1:1 where the Hokusai's made 6.8:1. Scaling to a measured
    ratio is what makes one rule cover every plate.
    """
    if contrast(colour, ground) >= target:
        return colour
    lo, hi = 0.0, 1.0                      # feasible factor is <= some f*
    for _ in range(steps):
        mid = (lo + hi) / 2
        if contrast(tuple(int(c * mid) for c in colour), ground) >= target:
            lo = mid
        else:
            hi = mid
    return tuple(int(c * lo) for c in colour)


def art_ink(img, ground=CREAM, target_l=88, sat=0.45, floor_l=112):
    """Type colour sampled from the artwork's own darkest tones.

    The frame stops being monochrome-with-a-picture and becomes something of the
    plate: a leaf-green for the belladonna, indigo for the Hokusai. The sample is
    forced to a fixed lightness, desaturated so it cannot vibrate against the
    cream, then darkened to a measured contrast ratio.

    `floor_l` is the safety net. A washed-out or very pale plate averages to
    something with no usable hue at all, so anything above the floor falls back
    to the neutral warm brown rather than tinting the frame on noise. Three of
    the 25 plates currently take that path.
    """
    # Mean of the darkest eighth, via getcolors() rather than getdata(): the
    # latter is deprecated in Pillow 14 and this runs once a minute under cron,
    # so the warning would be the bulk of the log.
    small = img.resize((80, 50))
    counts = small.getcolors(maxcolors=80 * 50) or [(1, (0, 0, 0))]
    counts.sort(key=lambda nc: (nc[1][0] * 0.299 + nc[1][1] * 0.587
                                + nc[1][2] * 0.114))
    want = max(1, sum(n for n, _ in counts) // 8)
    taken = 0
    acc = [0.0, 0.0, 0.0]
    for n, c in counts:
        take = min(n, want - taken)
        for i in range(3):
            acc[i] += c[i] * take
        taken += take
        if taken >= want:
            break
    r, g, b = (v / taken for v in acc)
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    if l * 255 > floor_l:
        return _to_contrast(WARM_BROWN, ground, INK_CONTRAST)
    r, g, b = colorsys.hls_to_rgb(h, target_l / 255, min(s, sat))
    return _to_contrast((int(r * 255), int(g * 255), int(b * 255)),
                        ground, INK_CONTRAST)


def dim_ink(ink, ground=CREAM):
    """Secondary type: the ink's hue, drained and darkened to a legible ratio.

    Blending the ink toward the ground was the obvious move and it was wrong --
    it bottomed out at 1.79:1 on the yellow-green plates, which is where the
    forecast lows and MANHATTAN-BOUND live. Holding a contrast floor instead
    keeps the hue family without letting any plate produce unreadable type.
    """
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in ink))
    r, g, b = colorsys.hls_to_rgb(h, 0.62, min(s, 0.16))
    return _to_contrast((int(r * 255), int(g * 255), int(b * 255)),
                        ground, DIM_CONTRAST)


def tone(ink, toward, amount):
    """Blend `ink` toward a lighter tone -- the hairline rules only.

    Rules are decorative and carry no information, so unlike the type they have
    no contrast floor to meet.
    """
    return tuple(int(c + (toward - c) * amount) for c in ink)


AA = 4


def aa(img, xy, size, fn):
    """Run `fn(draw, w, h)` at AA scale, downsample, and paste at `xy`."""
    w, h = size
    layer = Image.new("RGBA", (w * AA, h * AA), (0, 0, 0, 0))
    fn(ImageDraw.Draw(layer), w * AA, h * AA)
    img.paste(layer.resize((w, h), Image.LANCZOS), xy, layer.resize((w, h), Image.LANCZOS))


# ---------------------------------------------------------------- weather icons
# Lucide (https://lucide.dev), ISC licensed, vendored as fonts/lucide.ttf from
# the lucide-static package. Every icon is a 2px stroke on a 24px grid with no
# fills, which is the same drawing language as the L bullet's ring -- the old
# hand-drawn glyphs were solid silhouettes and read as heavy blobs beside it.
#
# A font rather than the SVGs: Pillow renders TrueType with its own antialiasing
# and no new dependency, where the SVGs would need cairosvg. It also means the
# icons scale by font size instead of by re-tuning a dozen hand-placed lobes.
LUCIDE_PATH = settings.HERE / "fonts" / "lucide.ttf"


# Codepoints are from the package's own font/info.json. Do not trust a web page
# for these -- the docs site and the font disagree, and a wrong codepoint renders
# a plausible but different icon rather than failing.
LUCIDE = {
    "sun": 0xE178, "moon": 0xE11E, "cloud": 0xE088, "cloud-sun": 0xE216,
    "cloud-rain": 0xE08E, "cloud-rain-wind": 0xE08F, "cloud-snow": 0xE090,
    "cloud-lightning": 0xE08C, "cloud-fog": 0xE214, "cloud-hail": 0xE08B,
    "cloud-drizzle": 0xE08A, "wind": 0xE1B0,
}


# Home Assistant's documented condition vocabulary, all of it.
COND_ICON = {
    "clear-night": "moon",      "cloudy": "cloud",
    "fog": "cloud-fog",         "hail": "cloud-hail",
    "lightning": "cloud-lightning", "lightning-rainy": "cloud-lightning",
    "partlycloudy": "cloud-sun", "pouring": "cloud-rain-wind",
    "rainy": "cloud-rain",      "snowy": "cloud-snow",
    "snowy-rainy": "cloud-hail", "sunny": "sun",
    "windy": "wind",            "windy-variant": "wind",
    "exceptional": "cloud",
}


@functools.lru_cache(maxsize=8)
def _icon_font(size):
    return ImageFont.truetype(str(LUCIDE_PATH), size)


def weather_glyph(img, center, size, condition, col=WHITE):
    """One outline weather icon, optically centred on `center`.

    Centring is on the glyph's *ink* box, not the font's line box: the icons
    differ in height (a cloud is 84 units where a sun is 96) and anchoring to
    the line box would leave the row icons sitting at visibly different heights
    against their temperatures.
    """
    name = COND_ICON.get((condition or "").lower(), "cloud")
    ch = chr(LUCIDE[name])
    f = _icon_font(int(size))
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = d.textbbox((0, 0), ch, font=f, anchor="lt")
    d.text((center[0] - (x0 + x1) / 2, center[1] - (y0 + y1) / 2), ch,
           font=f, fill=col, anchor="lt")


def draw_stat_cell(img, box, label, value, unit, trend, direction, series,
                   ink=WHITE, dim=LGRAY):
    """One compact stat tile: label, value, signed delta, bare sparkline.

    Shared by both body metrics so they read as peers rather than as two panels
    that happen to sit together. The rows stack instead of putting value and
    delta on one line: at half-column width "9.29 lb" plus "0.06 / 21d" overruns
    the cell, and the overflow lands on the frame edge where it is least visible.

    No axes, no point labels. A trend moving ~0.01 lb/day does not survive a
    two-second glance as numbers; the shape of the curve does.
    """
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    tracked(draw, (x0, y0), label, font(18), dim)

    if not series:
        draw.text((x0, y0 + 30), "no data", font=font(30), fill=dim)
        return

    big = font(42, bold=True)
    draw.text((x0, y0 + 24), value, font=big, fill=ink)
    if unit:
        draw.text((x0 + draw.textlength(value, font=big) + 9, y0 + 44), unit,
                  font=font(20), fill=dim)

    # Autofit rather than a fixed size: at half-column width the delta line is
    # the one thing whose length is not under our control ("+10.3% since Jun 8"
    # is 212px in a 211px cell), and it grows with both the number and the
    # month name. Shrinking beats overflowing onto the frame edge.
    s = 7
    for pt in (22, 20, 18):
        tf = font(pt)
        if draw.textlength(trend, font=tf) + s * 2 + 10 <= x1 - x0:
            break
    ax, ay = x0 + 2, y0 + 84
    def arrow(d, w, h):
        if direction > 0:
            d.polygon([(0, h), (w, h), (w / 2, 0)], fill=dim)
        elif direction < 0:
            d.polygon([(0, 0), (w, 0), (w / 2, h)], fill=dim)
        else:
            d.ellipse([0, h * 0.28, w, h * 0.72], fill=dim)
    aa(img, (int(ax), int(ay - s)), (s * 2, s * 2), arrow)
    draw.text((ax + s * 2 + 8, y0 + 74), trend, font=tf, fill=dim)

    sx0, sy0, sx1 = x0, y0 + 108, x1
    lo, hi = min(series), max(series)
    if hi - lo < 1e-9:
        lo, hi = lo - 0.1, hi + 0.1
    if len(series) > 1:
        pad = 8
        bw, bh = int(sx1 - sx0) + pad * 2, 34 + pad * 2

        def spark(d, w, h):
            n = len(series) - 1
            pts = [(pad * AA + (w - pad * 2 * AA) * (i / n),
                    (h - pad * AA) - (h - pad * 2 * AA) * ((v - lo) / (hi - lo)))
                   for i, v in enumerate(series)]
            d.line(pts, fill=ink, width=4 * AA, joint="curve")
            ex, ey = pts[-1]
            r = 6 * AA
            d.ellipse([ex - r, ey - r, ex + r, ey + r], fill=ink)

        aa(img, (int(sx0) - pad, int(sy0) - pad), (bw, bh), spark)


def _wrap(draw, text, font, width, max_lines):
    """Word wrap to `width`, ellipsised past `max_lines`.

    Two-line results are balanced (the break that makes the longer line
    shortest), so a headline does not end on an orphaned "Av". Splits on plain
    spaces only: callers glue "8 Av" with a no-break space to keep it whole.
    """
    fits = lambda t: draw.textlength(t, font=font) <= width
    words = text.split(" ")
    if fits(text):
        return [text]
    if max_lines >= 2:
        best = None
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            if fits(a) and fits(b):
                w = max(draw.textlength(a, font=font), draw.textlength(b, font=font))
                if best is None or w < best[0]:
                    best = (w, [a, b])
        if best:
            return best[1]
    lines, cur = [], ""
    for word in words:
        trial = (cur + " " + word).strip()
        if fits(trial) or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and not fits(last + "\u2026"):
            last = last[:-1].rstrip()
        lines[-1] = last + "\u2026"
    return lines

