"""Simulated downward camera for the scout (docs/history/mothership-plan.md §1.5), and the search pattern it flies.

The Crazyflies have no camera, so "seeing" the crashed drone is a sensor model: while the scout hovers at
height z, a target counts as seen when it lies inside the footprint a real downward camera would have
(radius z * tan(FOV/2)), with Gaussian position noise and a chance of missing it. The scout really has to fly
the pattern; nothing here lets it read the answer. The truth comes from the target Crazyflie's own Lighthouse
pose (live) or from a scenario file (fixed). In this lab setup Lighthouse stands in for RTK-GPS and this detector
stands in for a camera; everything after detection is real.
"""
import math
import random
from dataclasses import dataclass
from typing import Optional

from .adapters.base import RobotError
from .models import Rect


@dataclass
class Target:
    id: str
    kind: str = "hostile_drone"       # own_drone | hostile_drone | decoy
    source: str = "fixed"             # live = pose from the target Crazyflie; fixed = use x, y
    x: float = 0.0
    y: float = 0.0


class SimDetector:
    def __init__(self, drone, fov_deg: float = 60.0, noise_m: float = 0.05, miss_p: float = 0.05,
                 rng: Optional[random.Random] = None):
        self.drone = drone
        self.fov_deg, self.noise_m, self.miss_p = fov_deg, noise_m, miss_p
        self.rng = rng or random.Random()
        self.targets = [Target(id="t1", source="live")]     # default: the second Crazyflie, wherever it is
        self.live_ok = True                 # False when the target Crazyflie rides on the rover (ROVER_RIDER=target)

    def set_targets(self, targets: list):
        self.targets = list(targets)

    def footprint_radius(self, z: float) -> float:
        return max(z, 0.0) * math.tan(math.radians(self.fov_deg / 2))

    def truth(self) -> list:
        """Where the targets really are right now: [{id, x, y}]. A live target that cannot be read is skipped.
        There is one target Crazyflie, so at most one target should be `live`."""
        out, live = [], None
        for t in self.targets:
            if t.source == "live":
                if not self.live_ok:
                    continue
                if live is None:
                    try:
                        live = self.drone.pose(cf="target")
                    except RobotError:
                        live = {"ok": False}
                if live.get("ok", True) is not False:
                    out.append({"id": t.id, "x": live["x"], "y": live["y"]})
            else:
                out.append({"id": t.id, "x": t.x, "y": t.y})
        return out

    def observe(self, drone_pose: dict) -> list:
        """What the downward camera reports from this pose: [{x, y, target_id, error_m}], x/y noisy."""
        r = self.footprint_radius(drone_pose.get("z") or 0.0)
        hits = []
        for t in self.truth():
            if math.hypot(t["x"] - drone_pose["x"], t["y"] - drone_pose["y"]) > r:
                continue
            if self.rng.random() < self.miss_p:
                continue
            nx, ny = self.rng.gauss(0, self.noise_m), self.rng.gauss(0, self.noise_m)
            hits.append({"x": round(t["x"] + nx, 3), "y": round(t["y"] + ny, 3), "target_id": t["id"],
                         "error_m": round(math.hypot(nx, ny), 3)})
        return hits


def _spread(lo: float, hi: float, margin: float, spacing: float) -> list:
    """Points from lo+margin to hi-margin no further apart than `spacing`; one centred point if the span is thin."""
    if hi - lo <= 2 * margin:
        return [(lo + hi) / 2]
    a, b = lo + margin, hi - margin
    n = math.ceil((b - a) / spacing) + 1
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def search_waypoints(rect: Rect, r: float) -> list:
    """Lawnmower over `rect` for a camera footprint of radius r: lanes sqrt(2) r apart, waypoints r apart along a
    lane, alternating direction. The pattern is inset by r/sqrt(2) from every edge, which puts the rectangle's
    corners exactly on the edge of a footprint, so every point of the rectangle is within r of some waypoint.
    (The mothership plan suggested 1.6 r lanes; with the edge inset that leaves a few millimetres of blind strip
    along the boundary between lanes, and sqrt(2) r is the widest spacing that closes it.)"""
    rect = rect.normalized()
    inset = r / math.sqrt(2)
    ys = _spread(rect.y0, rect.y1, inset, math.sqrt(2) * r)
    xs = _spread(rect.x0, rect.x1, inset, r)
    path = []
    for i, y in enumerate(ys):
        path += [(x, y) for x in (xs if i % 2 == 0 else reversed(xs))]
    return path
