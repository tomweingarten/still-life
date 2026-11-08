"""Still Life settings: settings.toml next to this file (or $STILL_LIFE_SETTINGS).

Everything personal lives in the data directory and settings.toml, both
gitignored. See settings.example.toml for every option.
"""

import os
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
_file = Path(os.environ.get("STILL_LIFE_SETTINGS") or HERE / "settings.toml").expanduser()
S = tomllib.loads(_file.read_text()) if _file.exists() else {}


def get(section, key, default=None):
    return S.get(section, {}).get(key, default)


DATA = Path(os.environ.get("STILL_LIFE_DATA") or S.get("data_dir") or HERE / "data").expanduser()
ART_DIR = Path(get("art", "dir") or DATA / "art").expanduser()
OUT = DATA / "out"
OUT.mkdir(parents=True, exist_ok=True)

# Home Assistant
HA_HOST = get("home_assistant", "host", "homeassistant.local")
HA_PORT = int(get("home_assistant", "port", 8123))
HA_TOKEN_FILE = Path(get("home_assistant", "token_file") or DATA / "ha_token").expanduser()
WEATHER_ENTITY = get("weather", "entity", "weather.forecast_home")

# Location, for the rain nowcast (Open-Meteo).
LAT = float(get("location", "latitude", 0.0))
LON = float(get("location", "longitude", 0.0))
HAS_LOCATION = "location" in S

# Nest Hub and the stream it plays.
HUB_IP = get("hub", "ip", "")
STREAM_PORT = int(get("hub", "stream_port", 8099))

# Night mode: dark ground and dimmed art between these hours.
NIGHT_START = int(get("display", "night_start", 21))
NIGHT_END = int(get("display", "night_end", 7))
HOUR24 = bool(get("display", "clock_24h", False))

# Transit (optional): any GTFS-realtime trip-updates feed.
TRANSIT = S.get("transit")

# Graph cells for the bottom band (see graphs.py).
GRAPHS = S.get("graphs", [])
