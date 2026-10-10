"""Mock world: a rover and a drone that behave like the real ones closely enough to run the whole mission.

This is also our Level-3 backup demo (docs/ai-manager-plan.md §12). What it models on purpose:
  * rover wheel odometry that drifts (scale error, strafing drifts more) in its own frame
  * a scout drone. With DOCK_MODE=0 (default) it starts landed on a launch pad beside the rover; with DOCK_MODE=1
    it rides the rover's plate and can only dock if the rover has not moved since takeoff. Scans with +-10 cm
    noise and false positives, drains battery
  * a "target" drone: the first own/hostile drone in the world, readable through pose(cf="target") the way the
    second Crazyflie's Lighthouse pose will be
  * a pinhole-ish front camera: objects project to (column, row) so the grip-line visual servoing is real
  * failure injection (MOCK_FAIL=goto:0.2,grip:0.3)
Simulated time runs `mock_time_scale` times faster than the wall clock.
"""
import io
import math
import random
import threading
import time

from PIL import Image, ImageDraw

from ..models import Arena

FLY_Z = 0.6
GRIP_DIST_M = 0.25          # an object this far ahead sits exactly on the grip line
HORIZON = 0.35              # horizon as a fraction of image height
OBJ_WIDTH_M = 0.2


def _rot(x: float, y: float, deg: float):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return c * x - s * y, s * x + c * y


def _wrap(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


class FaultInjector:
    """hit('goto') is True with the probability configured in MOCK_FAIL."""

    def __init__(self, spec: dict, seed: int = 8):
        self.spec, self.rng = spec, random.Random(seed)

    def hit(self, key: str) -> bool:
        return self.rng.random() < self.spec.get(key, 0.0)


class MockWorld:
    def __init__(self, arena: Arena, cfg):
        self.arena, self.cfg = arena, cfg
        self.scale = max(cfg.mock_time_scale, 1e-6)
        self.rng = random.Random(cfg.mock_seed)
        self.lock = threading.RLock()
        self.t = 0.0
        self.rover = [arena.home.x, arena.home.y, arena.home.yaw or 0.0]   # ground truth x, y, yaw
        self.odom = [0.0, 0.0, 0.0]                                         # what the wheels report
        self.objects = [o.model_dump() for o in arena.mock_objects]
        self.held = None
        self.gripper = "closed"
        self.arm_pose = "home"
        if cfg.dock_mode:
            self.drone = {"state": "docked", "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0, "battery_v": 4.1}
        else:                           # launch pad 0.3 m ahead of the rover's start, on the floor
            self.drone = {"state": "landed", "x": arena.home.x + 0.3, "y": arena.home.y, "z": 0.0, "yaw": 0.0,
                          "battery_v": 4.1}
        self.takeoff_rover = None        # rover pose when the drone left the plate
        self.abort = threading.Event()   # set by drone.land() to interrupt a running scan

    # ------------------------------------------------------------ time
    def advance(self, dt: float):
        with self.lock:
            self.t += dt
            if self.drone["state"] == "flying":
                self.drone["battery_v"] -= 0.15 * dt / 60.0
        time.sleep(max(0.0, dt) / self.scale)

    # ------------------------------------------------------------ rover motion
    def move_body(self, fwd: float, left: float, turn: float):
        b = self.arena.bounds
        with self.lock:
            wx, wy = _rot(fwd, left, self.rover[2])
            self.rover = [min(max(self.rover[0] + wx, b.x0), b.x1), min(max(self.rover[1] + wy, b.y0), b.y1),
                          _wrap(self.rover[2] + turn)]
            ef, el, et = (1 + self.rng.gauss(0.02, 0.02), 1 + self.rng.gauss(0.05, 0.04),
                          1 + self.rng.gauss(0.01, 0.02))
            ox, oy = _rot(fwd * ef, left * el, self.odom[2])
            self.odom = [self.odom[0] + ox, self.odom[1] + oy, _wrap(self.odom[2] + turn * et)]
            self._carry()

    def _carry(self):
        if self.held is not None:
            dx, dy = _rot(GRIP_DIST_M, 0.0, self.rover[2])
            self.held["x"], self.held["y"] = self.rover[0] + dx, self.rover[1] + dy

    def body_coords(self, obj: dict):
        """Object position in the rover frame: (forward, left)."""
        dx, dy = obj["x"] - self.rover[0], obj["y"] - self.rover[1]
        return _rot(dx, dy, -self.rover[2])

    # ------------------------------------------------------------ camera
    def project(self, obj: dict):
        """Pixel column/row of an object's base on the floor, or None if it is not in the camera's view."""
        cam = self.arena.camera
        f, l = self.body_coords(obj)
        if f < 0.12:
            return None
        bearing = math.atan2(l, f)
        half = math.radians(cam.fov_deg / 2)
        if abs(bearing) > half:
            return None
        focal = (cam.width / 2) / math.tan(half)
        horizon = cam.height * HORIZON
        k = (cam.grip_line_row - horizon) * GRIP_DIST_M
        return {"col": cam.width / 2 - math.tan(bearing) / math.tan(half) * (cam.width / 2),
                "row": horizon + k / f, "w": focal * OBJ_WIDTH_M / f, "h": focal * obj["height"] / f,
                "f": f, "l": l}

    def visible(self):
        with self.lock:
            out = []
            for o in self.objects:
                if o is self.held:
                    continue
                p = self.project(o)
                if p:
                    out.append((o, p))
            return sorted(out, key=lambda op: -op[1]["f"])      # far first so near objects paint on top

    def render(self) -> bytes:
        cam = self.arena.camera
        img = Image.new("RGB", (cam.width, cam.height), (150, 150, 155))
        d = ImageDraw.Draw(img)
        hz = int(cam.height * HORIZON)
        d.rectangle([0, 0, cam.width, hz], fill=(205, 212, 222))
        d.line([0, cam.grip_line_row, cam.width, cam.grip_line_row], fill=(90, 90, 100), width=1)
        for o, p in self.visible():
            d.rectangle([p["col"] - p["w"] / 2, p["row"] - p["h"], p["col"] + p["w"] / 2, p["row"]],
                        fill=o["color"], outline=(30, 30, 30))
        if self.held is not None:
            d.rectangle([cam.width * 0.4, cam.height * 0.72, cam.width * 0.6, cam.height], fill=self.held["color"])
        d.text((6, 6), f"MOCK CAMERA  t={self.t:.0f}s", fill=(40, 40, 40))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return buf.getvalue()

    # ------------------------------------------------------------ drone pose
    def drone_pose(self) -> dict:
        with self.lock:
            d = dict(self.drone)
            if d["state"] == "docked":
                ox, oy = _rot(self.arena.plate_offset.x, self.arena.plate_offset.y, self.rover[2])
                d.update(x=self.rover[0] + ox, y=self.rover[1] + oy, z=self.arena.dock_z,
                         yaw=_wrap(self.rover[2] + (self.arena.plate_offset.yaw or 0.0)))
            return {"ok": True, **{k: round(v, 3) if isinstance(v, float) else v for k, v in d.items()}}

    def target_pose(self) -> dict:
        """Ground truth of the 'crashed drone' (the real target Crazyflie reports this via Lighthouse)."""
        with self.lock:
            for o in self.objects:
                if o.get("kind") in ("hostile_drone", "own_drone"):
                    return {"ok": True, "x": round(o["x"], 3), "y": round(o["y"], 3), "z": 0.0, "state": "landed"}
        return {"ok": False, "error": "no target drone"}


class SimClock:
    def __init__(self, world: MockWorld):
        self.world = world

    def now(self) -> float:
        return self.world.t

    def sleep(self, seconds: float):
        self.world.advance(seconds)


class MockRover:
    def __init__(self, world: MockWorld):
        self.w = world
        self.rtt_ms = 12

    def pose(self) -> dict:
        x, y, yaw = self.w.odom
        return {"x": round(x, 3), "y": round(y, 3), "yaw": round(yaw, 2)}

    def speed(self, x, y, z, t) -> dict:
        self.w.move_body(x * t, y * t, z * t)
        self.w.advance(t)
        return {"ok": True}

    def drive(self, x, y, z) -> dict:
        self.w.move_body(x, y, z)
        self.w.advance(max(abs(x), abs(y)) / 0.3 + abs(z) / 90.0)
        return {"ok": True}

    def snapshot(self) -> bytes:
        return self.w.render()

    def grip(self, action) -> dict:
        w = self.w
        with w.lock:
            if action == "open":
                if w.held is not None:
                    w.held = None                       # drops where it is (in front of the rover)
                w.gripper = "open"
            elif action == "close":
                w.gripper = "closed"
                for o in w.objects:
                    f, l = w.body_coords(o)
                    if o.get("grippable") and 0.04 <= f <= 0.40 and abs(l) <= 0.15:
                        w.held = o
                        break
            else:
                return {"ok": False, "error": f"bad action {action!r}"}
        w.advance(1.0)
        return {"ok": True, "gripper": w.gripper}

    def arm(self, preset) -> dict:
        if preset not in ("pick", "carry", "home"):
            return {"ok": False, "error": f"bad preset {preset!r}"}
        self.w.arm_pose = preset
        self.w.advance(1.5)
        return {"ok": True, "arm": preset}

    def stop(self) -> dict:
        return {"ok": True}


class MockDrone:
    def __init__(self, world: MockWorld):
        self.w = world
        self.rtt_ms = 15

    def pose(self, cf: str = "scout") -> dict:
        return self.w.target_pose() if cf == "target" else self.w.drone_pose()

    def takeoff(self, z: float = FLY_Z) -> dict:
        w = self.w
        with w.lock:
            if w.drone["state"] == "flying":
                return {"ok": True, "pose": w.drone_pose()}
            pose = w.drone_pose()
            w.takeoff_rover = list(w.rover) if w.drone["state"] == "docked" else None
            w.drone.update(state="flying", x=pose["x"], y=pose["y"], z=z)
        w.abort.clear()
        w.advance(2.0)
        return {"ok": True, "pose": w.drone_pose()}

    def goto(self, x, y, z) -> dict:
        w = self.w
        if w.drone["state"] != "flying":
            return {"ok": False, "error": "not flying"}
        dist = math.hypot(x - w.drone["x"], y - w.drone["y"]) + abs(z - w.drone["z"])
        w.advance(dist / 0.5)
        with w.lock:
            w.drone.update(x=x, y=y, z=z)
        return {"ok": True, "pose": w.drone_pose()}

    def scan(self, x0, y0, x1, y1) -> dict:
        w = self.w
        if w.drone["state"] != "flying":
            self.takeoff()
        lx, hx, ly, hy = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        start = (w.drone["x"], w.drone["y"])
        duration = min(max((hx - lx) * (hy - ly) / (0.4 * 0.3), 5.0), 90.0)
        for _ in range(int(duration / 0.5)):
            if w.abort.is_set():
                return {"ok": False, "error": "aborted", "findings": []}
            w.advance(0.5)
        findings = []
        with w.lock:
            for o in w.objects:
                if o is w.held or not (lx <= o["x"] <= hx and ly <= o["y"] <= hy):
                    continue
                findings.append({"x": round(o["x"] + w.rng.uniform(-0.1, 0.1), 2),
                                 "y": round(o["y"] + w.rng.uniform(-0.1, 0.1), 2),
                                 "dz": round(o["height"] + w.rng.uniform(-0.02, 0.02), 2)})
            if w.rng.random() < 0.1:                            # occasional false positive
                findings.append({"x": round(w.rng.uniform(lx, hx), 2), "y": round(w.rng.uniform(ly, hy), 2),
                                 "dz": round(w.rng.uniform(0.12, 0.16), 2)})
            w.drone.update(x=start[0], y=start[1], z=FLY_Z)
        return {"ok": True, "findings": findings}

    def land(self) -> dict:
        w = self.w
        w.abort.set()
        with w.lock:
            if w.drone["state"] == "flying":
                w.drone.update(state="landed", z=0.0)
        return {"ok": True}

    def dock(self) -> dict:
        w = self.w
        if not w.cfg.dock_mode:
            return {"ok": False, "docked": False, "error": "docking disabled"}
        with w.lock:
            if w.drone["state"] == "docked":
                return {"ok": True, "docked": True}
            if w.drone["state"] != "flying" or w.takeoff_rover is None:
                return {"ok": False, "docked": False, "error": "drone is not flying from the plate"}
            moved = math.hypot(w.rover[0] - w.takeoff_rover[0], w.rover[1] - w.takeoff_rover[1])
            if moved > 0.05:
                return {"ok": False, "docked": False,
                        "error": f"rover moved {moved:.2f} m since takeoff; the plate is not where the drone left it"}
        w.advance(3.0)
        with w.lock:
            w.drone.update(state="docked")
            w.takeoff_rover = None
        return {"ok": True, "docked": True}


def build_mock(arena: Arena, cfg):
    """-> (world, rover, drone, clock, faults)"""
    world = MockWorld(arena, cfg)
    return world, MockRover(world), MockDrone(world), SimClock(world), FaultInjector(cfg.mock_fail, cfg.mock_seed + 1)
