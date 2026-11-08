"""Graph cells for the bottom band: a label, a big number, a note and a sparkline.

Each [[graphs]] entry in settings.toml is one cell, drawn left to right. Two types:

  type = "ha"    Home Assistant long-term statistics for any entity.
      entity    = "sensor.living_room_temperature"
      label     = "Living room"      (default: the entity's friendly name)
      days      = 30                 (history to draw)
      smooth    = 7                  (rolling-mean window, in points; 1 = raw)
      decimals  = 1
      unit      = "°F"               (default: the entity's unit)
    The number is the latest value; the note is the change over the window.

  type = "json"  A JSON file written by something else.
      path      = "/path/to/file.json"
      label     = "Strength"
      series    = "weeks[].index"    (a list of numbers, or a list of objects + a field)
      value     = "delta_pct"        (key for the big number; default: last series point)
      format    = "{:+.1f}%"         (Python format for the number)
      note      = "since {baseline}" ({key} placeholders; ISO dates print as "Jun 8")
      updated   = "generated"        (optional timestamp key...)
      stale_after_days = 4           (...after which the note becomes stale_note)
      stale_note = "as of {asof}"

A cell whose source fails shows "no data" rather than taking the frame down.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import ha
import settings


def smooth(values, window):
    """Centred rolling mean, shrinking the window at the edges."""
    if window <= 1 or len(values) < 2:
        return list(values)
    half = window // 2
    out = []
    for i in range(len(values)):
        chunk = values[max(0, i - half):min(len(values), i + half + 1)]
        out.append(sum(chunk) / len(chunk))
    return out


def _empty(label):
    return {"label": label.upper(), "value": "", "unit": "", "note": "", "direction": 0, "series": None}


def _direction(delta, eps):
    return 0 if abs(delta) <= eps else (1 if delta > 0 else -1)


def ha_cell(c):
    entity = c["entity"]
    state = ha.state(entity)
    attrs = state.get("attributes", {})
    label = c.get("label") or attrs.get("friendly_name") or entity
    stats = ha.statistics(entity, days=int(c.get("days", 30)))
    if not stats:
        return _empty(label)
    dec = int(c.get("decimals", 1))
    values = [s[1] for s in stats]
    curve = smooth(values, min(int(c.get("smooth", 7)), max(1, len(values))))
    delta = curve[-1] - curve[0]
    span = max(1, (stats[-1][0] - stats[0][0]).days)
    return {"label": label.upper(), "value": f"{values[-1]:.{dec}f}",
            "unit": c.get("unit", attrs.get("unit_of_measurement", "")),
            "note": f"{abs(delta):.{dec}f} / {span}d",
            "direction": _direction(delta, 0.5 * 10 ** -dec), "series": curve}


def _short(v):
    """'2026-06-08' -> 'Jun 8'; anything else unchanged."""
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).strftime("%b %-d")
    except ValueError:
        return v


class _Fields(dict):
    def __missing__(self, key):
        return "?"


def _series(data, path):
    key, _, field = path.partition("[].")
    items = data.get(key) or []
    return [float(i[field] if field else i) for i in items]


def json_cell(c):
    label = c.get("label", Path(c["path"]).stem)
    data = json.loads(Path(c["path"]).expanduser().read_text())
    series = _series(data, c.get("series", "series"))
    if not series:
        return _empty(label)
    raw = data.get(c["value"]) if c.get("value") else series[-1]
    value = c.get("format", "{}").format(float(raw))
    fields = _Fields({k: _short(v) for k, v in data.items() if not isinstance(v, (list, dict))})
    note = c.get("note", "").format_map(fields)
    upd = data.get(c.get("updated", ""))
    if upd and "stale_after_days" in c:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(str(upd).replace("Z", "+00:00"))
            if age.days >= int(c["stale_after_days"]):
                note = c.get("stale_note", "as of {%s}" % c["updated"]).format_map(fields)
        except ValueError:
            pass
    trend = float(raw) if c.get("value") else series[-1] - series[0]
    return {"label": label.upper(), "value": value, "unit": c.get("unit", ""), "note": note,
            "direction": _direction(trend, 0.05), "series": series}


TYPES = {"ha": ha_cell, "json": json_cell}


def fetch_all():
    """One dict per configured cell, in order. Never raises."""
    cells = []
    for c in settings.GRAPHS:
        try:
            cells.append(TYPES[c.get("type", "ha")](c))
        except Exception as exc:
            print(f"graph {c.get('label') or c.get('entity') or c.get('path')} failed: {exc}", file=sys.stderr)
            cells.append(_empty(c.get("label") or c.get("entity") or "?"))
    return cells
