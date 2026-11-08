"""Home Assistant access for Still Life.

Two transports, because HA splits the data we need across them:

  * REST  - service calls (weather.get_forecasts needs return_response)
  * WS    - long-term statistics, which have no REST endpoint at all

The websocket client is hand-rolled against RFC 6455 rather than using the
`websockets` package: this box is PEP 668 managed and pip refuses to install
into the system interpreter, and a cron job that depends on an activated venv
is one more thing to break at 4am. We only ever send small text frames.
"""

import base64
import json
import os
import socket
import struct
import urllib.request
from datetime import datetime, timedelta, timezone

import settings

HA_HOST = settings.HA_HOST
HA_PORT = settings.HA_PORT
TOKEN_FILE = settings.HA_TOKEN_FILE   # a long-lived access token, read-only use


def _token():
    with open(TOKEN_FILE) as fh:
        return fh.read().strip()


# --------------------------------------------------------------------------- REST


def _rest(path, payload=None, method="GET"):
    url = f"http://{HA_HOST}:{HA_PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def state(entity_id):
    """Current state object for one entity."""
    return _rest(f"/api/states/{entity_id}")


def daily_forecast(entity_id="weather.forecast_home", days=4):
    """Daily forecast list.

    The `forecast` attribute was removed from weather entities in 2024.4; the
    only way to get it now is a service call with return_response=true.
    """
    resp = _rest(
        "/api/services/weather/get_forecasts?return_response=true",
        {"entity_id": entity_id, "type": "daily"},
        method="POST",
    )
    forecast = resp["service_response"][entity_id]["forecast"]
    return forecast[:days]


# ----------------------------------------------------------------------- WEBSOCKET


class _WS:
    """Just enough websocket to talk to HA: text frames, client-masked."""

    def __init__(self, host, port, path="/api/websocket"):
        self.sock = socket.create_connection((host, port), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("connection closed during handshake")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n")[0]:
            raise RuntimeError(f"websocket handshake failed: {buf[:200]!r}")
        self.buf = buf.split(b"\r\n\r\n", 1)[1]

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj):
        payload = json.dumps(obj).encode()
        n = len(payload)
        header = bytearray([0x81])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(
            bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        )

    def recv(self):
        while True:
            b0, b1 = self._read(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            data = self._read(n)
            if opcode == 0x8:
                raise RuntimeError("server closed connection")
            if opcode in (0x1, 0x2):
                return json.loads(data)
            # ping/pong/continuation: ignore

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def statistics(entity_id, days=90, period="day"):
    """Long-term statistics for one entity.

    Returns [(date, mean, min, max), ...] oldest first. These survive the
    recorder purge, so this is the only way to plot beyond purge_keep_days --
    /api/history/period cannot reach back this far.
    """
    ws = _WS(HA_HOST, HA_PORT)
    try:
        if ws.recv().get("type") != "auth_required":
            raise RuntimeError("unexpected greeting from HA websocket")
        ws.send({"type": "auth", "access_token": _token()})
        auth = ws.recv()
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"HA websocket auth failed: {auth}")

        start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        ws.send(
            {
                "id": 1,
                "type": "recorder/statistics_during_period",
                "start_time": start,
                "statistic_ids": [entity_id],
                "period": period,
                "types": ["mean", "min", "max"],
            }
        )
        while True:
            msg = ws.recv()
            if msg.get("id") == 1:
                break
        if not msg.get("success"):
            raise RuntimeError(f"statistics request failed: {msg}")

        out = []
        for row in msg.get("result", {}).get(entity_id, []):
            when = datetime.fromtimestamp(row["start"] / 1000, timezone.utc)
            if row.get("mean") is None:
                continue
            out.append((when, row["mean"], row.get("min"), row.get("max")))
        return out
    finally:
        ws.close()
