#!/usr/bin/env python3
"""Fetch public-domain scientific botanical plates from Wikimedia Commons.

Köhler's *Medizinal-Pflanzen* (1887) is the target collection: chromolithographs
with flat saturated colour, hard black outlines and inset dissection details,
which read well from across a room on a small screen.

Writes the images into the art folder and their provenance into credits.json,
so the board can label a plate properly instead of guessing from a filename.
Wikimedia asks for a contact in the User-Agent: set [art] contact in settings.toml.

    python3 fetch_art.py            # fetch the curated species list
    python3 fetch_art.py --list     # show what is already downloaded
"""

import argparse
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://commons.wikimedia.org/w/api.php"
import settings

UA = "still-life/1.0 (home art display; %s)" % settings.get("art", "contact", "no contact set")
ART = settings.ART_DIR
CREDITS = ART / "credits.json"
CATEGORY = "Category:Köhlers Medizinal-Pflanzen"
WIDTH = 2000                     # plenty for a 1280x800 Nest Hub Max

# Commons holds several uploads of many plates, and title search happily returns
# a 460x590 duplicate. Enumerating the category and filtering on the *original*
# scan height is the only reliable way to get plates that survive being scaled to
# 1200x1600. Below this, upscaling turns the lithograph's line work to mush.
MIN_HEIGHT = 1500
WANT = 18


def api(**params):
    params.setdefault("format", "json")
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def slug(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def all_plates():
    """Every file title in the Köhler category."""
    titles, cont = [], None
    while True:
        kw = {"cmcontinue": cont} if cont else {}
        r = api(action="query", list="categorymembers", cmtitle=CATEGORY,
                cmtype="file", cmlimit=500, **kw)
        titles += [m["title"] for m in r["query"]["categorymembers"]]
        cont = r.get("continue", {}).get("cmcontinue")
        if not cont:
            return titles
        time.sleep(0.3)


def plate_infos(titles):
    """imageinfo for many titles, batched 40 at a time."""
    out = {}
    for i in range(0, len(titles), 40):
        batch = titles[i:i + 40]
        r = api(action="query", titles="|".join(batch), prop="imageinfo",
                iiprop="url|size|extmetadata", iiurlwidth=WIDTH)
        for page in r.get("query", {}).get("pages", {}).values():
            info = (page.get("imageinfo") or [None])[0]
            if not info:
                continue
            meta = info.get("extmetadata", {})

            def field(key):
                v = meta.get(key, {}).get("value", "")
                return re.sub(r"<[^>]+>", "", v).strip()

            out[page["title"]] = {
                "url": info.get("thumburl") or info["url"],
                "orig_h": info.get("height", 0),
                "width": info.get("thumbwidth") or info.get("width"),
                "height": info.get("thumbheight") or info.get("height"),
                "artist": field("Artist") or "Franz Eugen Köhler",
                "license": field("LicenseShortName") or "Public domain",
            }
        time.sleep(0.3)
    return out


def species_of(title):
    """'File:Atropa belladonna - Köhler–s ...-018.jpg' -> 'Atropa belladonna'."""
    stem = title[len("File:"):].rsplit(".", 1)[0]
    return re.split(r"\s+-\s+", stem)[0].strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    ART.mkdir(parents=True, exist_ok=True)
    credits = json.loads(CREDITS.read_text()) if CREDITS.exists() else {}

    if args.list:
        for name, c in sorted(credits.items()):
            print(f"{name:<44} {c.get('title','?')}")
        print(f"\n{len(credits)} plate(s) with credits in {ART}")
        return

    titles = all_plates()
    print(f"{len(titles)} files in {CATEGORY}")
    infos = plate_infos(titles)

    # One entry per species, keeping the largest scan, then filter on height.
    best = {}
    for title, info in infos.items():
        sp = species_of(title)
        if not sp or info["orig_h"] < MIN_HEIGHT:
            continue
        if sp not in best or info["orig_h"] > best[sp][1]["orig_h"]:
            best[sp] = (title, info)
    print(f"{len(best)} species with a scan >= {MIN_HEIGHT}px tall")

    # Spread the pick across the alphabet rather than taking the first N, so the
    # set is not eighteen plates whose names all start with A.
    names = sorted(best)
    step = max(1, len(names) // WANT)
    chosen = names[::step][:WANT]

    for sp in chosen:
        title, info = best[sp]
        name = f"koehler-{slug(sp)}.jpg"
        dest = ART / name
        if dest.exists() and name in credits:
            print(f"have  {name}")
            continue
        req = urllib.request.Request(info["url"], headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        dest.write_bytes(data)
        credits[name] = {
            "title": sp,
            "plate": "Köhler's Medizinal-Pflanzen (1887)",
            "artist": info["artist"],
            "license": info["license"],
            "source": f"https://commons.wikimedia.org/wiki/{urllib.parse.quote(title)}",
        }
        print(f"got   {name}  {info['width']}x{info['height']}  {len(data)//1024} KB")
        CREDITS.write_text(json.dumps(credits, indent=1, ensure_ascii=False))
        time.sleep(0.6)          # be polite to Commons

    CREDITS.write_text(json.dumps(credits, indent=1, ensure_ascii=False))
    print(f"\n{len(credits)} plate(s) total, credits in {CREDITS}")


if __name__ == "__main__":
    main()
