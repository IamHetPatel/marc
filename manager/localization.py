"""Rover pose in the Lighthouse frame = latest absolute fix + wheel odometry since (plan §7).

Sources, best first (every pose carries `source` and `age_s`):
  docked_drone          drone sits on the plate, its Lighthouse pose IS the rover's pose (minus plate offset)
  fix+odometry          drone is out: last absolute fix, carried forward by odometry
  start_cross+odometry  never had a fix: assume the rover started on the home cross
  overhead_camera       the overhead webcam sees the rover's top tag (fresh sighting); between frames odometry
                        carries the last sighting forward like any other fix
"""
import math
import threading
from dataclasses import dataclass
from typing import Optional

from .adapters.base import RobotError
from .models import Arena, Pose

DRONE_POLL_S = 0.3


def _rot(x: float, y: float, deg: float):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return c * x - s * y, s * x + c * y


def _wrap(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


@dataclass
class Fix:
    lh: Pose          # rover pose in the Lighthouse frame at fix time
    odom: dict        # wheel odometry reading at the same moment
    t: float
    source: str


@dataclass
class LocalizedPose:
    x: float
    y: float
    yaw: float
    source: str
    age_s: float

    def as_dict(self) -> dict:
        return {"x": round(self.x, 3), "y": round(self.y, 3), "yaw": round(self.yaw, 1)}


class Localization:
    def __init__(self, rover, drone, arena: Arena, clock, use_drone_fix: bool = True, overhead=None):
        self.rover, self.drone, self.arena, self.clock = rover, drone, arena, clock
        self.use_drone_fix = use_drone_fix      # False when the drone is a mock and the rover is real
        self.overhead = overhead                # OverheadCamera or None
        self._sighting_t = None                 # capture time of the last overhead sighting used as a fix
        self.fix: Optional[Fix] = None
        self._drone_t = -1e9
        self._docked = False
        self._lock = threading.RLock()          # monitor thread and goto loop both call rover_pose

    def reanchor_home(self):
        """The operator put the rover on the start cross: the next pose starts again from arena.home."""
        with self._lock:
            self.fix = None

    def anchor(self, x: float, y: float, yaw: float, source: str = "crazyflie_wand"):
        """The rover is exactly here (Lighthouse frame), e.g. read from a Crazyflie sitting on its top plate.
        Wheel odometry carries on from this point, so rover and drone coordinates agree again."""
        with self._lock:
            self.fix = Fix(Pose(x=x, y=y, yaw=yaw), self.rover.pose(), self.clock.now(), source)

    def rover_pose(self) -> LocalizedPose:
        """Raises RobotError if the rover itself is unreachable."""
        with self._lock:
            return self._rover_pose()

    def _rover_pose(self) -> LocalizedPose:
        odom = self.rover.pose()
        now = self.clock.now()
        if self.use_drone_fix and now - self._drone_t >= DRONE_POLL_S:
            self._drone_t = now
            try:
                dp = self.drone.pose()
            except RobotError:
                dp = None
            self._docked = bool(dp and dp.get("state") == "docked")
            if self._docked:
                po = self.arena.plate_offset
                yaw = dp["yaw"] - (po.yaw or 0.0)
                ox, oy = _rot(po.x, po.y, yaw)
                self.fix = Fix(Pose(x=dp["x"] - ox, y=dp["y"] - oy, yaw=yaw), odom, now, "docked_drone")
        if self.overhead is not None and not self._docked:
            self._overhead_fix(odom, now)
        if self.fix is None:
            h = self.arena.home
            self.fix = Fix(Pose(x=h.x, y=h.y, yaw=h.yaw or 0.0), odom, now, "start_cross")
        f = self.fix
        # carry the fix forward: rotate the odometry delta into the Lighthouse frame
        dyaw = f.lh.yaw - f.odom["yaw"]
        dx, dy = _rot(odom["x"] - f.odom["x"], odom["y"] - f.odom["y"], dyaw)
        if self._docked:
            source = "docked_drone"
        elif f.source == "overhead_camera" and now - f.t <= self.arena.overhead.max_age_s:
            source = "overhead_camera"
        else:
            source = {"start_cross": "start_cross+odometry",
                      "crazyflie_wand": "crazyflie+odometry"}.get(f.source, "fix+odometry")
        return LocalizedPose(f.lh.x + dx, f.lh.y + dy, _wrap(odom["yaw"] + dyaw), source, now - f.t)

    def _overhead_fix(self, odom: dict, now: float):
        """Re-anchor on a NEW, fresh overhead sighting only: re-using an old one with a newer odometry reading would
        throw away the motion since. The sighting is up to one frame old; at 0.3 m/s that is a few cm."""
        sighting = self.overhead.rover_pose()
        if sighting is None or sighting["t"] == self._sighting_t or sighting["age_s"] > self.arena.overhead.max_age_s:
            return
        self._sighting_t = sighting["t"]
        self.fix = Fix(Pose(x=sighting["x"], y=sighting["y"], yaw=sighting["yaw"]), odom, now, "overhead_camera")
