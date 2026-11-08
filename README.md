# Still Life

A Google Nest Hub as an always-on art display: a public-domain botanical plate with a gallery label, the time, the next departures from your stop, the weather, and small graphs of anything Home Assistant records.

It runs on a small home server and casts to the Hub once. The picture then updates live, every second, without the Hub ever reloading.

## What's on it

- **Art:** one plate per day from Köhler's *Medizinal-Pflanzen* (1887), downloaded from Wikimedia Commons by `fetch_art.py`. The type colour is derived from each plate. At night the art dims and the ground goes dark.
- **Clock and label:** the time, and the plate's species and source.
- **Departures** (optional): minutes until the next trains or buses from any GTFS-realtime feed, hiding ones you can't walk to in time, plus service alerts for your line (MTA feeds).
- **Weather:** a daily forecast from Home Assistant, with a rain nowcast from Open-Meteo as its first row when rain is on the way.
- **Graph cells:** a label, a big number, the change and a sparkline, for any Home Assistant entity's history or any JSON file. Configure as many as fit.
- **Takeovers:** put an image (or a folder for a slideshow) on screen until a set time.

## Why a video

Every way of putting a still image on a Nest Hub reloads it, and the screen blinks black each time. These were all tried against a real Hub and failed:

- **DashCast or any custom web receiver:** the app launches, but smart displays never fetch the page.
- **HLS:** the receiver loads the playlist and first segment, then fails, for every variant tried.

A plain progressive MP4 does play. So Still Life serves one that never ends: fragmented MP4 from ffmpeg over a single chunked HTTP response, fed one rendered frame per second. The cast happens once; after that the Hub just keeps playing. `still_life.py` has the details, including how it measures the Hub's buffering delay so the clock on screen is right.

## Setup

Needs Python 3.11+, ffmpeg, Pillow, PyChromecast and the Noto Sans font (`fonts-noto-core` on Debian/Ubuntu).

```sh
python3 -m venv .venv && .venv/bin/pip install pillow pychromecast
cp settings.example.toml settings.toml      # Home Assistant, Hub address, location, graphs
mkdir -p data && echo "YOUR_HA_TOKEN" > data/ha_token
.venv/bin/python fetch_art.py               # download the plates (~80)
.venv/bin/python still_life.py              # render, stream and cast
```

`still_life.py --no-cast` serves the stream without touching the Hub, for testing: open `http://<this machine>:8099/live.mp4`. `deploy/still-life.service` is an example systemd unit.

## Graph cells

Each `[[graphs]]` entry in `settings.toml` is one cell, drawn left to right. `type = "ha"` graphs any entity's long-term statistics. `type = "json"` reads a number series out of a JSON file another program writes. The options are documented at the top of `graphs.py` and in `settings.example.toml`.

## Takeovers

Write `data/out/takeover.json`:

```json
{"image": "/path/to/photo.jpg", "until": "2026-12-31T23:00"}
{"slideshow": "/path/to/folder", "every": 20, "fade": 3, "until": "2026-12-31T23:00"}
```

Past `until` the file is ignored and the normal board returns.

## Privacy

`settings.toml`, the data folder and the token are gitignored, and the pre-commit hook blocks them. Turn it on with `git config core.hooksPath .githooks`. The stream contains everything on screen, so serve it only on your home network.

## License

Code: MIT (see `LICENSE`). Icons: Lucide (ISC), in `fonts/`. Art: public domain, downloaded by you; not included.
