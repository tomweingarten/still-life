#!/usr/bin/env python3
"""Still Life: the Nest Hub as a live video of the board, not a
re-cast still.

Why this exists
---------------
Casting a still PNG every minute means *every cast reloads the image*,
so the Hub blinks black once a minute. A still image cannot tick a clock or a
countdown by itself, so the blink was the price of correct data. This service
removes the trade: it casts **once**, and then keeps feeding the same open
connection, so the picture updates without the receiver ever reloading.

What did not work (all verified against a Nest Hub, 2026-08-29)
-------------------------------------------------------------------
* **DashCast / any web receiver.** The app launches and reports "Application
  ready", but the Hub never fetches the URL -- not over http, not over valid
  https. Smart displays do not render custom web receivers. That also rules out
  the Lovelace-dashboard plan in the README.
* **HLS.** The receiver fetches the playlist and the first segment, then returns
  LOAD_FAILED, for live and VOD, MPEG-TS and fMP4, with and without an audio
  track. (Two red herrings on the way: `python -m http.server` sends no CORS
  header, and the receiver pulls HLS by XHR, so the request arrives and the
  response is discarded; and `.ts` is served as a Qt translation type.)
* A plain progressive MP4 **does** play. So the winning shape is the one the
  receiver already likes -- a single `<video>` source -- that simply never ends:
  fragmented MP4 over one chunked HTTP response, held open indefinitely.

Shape of the thing
------------------
    render thread  -> keeps `Frame.latest` fresh (once a minute, or when data moves)
    pump thread    -> writes `latest` into ffmpeg at a strict 1 fps wall clock
    ffmpeg         -> fragmented MP4 on stdout
    HTTP /live.mp4 -> that stdout, chunked, never closed
    cast thread    -> makes sure the Hub is actually playing it

Pacing is deliberately decoupled from rendering. ffmpeg timestamps image input
by frame index, so if frames were emitted only as fast as they render, a 0.6 s
render would stretch the timeline, the player's buffer would drain, and the
stream would stall. The pump therefore ships the *last finished* frame on a
strict one-second cadence and never waits for the renderer.
"""

import argparse
import bisect
import fcntl
import os
import json
import random
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import alerts
import board
import chime
import graphs
import ha
import layout
import settings
import transit

W, H = board.W, board.H
PORT = settings.STREAM_PORT
STREAM_PATH = "/live.mp4"
AMOVIE_EATS = 65536
FPS_IN = 1                 # frames we hand ffmpeg per second
# Encoded rate. Plain fps=2 costs ~3% of one N150 core. A slideshow crossfade
# drawn in 1 s steps needs the framerate filter blending up to 10 fps to play
# smooth, and that costs ~16% -- so it is used only while a slideshow is up
# (2026-09-27: left global, it ran all night after the party slideshow ended).
FPS_OUT = 2
FPS_OUT_SMOOTH = 10
HUB_IP = settings.HUB_IP      # [hub] ip, or --hub-ip

# The current stream: whether it was encoded smooth, and the wall clock at which
# each input frame was pumped, as parallel lists (frame index = position in
# seconds, since FPS_IN = 1). Looking the Hub's play position up here gives the
# lag exactly. The old start + position arithmetic assumed the pump never
# slips, but a paused Hub stops reading, the pump blocks and skips frames on
# resume, and every pause then inflated the "lag" -- past MAX_OFFSET, where
# samples were thrown away and the clock kept a stale offset (2026-09-27:
# computed 80 s, displayed with 20.6 s).
STREAM = {"smooth": False, "idx": [], "at": []}
PUMP_HISTORY = 7200        # frames of pump times kept (2 h at 1 fps)
MAX_OFFSET = 600.0
# The measured offset survives restarts. It only re-converges once the Hub is
# playing again, and until then the clock would read a quarter-minute late --
# which is the one failure this display is not allowed to have.
OFFSET_FILE = settings.OUT / ".clock_offset"
# A still picture that replaces the whole board until a set time (a party, an
# announcement). {"image": "<path>", "until": "YYYY-MM-DDTHH:MM"}. Or a folder
# shuffled with crossfades: {"slideshow": "<dir>", "every": 20, "fade": 3,
# "until": ...}. Read every render tick, so it goes up and comes down without a
# restart or a re-cast.
TAKEOVER_FILE = settings.OUT / "takeover.json"

TRAIN_EVERY = 30           # the countdown is the only fast-moving number
ALERTS_EVERY = 300         # rain nowcast (15-min steps) and L service alerts
SLOW_EVERY = 600           # weather and graph cells: hours-scale data
CAST_CHECK_EVERY = 30
# BUFFERING with the play position frozen this long is a stall, not a load.
STUCK_BUFFERING = 90
# The lag only grows: each self-pause resumes where it stopped, adding its
# length (~9 s each on 2026-09-27; a fresh cast starts ~14 s behind). The clock
# is drawn for the lag, so it stays right, but new data (trains) arrives that
# late. A re-cast resets it at the price of ~15 s of black and the cast chime,
# so: once a day while nobody is looking, and in the day only past RECAST_LAG.
RECAST_LAG = 60
RECAST_HOUR = 4            # nightly reset, local hour
RECAST_NIGHT_MIN = 20      # ...skipped if already this close to live
RECAST_COOLDOWN = 600      # never lag-re-cast twice within this many seconds

# The Hub's own idle screen (Backdrop) and the default media receiver. Anything
# else in front means a person is playing something -- leave it alone.
IDLE_APPS = {"E8C28D3C", "CC1AD845", None, ""}


def lan_ip():
    """The address the Hub has to reach us on: [hub] host_ip, else the address
    this box uses to reach the Hub (no packet is actually sent)."""
    if settings.get("hub", "host_ip"):
        return settings.get("hub", "host_ip")
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((HUB_IP or "192.0.2.1", 8009))
        return s.getsockname()[0]
    finally:
        s.close()


def active_takeover(now):
    """(image path, mtime) if a takeover is in force at `now`, else None --
    or ("slideshow", dir, every, fade) for a folder.

    Past `until` the file is simply ignored, so one left behind can't hijack
    the screen; a missing or broken file means the normal board.
    """
    try:
        spec = json.loads(TAKEOVER_FILE.read_text())
        if now >= datetime.fromisoformat(spec["until"]):
            return None
        if "slideshow" in spec:
            folder = Path(spec["slideshow"]).expanduser()
            if not folder.is_dir():
                return None
            return ("slideshow", folder, float(spec.get("every", 20)),
                    float(spec.get("fade", 3)))
        path = Path(spec["image"]).expanduser()
        return path, path.stat().st_mtime
    except (OSError, ValueError, KeyError, TypeError):
        return None


def fit_to_screen(path):
    """The image centred on white at W x H, scaled to fit without cropping."""
    img = Image.open(path).convert("RGB")
    if img.size == (W, H):
        return img
    s = min(W / img.width, H / img.height)
    img = img.resize((round(img.width * s), round(img.height * s)),
                     Image.LANCZOS)
    out = Image.new("RGB", (W, H), (255, 255, 255))
    out.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
    return out


class Slideshow:
    """A folder of pictures, shuffled, one every `every` s with a `fade` s
    crossfade.

    New arrivals jump the queue, so a fresh photostrip is up within one slot;
    at startup the newest goes first and the rest are shuffled. The fade is
    drawn in render-tick (1 s) steps and ffmpeg's framerate filter blends
    between them. Only the outgoing and incoming pictures stay decoded.
    """

    EXTS = (".png", ".jpg", ".jpeg")

    def __init__(self):
        self.cur = self.prev = None
        self.since = 0.0
        self.seen = set()
        self.bag = []
        self.cache = {}              # path -> (mtime, fitted image)

    def _load(self, path):
        try:
            m = path.stat().st_mtime
        except OSError:              # gone (the placeholder, say): fade from memory
            hit = self.cache.get(path)
            return hit[1] if hit else None
        hit = self.cache.get(path)
        if hit is None or hit[0] != m:
            hit = self.cache[path] = (m, fit_to_screen(path))
        return hit[1]

    def _next(self, files):
        fresh = [p for p in files if p not in self.seen]
        if fresh:
            if not self.seen:        # startup: everything is "new"; lead with the newest
                self.seen.update(files)
            return max(fresh, key=lambda p: p.stat().st_mtime)
        self.bag = [p for p in self.bag if p in files and p != self.cur]
        if not self.bag:
            self.bag = [p for p in files if p != self.cur] or list(files)
            random.shuffle(self.bag)
        return self.bag.pop()

    def render(self, folder, every, fade, t):
        """(signature, image) for wall time t, or None for an empty folder."""
        files = sorted(p for p in folder.iterdir() if p.suffix.lower() in self.EXTS)
        if not files:
            return None
        if self.cur not in files or t - self.since >= every:
            nxt = self._next(files)
            self.seen.add(nxt)
            self.prev = self.cur if self.cur != nxt else None
            self.cur, self.since = nxt, t
            self.cache = {p: v for p, v in self.cache.items()
                          if p in (self.prev, self.cur)}
        cur = self._load(self.cur)
        a = (t - self.since) / fade if fade > 0 else 1.0
        prev = self._load(self.prev) if self.prev and a < 1 else None
        if prev is None:
            return ("slideshow", self.cur, self.cache[self.cur][0]), cur
        return ("slideshow", self.prev, self.cur, round(a, 2)), Image.blend(prev, cur, a)


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# --------------------------------------------------------------------- frames


class Frame:
    """The current picture, kept fresh by its own thread.

    Holds raw RGB bytes rather than PNG: the pump ships a frame every second and
    re-encoding an unchanged picture 86,400 times a day is pure waste. rawvideo
    also spares ffmpeg the decode.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.raw = None
        self.sig = None
        self.data = ([], [])   # forecast, graph cells
        # Raw unix arrival times, not floored minutes.
        # The countdown is then computed at *render* time against the effective
        # clock below, so it ticks down between fetches and -- more importantly
        # -- the number is right for the moment it is actually on screen. With
        # minutes frozen at fetch time, the display lag ate into the flooring
        # guarantee: a "1" could be 47 s out, and walk-time exists precisely so
        # a displayed train is one you can still catch. None = feed down.
        self.arrivals = None
        # Raw rain slots and line alerts; phrased at render time (alerts.py).
        self.rain = None
        self.line_alerts = None
        self.rendered_at = None
        # Seconds the Hub is behind real time. The receiver pre-buffers before it
        # starts playing and that offset then persists for the life of the
        # stream, so a clock drawn at render time reads ~15 s late on the wall.
        # Measured live by the cast thread rather than guessed; a wrong clock is
        # the one thing this display must not do.
        self.offset = 0.0
        self.slideshow = Slideshow()
        self.smooth = False

    # -- data -------------------------------------------------------------
    def refresh_slow(self):
        # Each source is guarded: no single dead one blanks the frame.
        try:
            forecast = ha.daily_forecast(settings.WEATHER_ENTITY, days=5)
        except Exception as exc:
            log("forecast fetch failed: %s" % exc)
            forecast = []
        cells = graphs.fetch_all()
        with self.lock:
            self.data = (forecast, cells)

    def refresh_alerts(self):
        # Separately guarded: a dead feed just drops its rows, and the old
        # data is kept rather than blanked, since both change slowly.
        if settings.HAS_LOCATION:
            try:
                rain = alerts.fetch_rain()
                with self.lock:
                    self.rain = rain
            except Exception as exc:
                log("rain fetch failed: %s" % exc)
        try:
            line_alerts = alerts.fetch_line_alerts()
            with self.lock:
                self.line_alerts = line_alerts
        except Exception as exc:
            log("line alerts fetch failed: %s" % exc)

    def alert_items(self, when):
        """[(kind, title, sub)] for the bottom band, as of unix time `when`.

        Line trouble in force now comes first, then the rain, then planned work
        that has not started yet.
        """
        with self.lock:
            rain, line_alerts = self.rain, self.line_alerts
        l = alerts.line_items(line_alerts, when)
        now_l = [("line",) + x for x in l if not x[1].startswith("from ")]
        later_l = [("line",) + x for x in l if x[1].startswith("from ")]
        r = alerts.rain_item(rain, when)
        kind = "snow" if r and r[0].startswith("Snow") else "rain"
        return now_l + ([(kind,) + r] if r else []) + later_l

    def refresh_trains(self):
        if not settings.TRANSIT:
            return
        try:
            times = transit.arrivals(settings.TRANSIT["feed_url"], settings.TRANSIT["stop_id"])
        except Exception as exc:
            log("subway fetch failed: %s" % exc)
            return
        with self.lock:
            self.arrivals = times

    def countdown(self, when):
        """Whole minutes to the next trains, as of `when` (a unix time).

        Floored, matching transit.next_arrivals: a displayed number never claims
        more time than you have.
        """
        with self.lock:
            times = self.arrivals
        if times is None:
            return None
        return [max(0, int((t - when) // 60))
                for t in times if t >= when - 30][:4]

    # -- picture ----------------------------------------------------------
    def maybe_render(self):
        with self.lock:
            offset = self.offset
        # The effective clock: what the wall will read when this frame reaches
        # the screen. Everything time-dependent is drawn against it.
        now = datetime.now() + timedelta(seconds=offset)
        takeover = active_takeover(now)
        # Read by the cast thread: a mismatch with the running stream re-casts
        # so the encoder is rebuilt at the right rate.
        self.smooth = bool(takeover and takeover[0] == "slideshow")
        if takeover and takeover[0] == "slideshow":
            shown = self.slideshow.render(*takeover[1:], time.time())
            if shown:
                sig, img = shown
                if sig == self.sig and self.raw is not None:
                    return False
                raw = img.tobytes()
                with self.lock:
                    self.raw, self.sig, self.rendered_at = raw, sig, now
                if len(sig) == 3:
                    log("slideshow: showing %s" % sig[1].name)
                return True
            takeover = None          # empty folder: the normal board
        if takeover:
            sig = ("takeover",) + takeover
            if sig == self.sig and self.raw is not None:
                return False
            raw = fit_to_screen(takeover[0]).tobytes()
            with self.lock:
                self.raw, self.sig, self.rendered_at = raw, sig, now
            log("takeover: showing %s" % takeover[0])
            return True
        with self.lock:
            data = self.data
        forecast, cells = data
        trains = self.countdown(time.time() + offset)
        art = board.art_for_day(now)
        night = not (settings.NIGHT_END <= now.hour < settings.NIGHT_START)
        items = self.alert_items(time.time() + offset)
        sig = (signature(forecast, trains, cells, night, art, now.strftime("%H:%M")),
               tuple(items))
        if sig == self.sig and self.raw is not None:
            return False

        t0 = time.time()
        img = layout.build_overlay(art, forecast, trains, cells, now=now, alerts=items)
        raw = img.convert("RGB").tobytes()
        with self.lock:
            self.raw, self.sig, self.rendered_at = raw, sig, now
        log("rendered in %.2fs (%s)" % (time.time() - t0, now.strftime("%H:%M")))
        return True

    def current(self):
        with self.lock:
            return self.raw


def signature(forecast, trains, cells, night, art, minute):
    """Fingerprint of what is on screen: an unchanged one skips the render.
    The minute is part of it, so the clock is never stale."""
    today = forecast[0] if forecast else {}
    return "|".join(str(x) for x in (
        trains, today.get("temperature"), today.get("templow"), today.get("condition"),
        tuple((c["label"], c["value"], c["note"]) for c in cells),
        night, Path(art).name if art else "-", minute))


def render_loop(frame, stop):
    """Keep the picture current: slow data rarely, trains often, redraw on change.

    Every fetch is already guarded in refresh_*, but the loop itself
    is wrapped too -- this thread dying would freeze the display on a stale
    frame while the stream kept happily serving it, which is exactly the
    failure a frozen display teaches you to distrust.
    """
    last_slow = 0.0
    last_train = 0.0
    last_alerts = 0.0
    while not stop.is_set():
        try:
            now = time.time()
            if now - last_slow >= SLOW_EVERY:
                frame.refresh_slow()
                last_slow = now
            if now - last_alerts >= ALERTS_EVERY:
                frame.refresh_alerts()
                last_alerts = now
            if now - last_train >= TRAIN_EVERY:
                frame.refresh_trains()
                last_train = now
            frame.maybe_render()
        except Exception as exc:
            log("render loop error: %s" % exc)
        stop.wait(1.0)


# --------------------------------------------------------------------- stream


def ffmpeg_cmd(smooth=False, audio_fd=None):
    fps = FPS_OUT_SMOOTH if smooth else FPS_OUT
    vf = ("format=yuv420p,framerate=fps=%d:interp_start=0:interp_end=255"
          ":scene=100" % fps) if smooth else "fps=%d,format=yuv420p" % fps
    # The bells (chime.py) arrive as raw PCM on a second pipe, one second per
    # video frame, so both are timestamped by the same pump ticks. The pipe is
    # opened by an amovie *filter*, not as a second -i: with two input files
    # ffmpeg reads pipes non-blocking, and the rawvideo reader then takes a
    # short read for a whole frame and fails ("packet size 65536 < expected
    # frame_size").
    audio = ([] if audio_fd is None else
             ["-filter_complex",
              # s16le defaults to 44.1 kHz mono, which is what chime.py makes.
              "amovie=filename=/dev/fd/%d:f=s16le:format_opts=probesize=32[a]" % audio_fd,
              "-map", "0:v", "-map", "[a]", "-c:a", "aac", "-b:a", "96k"])
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
        "-framerate", str(FPS_IN), "-i", "-",
        "-vf", vf,
        # zerolatency matters more than it looks: x264's default rc-lookahead is
        # 40 frames, which at 2 fps was 20 seconds of buffered future -- the wall
        # clock on screen was flipping the minute 20 s late. stillimage keeps the
        # quality tuning for a picture that barely moves; x264 takes both.
        "-c:v", "libx264", "-preset", "veryfast",
        "-tune", "stillimage,zerolatency",
        "-g", str(fps * 2), "-keyint_min", str(fps * 2),
        "-sc_threshold", "0", "-b:v", "2000k", "-bf", "0",
        *audio,
        "-flush_packets", "1",
        # empty_moov+frag_keyframe: playable from the first byte, no moov at the
        # end, which a stream that never ends could never write anyway.
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-frag_duration", "500000",
        "-f", "mp4", "pipe:1",
    ]


class StreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    frame = None            # set on the server
    chimes = None

    def log_message(self, fmt, *args):
        log("http %s %s" % (self.client_address[0], fmt % args))

    def _headers(self, ctype="video/mp4"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_HEAD(self):
        if self.path.split("?")[0] != STREAM_PATH:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            at = STREAM["at"]
            body = ("ok rendered=%s offset=%.1fs frames=%d over=%.0fs\n"
                    % (self.frame.rendered_at or "never", self.frame.offset,
                       len(at),
                       at[-1] - at[0] if at else 0)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path != STREAM_PATH:
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        smooth = self.frame.smooth
        audio_r = audio_w = None
        if self.chimes.enabled:
            audio_r, audio_w = os.pipe()
            # Room for several seconds of PCM, so an audio write never blocks
            # the pump ahead of its video write.
            try:
                fcntl.fcntl(audio_w, fcntl.F_SETPIPE_SZ, 1 << 20)
            except OSError:
                pass
        enc = subprocess.Popen(ffmpeg_cmd(smooth, audio_r), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE,
                               pass_fds=(audio_r,) if audio_r is not None else ())
        if audio_r is not None:
            os.close(audio_r)
            # amovie's open swallows its first 64 KiB read (measured: every
            # bell landed 0.74 s early without this), so give it silence to eat.
            os.write(audio_w, bytes(AMOVIE_EATS))
        # A fresh object, so a pump still draining the previous stream cannot
        # write into this one's history.
        STREAM.update(smooth=smooth, idx=[], at=[])
        log("stream started (%s)" % ("smooth %d fps" % FPS_OUT_SMOOTH
                                     if smooth else "%d fps" % FPS_OUT))
        stop = threading.Event()
        pump = threading.Thread(target=self._pump, args=(enc, stop, audio_w),
                                daemon=True)
        pump.start()
        sent = 0
        try:
            while True:
                chunk = enc.stdout.read(16384)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            stop.set()
            try:
                enc.kill()
                enc.wait(timeout=5)      # reap it; unreaped encoders pile up
            except (OSError, subprocess.TimeoutExpired):
                pass
            if audio_w is not None:
                pump.join(timeout=5)     # it fails out on the dead pipes
                try:
                    os.close(audio_w)
                except OSError:
                    pass
            log("viewer %s left after %.1f MB" % (self.client_address[0],
                                                  sent / 1e6))

    def _pump(self, enc, stop, audio_w=None):
        """Feed ffmpeg the latest frame on a strict wall-clock cadence."""
        idx, at = STREAM["idx"], STREAM["at"]
        n = 0
        next_at = time.time()
        while not stop.is_set():
            raw = self.frame.current()
            if raw is not None:
                sent_at = time.time()
                try:
                    if audio_w is not None:
                        pcm = self.chimes.second(sent_at, self.frame.offset)
                        view = memoryview(pcm)
                        while view:
                            view = view[os.write(audio_w, view):]
                    enc.stdin.write(raw)
                    enc.stdin.flush()
                except (BrokenPipeError, OSError):
                    return
                # Stamp the write's start: a blocked write (paused Hub) means
                # this frame reached the pipe late, but it was drawn for then.
                idx.append(n)
                at.append(sent_at)
                if len(idx) > PUMP_HISTORY + 600:
                    del idx[:600], at[:600]
                n += 1
            next_at += 1.0 / FPS_IN
            delay = next_at - time.time()
            if delay < -5:       # fell badly behind; resync rather than sprint
                next_at = time.time()
                delay = 0
            if delay > 0:
                stop.wait(delay)


# ----------------------------------------------------------------------- cast


def measure_lag(st):
    """Seconds between a frame being pumped and the Hub showing it, or None."""
    pos = st.adjusted_current_time
    idx, at = STREAM["idx"], STREAM["at"]
    if pos is None or not idx:
        return None
    try:                     # the pump trims these lists concurrently
        i = bisect.bisect_right(idx, int(pos // FPS_IN)) - 1
        if i < 0:
            return None
        # The position is extrapolated to now, so compare against now.
        return time.time() - at[i] - (pos - idx[i] * FPS_IN)
    except IndexError:
        return None


def cast_loop(url, frame, stop, force_first=True):
    """Make sure the Hub is playing our stream; re-cast only when it is not.

    In steady state this does nothing at all -- which is the entire point, since
    a re-cast is the blink. It matters after a Hub reboot, a network blip, or
    someone casting music and finishing.

    One connection is held open rather than dialled every check: the sender
    session is what keeps the receiver's media session alive, and reconnecting
    on a timer churns sockets on a device whose cast port is already known to
    wedge (it needed a power-cycle in August).

    Deliberately *not* an HA service call: HA returns 200 for play_media against
    an unavailable entity, which is how a dead display went unnoticed for 29
    hours. pychromecast talks to the device and reports what it is really doing.
    """
    import pychromecast

    cast = None
    first = force_first
    # Play position when we last issued mc.play(), and how many resumes we have
    # fired without seeing that position move. A resume that worked advances the
    # stream even if the Hub pauses again seconds later, which it does; only a
    # position frozen where we left it means play() is landing on a dead session.
    resume_pos = None
    resume_tries = 0
    # When the current run of BUFFERING began, and the position at the time.
    buf_since = buf_pos = None
    # Lag-driven re-casts: when the last one fired, how many samples in a row
    # have been over the line, the day the nightly reset last ran, and which
    # stream the offset was last measured on (a new stream's first sample is
    # taken as-is rather than eased in from the old stream's much larger lag).
    recast_at = 0.0
    over = 0
    night_done = None
    measured_on = None
    while not stop.is_set():
        try:
            if cast is None:
                cast = pychromecast.get_chromecast_from_host(
                    (HUB_IP, 8009, "still-life", "Still Life", "Nest Hub"))
                cast.wait(timeout=20)
                log("connected to hub")

            mc = cast.media_controller
            mc.update_status()
            time.sleep(2)
            st = mc.status
            app = cast.status.app_id if cast.status else None
            playing_ours = (st.content_id == url
                            and st.player_state in ("PLAYING", "BUFFERING"))

            lag = measure_lag(st) if playing_ours else None
            if playing_ours and frame.smooth != STREAM["smooth"]:
                log("slideshow %s; re-casting to switch encoder"
                    % ("started" if frame.smooth else "ended"))
                playing_ours = False
            if lag is not None:
                if 0 <= lag <= MAX_OFFSET:
                    fresh = measured_on is not STREAM["at"]
                    measured_on = STREAM["at"]
                    with frame.lock:
                        # Ease into it: a single bad sample should not yank the
                        # clock, but a genuine change should land within a minute.
                        before = frame.offset
                        frame.offset = round(lag if fresh else
                                             0.5 * frame.offset + 0.5 * lag, 1)
                        settled = frame.offset
                    if abs(settled - before) > 0.5:
                        try:
                            OFFSET_FILE.write_text("%.1f" % settled)
                        except OSError as exc:
                            log("could not save clock offset: %s" % exc)
                # Two samples in a row, so one odd reading cannot blank the screen.
                over = over + 1 if lag > RECAST_LAG else 0
                today = datetime.now().date()
                if (over >= 2 and time.time() - recast_at > RECAST_COOLDOWN):
                    log("hub %.0fs behind live; re-casting to catch up" % lag)
                    playing_ours = False
                elif (datetime.now().hour == RECAST_HOUR and night_done != today
                        and lag > RECAST_NIGHT_MIN):
                    log("nightly catch-up: hub %.0fs behind live; re-casting" % lag)
                    playing_ours = False
                if not playing_ours:
                    recast_at, over = time.time(), 0
                if datetime.now().hour == RECAST_HOUR:
                    night_done = today

            cur = st.current_time
            # Seen 2026-09-26: the Hub sat in BUFFERING on our URL for well over
            # an hour, frozen on one frame. That counted as playing_ours, so
            # nothing acted. A position that stops moving while buffering is a
            # stall; re-cast (the fallthrough below) rather than wait on it.
            if st.content_id == url and st.player_state == "BUFFERING":
                moved = (cur is not None and buf_pos is not None
                         and cur - buf_pos > 1.0)
                if buf_since is None or moved:
                    buf_since, buf_pos = time.time(), cur
                elif time.time() - buf_since >= STUCK_BUFFERING:
                    log("hub stuck buffering for %.0fs at %s; re-casting"
                        % (time.time() - buf_since,
                           "%.0fs" % cur if cur is not None else "?"))
                    playing_ours = False
                    buf_since = buf_pos = None
            else:
                buf_since = buf_pos = None

            if (resume_pos is not None and cur is not None
                    and cur - resume_pos > 1.0):
                # The last resume demonstrably took; a pause we are looking at
                # now is a fresh one, not evidence that resuming does not work.
                resume_tries = 0

            if playing_ours and not first:
                resume_pos, resume_tries = None, 0
            elif app not in IDLE_APPS and not playing_ours:
                resume_pos, resume_tries = None, 0
                log("hub busy with app %s, leaving it alone" % app)
            elif (not first and resume_tries < 2 and st.content_id == url
                    and st.player_state == "PAUSED"):
                # The Hub pauses itself every few hours -- a Wi-Fi hiccup, most
                # likely; nothing on this box moves when it happens. On a still
                # image a paused stream looks exactly like a playing one, so the
                # pause itself is invisible. What is very visible is the cure:
                # re-casting tears the receiver down (paused -> idle -> off ->
                # idle -> buffering), which is the 15 s of black and the cast
                # chime. The recorder shows untouched pauses healing by
                # themselves in 0.2-27 s, so resume first and stay silent.
                resume_pos = cur
                resume_tries += 1
                log("hub paused on our stream at %s; resuming (try %d)"
                    % ("%.0fs" % cur if cur is not None else "?", resume_tries))
                mc.play()
            else:
                resume_pos, resume_tries = None, 0
                log("casting %s (app=%s state=%s)" % (url, app, st.player_state))
                mc.play_media(url, "video/mp4", stream_type="LIVE", autoplay=True)
                try:
                    mc.block_until_active(timeout=20)
                except Exception:
                    pass
            first = False
        except Exception as exc:
            log("cast check failed: %s" % exc)
            if cast is not None:
                try:
                    cast.disconnect()
                except Exception:
                    pass
                cast = None          # dial again next time round
        stop.wait(CAST_CHECK_EVERY)


# ----------------------------------------------------------------------- main


def main():
    global HUB_IP
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host-ip", default=None,
                    help="this box's LAN address, as the Hub must reach it "
                         "(default: [hub] host_ip, else detected)")
    ap.add_argument("--hub-ip", default=settings.HUB_IP)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-cast", action="store_true",
                    help="serve the stream but do not touch the Hub")
    args = ap.parse_args()
    HUB_IP = args.hub_ip
    host_ip = args.host_ip or lan_ip()

    frame = Frame()
    try:
        saved = float(OFFSET_FILE.read_text().strip())
        if 0 <= saved <= MAX_OFFSET:
            frame.offset = saved
            log("resuming with saved clock offset %.1fs" % saved)
    except (OSError, ValueError):
        pass
    stop = threading.Event()

    log("priming first frame...")
    try:
        frame.refresh_slow()
    except Exception as exc:
        log("initial fetch failed: %s" % exc)
    frame.maybe_render()

    threading.Thread(target=render_loop, args=(frame, stop), daemon=True).start()

    StreamHandler.frame = frame
    StreamHandler.chimes = chime.Chimes(log)
    StreamHandler.chimes.start(stop)
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), StreamHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://%s:%d%s" % (host_ip, args.port, STREAM_PATH)
    log("serving %s" % url)

    if not args.no_cast:
        threading.Thread(target=cast_loop, args=(url, frame, stop),
                         daemon=True).start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
        log("bye")


if __name__ == "__main__":
    main()
