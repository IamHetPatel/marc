"""HTTP client for rover/robot_server.py (:8000): /speed /drive /grip /arm /snapshot /stop /pose.

The server takes +y = left everywhere. The y signs stay configurable for a server that passes raw SDK
signs (+y = right) through.
"""
import time

import requests

from .base import RobotError

# Used only when ROVER_ARM_MODE=relative: absolute arm targets (x_mm, y_mm); we send the delta to /arm.
ARM_TARGETS = {"home": (0, 0), "carry": (60, 80), "pick": (140, -40)}


class HttpClient:
    def __init__(self, base: str, timeout: float = 5.0):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.last_rtt_ms = None

    def get(self, path: str, timeout: float = None, **params) -> requests.Response:
        t0 = time.monotonic()
        try:
            r = self.session.get(self.base + path, params=params, timeout=timeout or self.timeout)
        except requests.RequestException as e:
            raise RobotError(f"{self.base}{path}: {e}") from e
        self.last_rtt_ms = round((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            raise RobotError(f"{self.base}{path}: HTTP {r.status_code}")
        return r

    def json(self, path: str, timeout: float = None, **params) -> dict:
        r = self.get(path, timeout, **params)
        try:
            out = r.json()
        except ValueError:
            return {"ok": True, "raw": r.text[:200]}
        return out if isinstance(out, dict) else {"ok": True, "value": out}


class RoverHttp:
    def __init__(self, base: str, cfg):
        self.http = HttpClient(base)
        self.cfg = cfg
        self._arm_at = ARM_TARGETS["home"]

    @property
    def rtt_ms(self):
        return self.http.last_rtt_ms

    def pose(self) -> dict:
        return self.http.json("/pose", timeout=2.0)

    def speed(self, x, y, z, t) -> dict:
        return self.http.json("/speed", timeout=2.0, x=x, y=self.cfg.rover_speed_y_sign * y, z=z, t=t)

    def drive(self, x, y, z) -> dict:
        return self.http.json("/drive", timeout=30.0, x=x, y=self.cfg.rover_drive_y_sign * y, z=z)

    def snapshot(self) -> bytes:
        return self.http.get("/snapshot", timeout=10.0).content

    def grip(self, action) -> dict:
        return self.http.json("/grip", timeout=10.0, action=action)

    def arm(self, preset) -> dict:
        if self.cfg.rover_arm_mode == "preset":
            return self.http.json("/arm", timeout=10.0, preset=preset)
        tx, ty = ARM_TARGETS[preset]
        out = self.http.json("/arm", timeout=10.0, x=tx - self._arm_at[0], y=ty - self._arm_at[1])
        self._arm_at = (tx, ty)
        return out

    def stop(self) -> dict:
        return self.http.json("/stop", timeout=1.0)
