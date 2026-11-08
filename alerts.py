"""Glance alerts for the board: rain on the way, and service changes on your line.

Both are silent unless they matter. Rain is drawn as the forecast's first row,
service changes under the departures.

    fetch_rain()       -> [(unix_start, mm, snow_cm), ...] in 15-minute steps
    rain_item(rain, t) -> (title, sub) or None
    fetch_line_alerts() -> [{"text": ..., "periods": [(start, end), ...]}, ...]
    line_items(alerts, t) -> [(title, sub), ...]

Fetch and phrase are split so the phrasing runs at render time against the
display's effective clock, the same way the train countdown does: data fetched
at 21:00 still says the right thing at 21:04.

Sources: Open-Meteo's minutely_15 (HRRR over the US, free, no key) and, for
service changes, the MTA's GTFS-rt alerts feed in its JSON form (no key).
Service alerts are MTA-specific (they read the MTA's "mercury" alert types). Rain is 15-minute
data, so the countdown is rounded to 5 min and carries a "~"; claiming "rain in
12 min" would promise a precision the model does not have.
"""

import json
import re
import time
import urllib.request
from datetime import datetime

import settings

HOME = (settings.LAT, settings.LON)
RAIN_URL = ("https://api.open-meteo.com/v1/forecast?latitude=%.3f&longitude=%.3f"
            "&minutely_15=precipitation,snowfall&forecast_minutely_15=12"
            "&past_minutely_15=1&timeformat=unixtime")
_T = settings.TRANSIT or {}
ALERTS_URL = _T.get("alerts_url")   # optional; e.g. the MTA subway alerts JSON feed
ROUTE = _T.get("route", "")

STEP = 900                        # seconds per minutely_15 slot
WET_MM = 0.2                      # per 15 min (~0.8 mm/h): below this is mist
HEAVY_MM = 1.5                    # per 15 min (~6 mm/h): worth a word
RAIN_HORIZON = 7200               # look this far ahead for rain

# An alert on any stop of your trip, or on the whole line (no stops listed), counts.
OUR_STOPS = set(_T.get("trip_stops", []))
# Nothing here changes whether the train comes: platform moves in Manhattan,
# notices, extra trains, and express patterns the L does not have.
IGNORED_TYPES = {"Boarding Change", "Station Notice", "Extra Service",
                 "Special Schedule", "Express to Local",
                 "Planned - Express to Local"}
LINE_HORIZON = 24 * 3600             # announce planned work this far ahead


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "still-life/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


# ----------------------------------------------------------------------- rain


def fetch_rain(lat=HOME[0], lon=HOME[1]):
    m = _get(RAIN_URL % (lat, lon))["minutely_15"]
    snow = m.get("snowfall") or [0] * len(m["time"])
    return [(t, p or 0.0, s or 0.0)
            for t, p, s in zip(m["time"], m["precipitation"], snow)]


def _clock(t):
    return datetime.fromtimestamp(t).strftime("%-I:%M")


def rain_item(rain, now):
    """(title, sub) for the next two hours of precipitation, or None."""
    if not rain:
        return None
    # Slots are labelled by their start. The one we are in is the last one
    # starting at or before now; ignore anything that has already ended.
    slots = [s for s in rain if s[0] + STEP > now and s[0] < now + RAIN_HORIZON]
    if not slots:
        return None
    wet = [s for s in slots if s[1] >= WET_MM]
    if not wet:
        return None
    word = "Snow" if sum(s[2] for s in wet) > 0 else "Rain"
    heavy = max(s[1] for s in wet) >= HEAVY_MM

    if slots[0][1] >= WET_MM:                 # already coming down
        # It stops at the first two dry slots in a row; one dry slot is a lull.
        end = None
        for a, b in zip(slots, slots[1:] + [None]):
            if a[1] < WET_MM and (b is None or b[1] < WET_MM):
                end = a[0]
                break
        title = "%sing now" % word if word == "Snow" else "Raining now"
        if end is None:
            sub = "for the next 2 hours"
        else:
            sub = "until ~%s" % _clock(end)
        later_heavy = [s for s in slots[1:] if s[1] >= HEAVY_MM]
        if later_heavy and slots[0][1] < HEAVY_MM:
            then = end is not None and end <= later_heavy[0][0]
            sub += "%s heavy ~%s" % (", then" if then else " ·",
                                     _clock(later_heavy[0][0]))
        elif heavy:
            sub = "heavy, " + sub
        return title, sub

    start = wet[0][0]
    mins = max(5, int(round((start - now) / 300.0)) * 5)
    title = "%s in ~%d min" % (word, mins)
    sub = ("heavy" if heavy else "light") + " · around %s" % _clock(start)
    return title, sub


# ------------------------------------------------------------------ L alerts


def _en(block):
    for t in (block or {}).get("translation", []):
        if t.get("language") == "en":
            return t.get("text", "")
    return ""


def _tidy(text):
    """MTA headline -> glance headline. Boroughs go: they are implied by the stop
    names, and they are what pushed a one-liner onto three lines."""
    text = re.sub(r"\[(\w+)\]", r"\1", text).rstrip(". ")
    text = re.sub(r"^In (Manhattan|Brooklyn|Queens|the Bronx|Staten Island), ", "", text)
    text = re.sub(r", (Manhattan|Brooklyn|Queens|the Bronx|Staten Island)\b", "", text)
    # Keep "8 Av", "14 St", "Lorimer St" whole when the line wraps.
    text = re.sub(r"(\S+) (Av|Avs|St|Sq)\b", "\\1\u00a0\\2", text)
    text = re.sub(r"\b(\d+) ", "\\1\u00a0", text)
    return text[:1].upper() + text[1:]


def fetch_line_alerts():
    if not ALERTS_URL:
        return []
    out = []
    for ent in _get(ALERTS_URL).get("entity", []):
        a = ent.get("alert", {})
        kind = a.get("transit_realtime.mercury_alert", {}).get("alert_type", "")
        if kind in IGNORED_TYPES:
            continue
        informed = [i for i in a.get("informed_entity", [])
                    if i.get("route_id") == ROUTE]
        if not informed:
            continue
        stops = {i["stop_id"][:3] for i in a.get("informed_entity", [])
                 if i.get("stop_id")}
        if stops and not stops & OUR_STOPS:
            continue
        # The header can carry several notices for several lines; the first
        # line is the one that names ours often enough, and it is the headline.
        text = _en(a.get("header_text")).strip().split("\n")[0].strip()
        text = _tidy(text)
        if not text:
            continue
        periods = [(p.get("start", 0), p.get("end") or None)
                   for p in a.get("active_period", [])]
        out.append({"text": text, "type": kind, "periods": periods})
    return out


def _when(t, now):
    d, today = datetime.fromtimestamp(t), datetime.fromtimestamp(now)
    hm = d.strftime("%-I:%M %p").replace(":00 ", " ")
    days = (d.date() - today.date()).days
    if days == 0:
        return hm
    if days == 1:
        return "tomorrow " + hm
    return d.strftime("%a ") + hm


def line_items(alerts, now):
    """(title, sub) per alert in force now or starting within LINE_HORIZON."""
    items = []
    for a in alerts or []:
        cur = next((p for p in a["periods"]
                    if p[0] <= now and (p[1] is None or now < p[1])), None)
        if cur:
            sub = "until " + _when(cur[1], now) if cur[1] else "now"
            items.append((0, a["text"], sub))
            continue
        nxt = min((p for p in a["periods"] if now < p[0] <= now + LINE_HORIZON),
                  default=None)
        if nxt:
            sub = "from " + _when(nxt[0], now)
            if nxt[1]:
                sub += " to " + _when(nxt[1], nxt[0])
            items.append((nxt[0], a["text"], sub))
    items.sort()
    return [(t, s) for _, t, s in items]


if __name__ == "__main__":
    now = time.time()
    print("rain:", rain_item(fetch_rain(), now))
    alerts = fetch_line_alerts()
    print("line alerts (%d relevant):" % len(alerts))
    for a in alerts:
        print("  ", a["type"], "|", a["text"])
    print("shown:", line_items(alerts, now))
