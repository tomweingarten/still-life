"""Live transit arrivals from any GTFS-realtime trip-updates feed, dependency-free.

The MTA publishes real-time arrivals as GTFS-realtime protobuf. The obvious way
to read it is `gtfs-realtime-bindings` + `protobuf`, but this box is PEP 668
managed and the project's rule is no venv, no pip -- see ha.py. So this walks
the protobuf wire format by hand. We only need a handful of fields, and the
wire format is simple enough that a ~40-line decoder is less fragile than a
build-time codegen dependency that has to survive an unattended cron job.

    from transit import next_arrivals
    next_arrivals(FEED_URL, "L08N")   # -> [3, 7, 12]  (minutes away)

Stop ids are the agency's GTFS stop ids. Example: the NYC subway's L line feed
is https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-l, where
"L08" is Bedford Av and the "N"/"S" suffix is direction. No API key is required
for MTA feeds; other agencies may need one in the URL.
"""

import time
import urllib.request


# --- minimal protobuf wire reader -----------------------------------------
# Returns {field_number: [values]}. A length-delimited value is raw bytes
# (decode again for a nested message); a varint is an int. Other wire types
# we don't need are skipped but their bytes are consumed so parsing stays sync.


def _varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _fields(buf):
    out = {}
    i, n = 0, len(buf)
    while i < n:
        tag, i = _varint(buf, i)
        field, wire = tag >> 3, tag & 0x07
        if wire == 0:                       # varint
            val, i = _varint(buf, i)
        elif wire == 2:                     # length-delimited
            length, i = _varint(buf, i)
            val, i = buf[i:i + length], i + length
        elif wire == 1:                     # 64-bit
            val, i = buf[i:i + 8], i + 8
        elif wire == 5:                     # 32-bit
            val, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"bad wire type {wire}")
        out.setdefault(field, []).append(val)
    return out


def _first(fields, key):
    v = fields.get(key)
    return v[0] if v else None


def arrivals(feed_url, stop_id):
    """Unix arrival times (seconds) for `stop_id`, future only, sorted."""
    req = urllib.request.Request(feed_url, headers={"User-Agent": "still-life/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        feed = _fields(resp.read())

    now = time.time()
    times = []
    for entity in feed.get(2, []):              # FeedMessage.entity
        tu = _first(_fields(entity), 3)         # FeedEntity.trip_update
        if tu is None:
            continue
        for stu in _fields(tu).get(2, []):      # TripUpdate.stop_time_update
            f = _fields(stu)
            sid = _first(f, 4)                  # StopTimeUpdate.stop_id
            if sid is None or sid.decode() != stop_id:
                continue
            ev = _first(f, 2) or _first(f, 3)   # arrival, else departure
            if ev is None:
                continue
            raw = _first(_fields(ev), 2)        # StopTimeEvent.time (varint int64)
            if raw is None:
                continue
            t = raw if isinstance(raw, int) else _varint(raw, 0)[0]
            if t >= now - 30:                   # keep just-now, drop stale
                times.append(t)
    return sorted(times)


def next_arrivals(feed_url, stop_id, limit=4):
    """Whole minutes until the next `limit` trains at `stop_id`.

    Floored, not rounded, and deliberately so: rounding tells you a train 2.6
    minutes out is "3", i.e. that you have more time than you really do, which
    is the one error that makes you miss it. Flooring is always conservative.
    0 means under a minute away; the Hub renders
    that as a plain "0 min" like any other figure.
    """
    now = time.time()
    return [max(0, int((t - now) // 60)) for t in arrivals(feed_url, stop_id)[:limit]]


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        sys.exit("usage: python3 transit.py FEED_URL STOP_ID")
    print(f"{sys.argv[2]}: next arrivals (min) = {next_arrivals(sys.argv[1], sys.argv[2])}")
