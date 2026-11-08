"""The board's layout: art on the left, one data column on the right.

Departures (if configured) at the top with service alerts under them, then the
weather forecast with any rain as its first row, then the graph cells along the
bottom. The forecast table flexes (up to five days) to fill what is left; the
graph band keeps its room.

The grid has two x-lines and nothing else:
    X0  every section's left edge: captions, day names, big numbers, icons
    TX  text that follows an icon (the station name after the route bullet)
Vertical rhythm: every section is caption -> content, and every rule has the
same RULE_GAP above and below it.
"""

from datetime import datetime

from PIL import ImageDraw

import board as cf
import settings
from board import H, SAFE, AA, aa, weather_glyph, _wrap, draw_stat_cell, font, tracked

X0, TX, XR = 792, 856, cf.W - SAFE - 6
RULE_GAP = 22
CAPTION = 18


def caption(draw, y, text, dim):
    tracked(draw, (X0, y), text, font(CAPTION), dim)


def rule(draw, y, colour):
    draw.line([(X0, y), (XR, y)], fill=colour, width=1)


def draw_trains(img, y, trains, line_alerts, ink, dim, ground, walk=0):
    """Header, countdown, then any service alert lines. Returns the bottom y."""
    t = settings.TRANSIT or {}
    draw = ImageDraw.Draw(img)
    r = 24
    cx, cy = X0 + r, y + r
    aa(img, (cx - r, cy - r), (r * 2, r * 2),
       lambda d, w, h: d.ellipse([1, 1, w - 2, h - 2], outline=ink, width=3 * AA))
    bullet = str(t.get("bullet", t.get("route", "")))[:2]
    draw.text((cx, cy - 1), bullet, font=font(33 if len(bullet) < 2 else 24, bold=True),
              fill=ink, anchor="mm")
    draw.text((TX, y - 3), t.get("station", "").upper(), font=font(29, bold=True), fill=ink)
    tracked(draw, (TX, y + 33), t.get("direction", "").upper(), font(CAPTION), dim)

    y += 74
    hero_size = 112
    if trains is None or not trains:
        draw.text((X0, y + 20), "feed unavailable" if trains is None
                  else "no trains scheduled", font=font(38), fill=dim)
        y += 80
    else:
        hero_i = next((i for i, m in enumerate(trains) if m >= walk), 0) if walk else 0
        big = font(hero_size, bold=True)
        # Pillow's text box starts above the cap height; lift so the digit's ink
        # sits on the grid rather than on the font's internal leading.
        top = draw.textbbox((0, 0), "0", font=big)[1]
        text = str(trains[hero_i])
        x = X0
        draw.text((x, y - top), text, font=big, fill=ink)
        x += draw.textlength(text, font=big) + 8
        base = y + hero_size * 0.72
        unit_f, then_f = font(28), font(36)
        draw.text((x, base), "min", font=unit_f, fill=dim, anchor="ls")
        x += draw.textlength("min", font=unit_f) + 26
        later = trains[hero_i + 1:]
        row = "  ·  ".join(str(m) for m in later)
        while later and x + draw.textlength(row, font=then_f) > XR:
            later = later[:-1]
            row = "  ·  ".join(str(m) for m in later)
        if later:
            draw.text((x, base), row, font=then_f, fill=dim, anchor="ls")
        y += int(hero_size * 0.72) + 4

    # Alerts about the line live with the line. Bold ink headline, dim when-line.
    f_t, f_s = font(23, bold=True), font(19)
    for title, sub in line_alerts:
        y += 14
        for line in _wrap(draw, title, f_t, XR - X0, 2):
            draw.text((X0, y), line, font=f_t, fill=ink)
            y += 28
        if sub:
            draw.text((X0, y + 1), sub, font=f_s, fill=dim)
            y += 25
    return y


def draw_forecast(img, y, forecast, rain, ink, dim, max_bottom):
    """Caption, optional rain row, then today + following days as one table."""
    draw = ImageDraw.Draw(img)
    cond = ""
    if forecast:
        cond = (forecast[0].get("condition") or "").replace("-", " ").upper()
    caption(draw, y, "FORECAST" + ("  ·  " + cond if cond else ""), dim)
    y += 34
    if not forecast:
        draw.text((X0, y), "unavailable", font=font(32), fill=dim)
        return y + 40

    ICON_CX, HI_R, LO_R = X0 + 140, X0 + 268, X0 + 336

    if rain:
        kind, title, sub = rain
        weather_glyph(img, (X0 + 22, y + 17), 40,
                      "snowy" if kind == "snow" else "rainy", ink)
        f_t = font(25, bold=True)
        draw.text((TX, y), title, font=f_t, fill=ink)
        if sub:
            x = TX + draw.textlength(title, font=f_t) + 14
            f_s = font(19)
            if x + draw.textlength(sub, font=f_s) > XR:
                draw.text((TX, y + 31), sub, font=f_s, fill=dim)
                y += 26
            else:
                draw.text((x, y + 6), sub, font=f_s, fill=dim)
        y += 48

    # Fill the space exactly: as many days as fit at their minimum heights
    # (up to five), then share out what is left, up to a comfortable maximum,
    # so the gap above the stat band's rule is always RULE_GAP and never a hole.
    MIN_T, MIN_D, MAX_T, MAX_D = 56, 44, 66, 54
    avail = max_bottom - y
    n = max(1, min(5, len(forecast), 1 + int((avail - MIN_T) // MIN_D)))
    spare = avail - MIN_T - (n - 1) * MIN_D
    extra = max(0, spare) / n
    rows = [(day, i == 0, min(MAX_T if i == 0 else MAX_D,
                              (MIN_T if i == 0 else MIN_D) + extra))
            for i, day in enumerate(forecast[:n])]

    for day, big, h in rows:
        name = "Today" if big else datetime.fromisoformat(day["datetime"]).strftime("%a")
        f_name = font(30 if big else 26, bold=True)
        f_hi = font(44 if big else 32, bold=True)
        f_lo = font(30 if big else 26)
        mid = y + h / 2 - 4
        draw.text((X0, mid), name, font=f_name, fill=ink, anchor="lm")
        weather_glyph(img, (ICON_CX, mid), 56 if big else 44, day.get("condition"), ink)
        t_hi, t_lo = day.get("temperature"), day.get("templow")
        draw.text((HI_R, mid), f"{round(t_hi)}°" if t_hi is not None else "--",
                  font=f_hi, fill=ink, anchor="rm")
        if t_lo is not None:
            draw.text((LO_R, mid), f"{round(t_lo)}°", font=f_lo, fill=dim,
                      anchor="rm")
        p = day.get("precipitation") or 0
        if p > 0:
            draw.text((XR, mid), f'{p:.2f}"', font=font(24), fill=dim,
                      anchor="rm")
        y += h
    return y


def build_overlay(art_path, forecast, trains, cells, now=None, alerts=None):
    """The whole board for `now`. cells: graphs.fetch_all() output."""
    now = now or datetime.now()
    img, _ = cf.place_art(art_path, seed=f"{cf.Path(art_path).name if art_path else '-'}:{now.toordinal()}")
    night = not (settings.NIGHT_END <= now.hour < settings.NIGHT_START)
    if night:
        img = cf.ImageEnhance.Brightness(img).enhance(0.42)
        ground, ink = cf.NIGHT_GROUND, (238, 238, 240)
        dim, rule_c = cf.LGRAY, (150, 150, 152)
        wash_alpha = 150
    else:
        img = cf.Image.blend(img, cf.Image.new("RGB", img.size, cf.CREAM), cf.CREAM_VEIL)
        ground = cf.CREAM
        ink = cf.art_ink(img, ground)
        dim, rule_c = cf.dim_ink(ink, ground), cf.tone(ink, 238, 0.84)
        wash_alpha = 238
    cf.scrim(img, (500, 0, cf.W, H), "right", max_alpha=wash_alpha, ramp_px=292,
             colour=ground)
    draw = ImageDraw.Draw(img)

    alerts = alerts or []
    line_alerts = [(t, s) for k, t, s in alerts if k == "line"]
    rain = next((a for a in alerts if a[0] in ("rain", "snow")), None)

    # Bottom band is fixed: the graph cells always keep their room.
    STATS_H = 150 if cells else 0
    stats_top = H - SAFE - STATS_H

    y = SAFE + 10
    if settings.TRANSIT:
        y = draw_trains(img, y, trains, line_alerts, ink, dim, ground,
                        walk=cf.WALK_MIN)
        y += RULE_GAP
        rule(draw, y, rule_c)
        y += RULE_GAP
    fc_bottom = stats_top - 2 * RULE_GAP if cells else H - SAFE
    draw_forecast(img, y, forecast, rain, ink, dim, fc_bottom)

    if cells:
        rule(draw, stats_top - RULE_GAP, rule_c)
        gutter = 26
        n = len(cells)
        w = (XR - X0 - gutter * (n - 1)) / n
        for i, c in enumerate(cells):
            x0 = int(X0 + i * (w + gutter))
            draw_stat_cell(img, (x0, stats_top, int(x0 + w), H - SAFE), c["label"], c["value"],
                           c["unit"], c["note"], c["direction"], c["series"], ink=ink, dim=dim)

    cf.draw_clock(img, now, ink, dim, tint=ground, alpha=wash_alpha)
    cf.draw_species(img, cf.LABEL_CX, *cf.credit_for(art_path), ink=ink, sub=dim,
                    tint=ground, alpha=wash_alpha)
    return img
