"""Safety lives in code, never only in the prompt (plan §8). Tools call these before touching an adapter."""
import threading
from typing import Optional

from .models import Rect

STOP_TIMEOUT_S = 1.0


class Safety:
    def __init__(self, cfg, arena, store, rover, drone):
        self.cfg, self.arena, self.store, self.rover, self.drone = cfg, arena, store, rover, drone

    # ------------------------------------------------------------ gates (return an error string or None)
    def mentor_gate(self) -> Optional[str]:
        if not self.store.mentor_present:
            return "mentor gate: drone tools are locked until a mentor is present (POST /mentor)"
        return None

    def rover_target(self, x: float, y: float) -> Optional[str]:
        """Anywhere on the map (the field plus its margin): the rover is slow and the floor is open, so the limit is
        only there to catch a goal in the wrong frame (metres off the map), not to fence the field."""
        v = self.arena.view_rect()
        if not v.contains(x, y, margin=0.2):
            return f"({x:.2f}, {y:.2f}) is off the map [{v.x0:.1f}..{v.x1:.1f}] x [{v.y0:.1f}..{v.y1:.1f}]"
        return None

    def drone_target(self, x: float, y: float, z: Optional[float] = None) -> Optional[str]:
        if not self.arena.drone_cage.contains(x, y):
            return f"({x:.2f}, {y:.2f}) is outside the drone cage"
        if z is not None and not (0.0 <= z <= self.arena.drone_z_max):
            return f"z={z:.2f} is outside 0..{self.arena.drone_z_max} m"
        return None

    def drone_rect(self, r: Rect) -> Optional[str]:
        r = r.normalized()
        return self.drone_target(r.x0, r.y0) or self.drone_target(r.x1, r.y1)

    def drone_battery(self, volts: Optional[float]) -> Optional[str]:
        if volts is not None and volts < self.cfg.drone_min_battery_v:
            return f"drone battery {volts:.2f} V is below {self.cfg.drone_min_battery_v} V"
        return None

    # ------------------------------------------------------------ caps
    def clamp_speed(self, x: float, y: float, z: float = 0.0):
        cap = self.cfg.rover_max_speed
        return max(-cap, min(cap, x)), max(-cap, min(cap, y)), z

    def clamp_move(self, x: float, y: float, z: float):
        cap = self.cfg.rover_max_move_m
        return max(-cap, min(cap, x)), max(-cap, min(cap, y)), max(-90.0, min(90.0, z))

    # ------------------------------------------------------------ global stop
    def stop_all(self) -> dict:
        """Rover /stop and drone /land in parallel, 1 s each. Never involves the LLM, works from any state."""
        out = {}

        def run(name, fn):
            try:
                fn()
                out[name] = "ok"
            except Exception as e:             # a dead robot must not block the other one stopping
                out[name] = f"error: {e}"

        threads = [threading.Thread(target=run, args=("rover", self.rover.stop), daemon=True),
                   threading.Thread(target=run, args=("drone", self.drone.land), daemon=True)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(STOP_TIMEOUT_S)
        for name in ("rover", "drone"):
            out.setdefault(name, "timeout")
        self.store.event("stop_all", **out)
        return out
