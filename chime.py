"""Bells on the stream while a kitchen timer runs.

The Hub plays our stream, so anything cast to it for a sound would replace the
picture. Instead the stream carries an audio track of its own: silence, with a
bell mixed in every 30 s while a Home Assistant timer entity (a coffee scale's
"timer running") is on. Each bell is the next note up a C major arpeggio from a
low C, so the pitch says roughly how long it has been, and it wraps back down
before it gets shrill.

Timing: the Hub plays the stream `offset` seconds behind live (measured by the
cast thread). A bell due on the wall at time d is therefore written into the
second of audio pumped at d - offset, and rings in the kitchen on the mark.
A bell whose moment has already passed on screen is skipped rather than played
late, so a long lag drops the first bell instead of smearing the schedule.

The audio is produced one second at a time, alongside each video frame, by the
same pump -- so audio and picture share one clock by construction.
"""

import math
import struct
import threading
import time
from array import array
from datetime import datetime

import ha
import settings

RATE = 44100                         # ffmpeg's s16le default; still_life relies on it
SECOND_BYTES = RATE * 2              # s16le mono
SILENCE = bytes(SECOND_BYTES)

ENTITY = settings.get("chime", "timer_entity", "")
EVERY = float(settings.get("chime", "every", 30))
MAX_RUN = 30 * 60                    # a timer left running stops ringing after this
POLL = 1.0
LATE_OK = 2.0                        # seconds a bell may be late before it is skipped

# C3 E3 G3 C4 E4 G4 C5, then back to C3.
NOTES = [130.81, 164.81, 196.00, 261.63, 329.63, 392.00, 523.25]
BELL_SECONDS = 6.0
PEAK = 0.25                          # -12 dBFS; the Hub's volume does the rest

# The voice: glassy but soft -- "lo-fi" in the warm, muffled sense (a bell
# heard through a cassette in another room), not the gritty one: no bit
# crushing, no saturation. Each partial is a pair of modes a hair apart (a real
# bell or glass is never perfectly round, so its modes split and beat -- that
# slow shimmer is most of what reads as "real"). The upper series is a free
# bar's (2.756, 5.404: glass/glockenspiel), kept quiet, with an octave for
# warmth. (ratio, amplitude, decay seconds at middle C.)
PARTIALS = [
    (1.000, 1.00, 3.6),
    (2.000, 0.30, 2.4),
    (2.756, 0.22, 1.4),
    (5.404, 0.05, 0.5),
]
SPLIT = 0.0011                       # mode pair spacing (fraction): ~0.3-1 Hz beats
STRIKE = 0.05                        # a felt mallet, barely there
STRIKE_HZ = 1200                     # ...and dull
ROOM = 0.34                          # reverb wet mix
LOWPASS_HZ = 2600                    # warm: take the edge off the top
WOW_HZ, WOW_S = 0.45, 0.0006         # slow tape pitch drift (~3 cents)
HISS = 0.0015                        # the faintest tape hiss, only under the bell
SYNTH_VERSION = 3                    # bump to invalidate the on-disk cache


def _onepole(buf, hz):
    a = math.exp(-2 * math.pi * hz / RATE)
    y = 0.0
    for i, x in enumerate(buf):
        y = (1 - a) * x + a * y
        buf[i] = y


def _room(buf):
    """Small Schroeder reverb: four combs into two allpasses."""
    n = len(buf)
    wet = [0.0] * n
    for ms, g in ((29.7, 0.80), (37.1, 0.78), (41.1, 0.76), (43.7, 0.74)):
        d = int(ms * RATE / 1000)
        line = [0.0] * d
        j = 0
        for i in range(n):
            y = line[j]
            line[j] = buf[i] + g * y
            wet[i] += y
            j = j + 1 if j + 1 < d else 0
    for ms, g in ((5.0, 0.7), (1.7, 0.7)):
        d = int(ms * RATE / 1000)
        line = [0.0] * d
        j = 0
        for i in range(n):
            v = line[j]
            u = wet[i] + g * v
            wet[i] = v - g * u
            line[j] = u
            j = j + 1 if j + 1 < d else 0
    for i in range(n):
        buf[i] = (1 - ROOM) * buf[i] + ROOM * 0.25 * wet[i]


def _bell(freq, seed=0):
    import random
    rnd = random.Random(int(freq * 100) + seed)
    n = int(BELL_SECONDS * RATE)
    buf = [0.0] * n
    env = [0.0] * n                  # overall loudness, for gating the hiss
    for ratio, amp, tau in PARTIALS:
        f0 = freq * ratio
        if f0 > RATE / 2.5:
            continue
        tau *= (261.63 / freq) ** 0.35   # low notes ring longer, like real ones
        for f, a in ((f0 * (1 - SPLIT / 2), amp * 0.55),
                     (f0 * (1 + SPLIT / 2), amp * 0.45)):
            # Damped resonator as a two-term recurrence: one multiply-add per
            # sample instead of a sin() and an exp().
            w = 2 * math.pi * f / RATE
            r = math.exp(-1.0 / (tau * RATE))
            c, r2 = 2 * r * math.cos(w), r * r
            ph = rnd.uniform(0, 2 * math.pi)
            y1 = a * math.sin(ph)                   # y[0] of a r^n sin(wn + ph)
            y2 = a * math.sin(ph - w) / r           # y[-1]
            for i in range(n):
                buf[i] += y1
                y1, y2 = c * y1 - r2 * y2, y1
    for i in range(n):
        env[i] = math.exp(-i / (2.5 * RATE))
    # Mallet: a few ms of noise, dulled, on the attack.
    click = int(0.004 * RATE)
    noise = [rnd.uniform(-1, 1) * STRIKE * (1 - k / click) for k in range(click)]
    _onepole(noise, STRIKE_HZ)
    _onepole(noise, STRIKE_HZ)
    for k in range(click):
        buf[k] += noise[k]
    att = int(0.006 * RATE)          # a soft onset, no click
    for i in range(att):
        buf[i] *= i / att
    _room(buf)
    # Tape wow: read the buffer back on a slowly wobbling clock.
    warped = [0.0] * n
    for i in range(n):
        t = i + WOW_S * RATE * math.sin(2 * math.pi * WOW_HZ * i / RATE)
        j = int(t)
        if 0 <= j < n - 1:
            fr = t - j
            warped[i] = buf[j] * (1 - fr) + buf[j + 1] * fr
    buf = warped
    _onepole(buf, LOWPASS_HZ)
    _onepole(buf, LOWPASS_HZ)
    peak = max(abs(x) for x in buf) or 1.0
    tail = int(0.4 * RATE)
    out = array("h", bytes(2 * n))
    for i in range(n):
        x = buf[i] / peak
        x += HISS * env[i] * rnd.uniform(-1, 1)
        if i >= n - tail:
            x *= (n - 1 - i) / tail
        out[i] = int(max(-1.0, min(1.0, x)) * PEAK * 32767)
    return out.tobytes()


def _all_bells():
    """Every note, cached on disk: pure-Python synthesis takes a while."""
    import hashlib
    key = hashlib.sha1(repr((SYNTH_VERSION, RATE, NOTES, BELL_SECONDS, PEAK,
                             PARTIALS, SPLIT, STRIKE, STRIKE_HZ, ROOM,
                             LOWPASS_HZ, WOW_HZ, WOW_S, HISS)).encode()).hexdigest()[:12]
    path = settings.OUT / (".bells-%s.pcm" % key)
    size = int(BELL_SECONDS * RATE) * 2
    try:
        data = path.read_bytes()
        if len(data) == size * len(NOTES):
            return [data[i * size:(i + 1) * size] for i in range(len(NOTES))]
    except OSError:
        pass
    bells = [_bell(f) for f in NOTES]
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(b"".join(bells))
        tmp.replace(path)
    except OSError:
        pass
    return bells


class Chimes:
    """Watches the timer entity; hands the pump one second of audio per tick."""

    def __init__(self, log):
        self.log = log
        self.lock = threading.Lock()
        self.bells = None            # synthesized lazily, off the pump's thread
        self.started = None          # unix time the timer started, or None
        self.next_k = 1              # the next bell's index (1 = first, at +EVERY)
        self.ringing = None          # [pcm bytes, byte position] of the bell in progress
        self.enabled = bool(ENTITY)

    def start(self, stop):
        if not self.enabled:
            return
        threading.Thread(target=self._synth, daemon=True).start()
        threading.Thread(target=self._watch, args=(stop,), daemon=True).start()

    def _synth(self):
        t = time.time()
        self.bells = _all_bells()
        self.log("chime: %d bells ready in %.1fs, watching %s"
                 % (len(NOTES), time.time() - t, ENTITY))

    def _watch(self, stop):
        failing = False
        while not stop.is_set():
            try:
                st = ha.state(ENTITY)
                on = st.get("state") == "on"
                since = datetime.fromisoformat(st["last_changed"]).timestamp()
                failing = False
            except Exception as exc:      # HA restarting, network blip
                if not failing:
                    self.log("chime: cannot read %s: %s" % (ENTITY, exc))
                failing = True
                on, since = False, None
            with self.lock:
                if on and self.started != since:
                    self.started, self.next_k = since, 1
                    self.log("chime: timer started")
                elif not on and self.started is not None:
                    self.started = None
                    self.log("chime: timer stopped")
            stop.wait(POLL)

    def second(self, sent_at, offset):
        """Audio for the second of stream pumped at `sent_at`, which the Hub
        will play `offset` seconds later."""
        out = None
        with self.lock:
            start = sent_at + offset         # wall time this second is heard
            if (self.started is not None and self.bells
                    and start - self.started < MAX_RUN):
                due = self.started + self.next_k * EVERY
                while due < start - LATE_OK:            # already gone by on screen
                    self.next_k += 1
                    due = self.started + self.next_k * EVERY
                if due < start + 1.0:
                    note = self.bells[(self.next_k - 1) % len(self.bells)]
                    at = max(0, int((due - start) * RATE)) * 2
                    out = bytearray(SILENCE)
                    if self.ringing:                    # never overlaps at 30 s; be safe
                        self._spill(out, 0, at)
                    self.ringing = [note, 0]
                    self._spill(out, at, SECOND_BYTES)
                    self.next_k += 1
                    return bytes(out)
            if self.ringing:
                out = bytearray(SILENCE)
                self._spill(out, 0, SECOND_BYTES)
                return bytes(out)
        return SILENCE

    def _spill(self, out, a, b):
        pcm, pos = self.ringing
        take = min(b - a, len(pcm) - pos)
        out[a:a + take] = pcm[pos:pos + take]
        pos += take
        self.ringing = None if pos >= len(pcm) else [pcm, pos]


def write_wav(path):
    """All seven bells, 2 s apart, for listening off the Hub."""
    gap = 2 * RATE * 2
    bells = _all_bells()
    total = bytearray(gap * (len(bells) - 1) + len(bells[-1]))
    for i, b in enumerate(bells):
        seg = array("h", total[i * gap:i * gap + len(b)])
        add = array("h", b)
        for j in range(len(add)):
            seg[j] = max(-32768, min(32767, seg[j] + add[j]))
        total[i * gap:i * gap + len(b)] = seg.tobytes()
    with open(path, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", 36 + len(total)) + b"WAVEfmt "
                 + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16)
                 + b"data" + struct.pack("<I", len(total)) + total)


if __name__ == "__main__":
    import sys
    write_wav(sys.argv[1] if len(sys.argv) > 1 else "bells.wav")
