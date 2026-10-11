"""Rover pose in the Lighthouse frame = latest absolute fix + wheel odometry since (plan §7).

Sources, best first (every pose carries `source` and `age_s`):
  lighthouse_rider      a Crazyflie rides on the rover (ROVER_RIDER): its Lighthouse pose minus plate_offset IS the
                        rover's pose, read on every call; odometry is used only to bridge a missed read
  docked_drone          drone sits on the plate, its Lighthouse pose IS the rover's pose (minus plate offset)
  fix+odometry          drone is out: last absolute fix, carried forward by odometry
  start_cross+odometry  never had a fix: assume the rover started on the home cross
  overhead_camera       the overhead webcam sees the rover's top tag (fresh sighting); between frames odometry
                        carries the last sighting forward like any other fix

With ROVER_RIDER set, RiderLocalization (below) is used INSTEAD: the riding Crazyflie is the only source, with no
odometry, start cross, anchor or camera mixed in, so the rover can never jump between two frames.
"""
import json
import math
import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .adapters.base import RobotError
from .models import Arena, Pose

DRONE_POLL_S = 0.3
RIDER_POLL_S = 0.1          # the drone server logs the rider at 10 Hz; the goto loop asks every 0.1 s


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
    ok: bool = True           # False: no trustworthy position right now (motion loops stop and wait)

    def as_dict(self) -> dict:
        return {"x": round(self.x, 3), "y": round(self.y, 3), "yaw": round(self.yaw, 1)}


class Localization:
    RIDER_FRESH_S = 1.0
    RIDER_DIRECT_S = 0.25   # a rider read this fresh is the rover's pose as is; older ones are bridged by odometry

    def __init__(self, rover, drone, arena: Arena, clock, use_drone_fix: bool = True, overhead=None,
                 rider: str = ""):
        self.rover, self.drone, self.arena, self.clock = rover, drone, arena, clock
        self.rider = rider                      # "target" | "scout" | "": a Crazyflie riding on the EP (ROVER_RIDER)
        self._rider_t = -1e9
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
        # With a rider the scout never sits on the rover: "docked" then means "on its own pad", not "on the plate"
        if self.use_drone_fix and not self.rider and now - self._drone_t >= DRONE_POLL_S:
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
        if self.rider and now - self._rider_t >= RIDER_POLL_S:
            self._rider_fix(odom, now)
        f = self.fix
        if self.rider and f is not None and f.source == "lighthouse_rider" and now - f.t <= self.RIDER_DIRECT_S:
            # a fresh rider read: the drone's position alone, no odometry added
            return LocalizedPose(f.lh.x, f.lh.y, _wrap(f.lh.yaw), "lighthouse_rider", now - f.t)
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
        elif f.source == "lighthouse_rider":
            source = "lighthouse_rider" if now - f.t <= self.RIDER_FRESH_S else "rider+odometry"
        elif f.source == "overhead_camera" and now - f.t <= self.arena.overhead.max_age_s:
            source = "overhead_camera"
        else:
            source = {"start_cross": "start_cross+odometry",
                      "crazyflie_wand": "crazyflie+odometry"}.get(f.source, "fix+odometry")
        return LocalizedPose(f.lh.x + dx, f.lh.y + dy, _wrap(odom["yaw"] + dyaw), source, now - f.t)

    def _rider_fix(self, odom: dict, now: float):
        """A Crazyflie on the EP's top plate (motors off) knows its Lighthouse pose; minus the plate offset that is
        the rover's pose, in the very frame the scout flies in. Wheel odometry only bridges the gaps between reads."""
        self._rider_t = now
        try:
            dp = self.drone.pose(cf=self.rider)
        except RobotError:
            return
        if dp.get("ok", True) is False or dp.get("state") == "flying" or not isinstance(dp.get("x"), (int, float)):
            return
        po = self.arena.plate_offset
        yaw = float(dp.get("yaw") or 0.0) - (po.yaw or 0.0)
        ox, oy = _rot(po.x, po.y, yaw)
        self.fix = Fix(Pose(x=dp["x"] - ox, y=dp["y"] - oy, yaw=yaw), odom, now, "lighthouse_rider")

    def _overhead_fix(self, odom: dict, now: float):
        """Re-anchor on a NEW, fresh overhead sighting only: re-using an old one with a newer odometry reading would
        throw away the motion since. The sighting is up to one frame old; at 0.3 m/s that is a few cm."""
        sighting = self.overhead.rover_pose()
        if sighting is None or sighting["t"] == self._sighting_t or sighting["age_s"] > self.arena.overhead.max_age_s:
            return
        self._sighting_t = sighting["t"]
        self.fix = Fix(Pose(x=sighting["x"], y=sighting["y"], yaw=sighting["yaw"]), odom, now, "overhead_camera")


# ---------------------------------------------------------------- a Crazyflie riding on the rover
MOUNT_FILE = Path(__file__).resolve().parent.parent / "runs" / "rider_mount.json"


def load_mount(arena: Arena, path: Path = MOUNT_FILE) -> Pose:
    """The calibrated mount (rover_calibrate_rider) for this field if there is one, else the arena's."""
    try:
        raw = json.loads(path.read_text())
        if raw.get("arena") == arena.kind:
            return Pose(x=raw["x"], y=raw["y"], yaw=raw["yaw"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return arena.mount()


def save_mount(arena: Arena, mount: Pose, path: Path = MOUNT_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"arena": arena.kind, "x": round(mount.x, 3), "y": round(mount.y, 3),
                                "yaw": round(mount.yaw or 0.0, 1), "t": time.time()}, indent=2))


class RiderLocalization:
    """ROVER_RIDER: a Crazyflie sits on the rover (motors off). Its Lighthouse pose, through the mount, IS the rover's
    pose, in the very frame the scout flies in. It is the ONLY source: nothing else can override it.

    Every read is checked before it is used, because a Lighthouse dropout reports nonsense (e.g. y = -4 m):
      * outside the map (plus 1 m) or a silly height       -> rejected
      * further from the last good pose than the rover can drive in the time since (MAX_SPEED + slack)
                                                            -> rejected, unless CONFIRM reads in a row agree on the
                                                               new place (someone carried the rover)
    A good pose is held for HOLD_S; after that the pose says ok=False and motion loops stop until it is back."""

    POLL_S = 0.1            # the drone server logs the rider at 10 Hz
    HOLD_S = 1.0
    MAX_SPEED = 0.8         # m/s, faster than the EP ever drives here
    JUMP_SLACK_M = 0.15
    CONFIRM = 3
    absolute = True         # positions are absolute: nothing to anchor or reset

    def __init__(self, rover, drone, arena: Arena, clock, rider: str = "target",
                 mount_file: Optional[Path] = MOUNT_FILE):
        self.rover, self.drone, self.arena, self.clock = rover, drone, arena, clock
        self.rider = rider
        self.mount_file = mount_file            # where rover_calibrate_rider keeps its result (None: not kept)
        self.mount = load_mount(arena, mount_file) if mount_file else arena.mount()
        self.fix: Optional[Fix] = None          # last accepted pose (the rover's), Fix.odom unused
        self._t = -1e9
        self._suspect: list = []
        self.rejected = 0
        self.last_reject = None
        self.last_cf: Optional[dict] = None     # last accepted raw Crazyflie pose (for calibration and the map)
        self._lock = threading.RLock()

    # the legacy re-anchoring has no meaning here; kept so callers need no special case
    def reanchor_home(self):
        pass

    def anchor(self, x: float, y: float, yaw: float, source: str = "crazyflie_wand"):
        pass

    def set_mount(self, mount: Pose):
        with self._lock:
            self.mount = mount
            self.fix, self._suspect = None, []

    def to_rover(self, cf: dict) -> Pose:
        """Crazyflie pose -> rover centre pose."""
        yaw = _wrap(float(cf.get("yaw") or 0.0) - (self.mount.yaw or 0.0))
        ox, oy = _rot(self.mount.x, self.mount.y, yaw)
        return Pose(x=cf["x"] - ox, y=cf["y"] - oy, yaw=yaw)

    def read_cf(self) -> Optional[dict]:
        """One raw read of the riding Crazyflie, or None if it has no usable pose."""
        try:
            dp = self.drone.pose(cf=self.rider)
        except RobotError:
            return None
        if dp.get("ok", True) is False or dp.get("state") == "flying":
            return None
        if not all(isinstance(dp.get(k), (int, float)) for k in ("x", "y")):
            return None
        return dp

    def _plausible(self, p: Pose, cf: dict, now: float) -> bool:
        if not self.arena.view_rect().contains(p.x, p.y, margin=1.0) or not -0.3 <= float(cf.get("z") or 0.0) <= 1.0:
            self._reject(f"({p.x:.2f}, {p.y:.2f}, z {cf.get('z')}) is off the map: tracking glitch")
            return False
        f = self.fix
        if f is None:
            return True
        jump = math.hypot(p.x - f.lh.x, p.y - f.lh.y)
        if jump <= self.MAX_SPEED * (now - f.t) + self.JUMP_SLACK_M:
            self._suspect = []
            return True
        self._suspect.append(p)
        last = self._suspect[-self.CONFIRM:]
        if len(last) == self.CONFIRM and all(math.hypot(q.x - p.x, q.y - p.y) < 0.1 for q in last):
            self._suspect = []
            return True                          # it really is somewhere else now (carried by hand)
        self._reject(f"jumped {jump:.2f} m in {now - f.t:.1f} s: tracking glitch")
        return False

    def _reject(self, why: str):
        self.rejected += 1
        self.last_reject = why

    def _poll(self, now: float):
        self._t = now
        cf = self.read_cf()
        if cf is None:
            return
        p = self.to_rover(cf)
        if self._plausible(p, cf, now):
            self.fix = Fix(p, {}, now, "lighthouse_rider")
            self.last_cf = cf

    def rover_pose(self) -> LocalizedPose:
        """Never raises for a missing rider: the pose then says ok=False (the watchdog watches the rover itself)."""
        with self._lock:
            now = self.clock.now()
            if now - self._t >= self.POLL_S - 1e-3:
                self._poll(now)
            f = self.fix
            if f is None:
                h = self.arena.home
                return LocalizedPose(h.x, h.y, h.yaw or 0.0, "rider_waiting", 1e9, ok=False)
            age = now - f.t
            ok = age <= self.HOLD_S
            return LocalizedPose(f.lh.x, f.lh.y, _wrap(f.lh.yaw or 0.0), "lighthouse_rider" if ok else "rider_lost",
                                 age, ok=ok)

    def averaged_cf(self, n: int = 5, gap_s: float = 0.12) -> Optional[dict]:
        """Median of n raw rider reads, for calibration (the rover stands still meanwhile)."""
        reads = []
        for _ in range(n * 2):
            cf = self.read_cf()
            if cf is not None:
                reads.append(cf)
                if len(reads) == n:
                    break
            self.clock.sleep(gap_s)
        if len(reads) < max(2, n // 2):
            return None
        yaws = [math.radians(float(r.get("yaw") or 0.0)) for r in reads]
        return {"x": statistics.median(r["x"] for r in reads), "y": statistics.median(r["y"] for r in reads),
                "yaw": math.degrees(math.atan2(sum(map(math.sin, yaws)), sum(map(math.cos, yaws))))}

    def status(self) -> dict:
        m = self.mount
        return {"mount": {"x": round(m.x, 3), "y": round(m.y, 3), "yaw": round(m.yaw or 0.0, 1)},
                "rejected": self.rejected, "last_reject": self.last_reject,
                "cf": {k: self.last_cf.get(k) for k in ("x", "y", "yaw")} if self.last_cf else None}
