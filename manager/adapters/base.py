"""The only robot interface the tools see. No hardware library is ever imported under manager/.

Conventions (same for mock and HTTP adapters):
  * metres, Lighthouse frame, yaw in degrees counter-clockwise
  * rover body frame: x forward, y LEFT, z turn-left (CCW). HTTP adapters translate to whatever the
    rover server uses (see Config.rover_*_sign).
  * every method returns a dict (or bytes for snapshots) and raises RobotError only when the robot is unreachable
"""
import time
from typing import Protocol, runtime_checkable


class RobotError(Exception):
    """The robot server did not answer (timeout, connection refused, bad status)."""


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float):
        time.sleep(max(0.0, seconds))


@runtime_checkable
class RoverAdapter(Protocol):
    def pose(self) -> dict:
        """Wheel odometry in the rover's own frame: {x, y, yaw}. Not Lighthouse; see localization.py."""

    def speed(self, x: float, y: float, z: float, t: float) -> dict:
        """Velocity command (m/s fwd, m/s left, deg/s turn-left) that must auto-expire after t seconds."""

    def drive(self, x: float, y: float, z: float) -> dict:
        """Blocking relative move: forward m, left m, turn-left deg."""

    def snapshot(self) -> bytes:
        """One JPEG from the front camera."""

    def grip(self, action: str) -> dict:
        """'open' or 'close'."""

    def arm(self, preset: str) -> dict:
        """'pick', 'carry' or 'home'."""

    def stop(self) -> dict:
        """Zero velocity now."""


@runtime_checkable
class DroneAdapter(Protocol):
    def pose(self) -> dict:
        """{x, y, z, yaw, battery_v, state: docked|flying|landed} in the Lighthouse frame."""

    def takeoff(self) -> dict: ...

    def goto(self, x: float, y: float, z: float) -> dict: ...

    def scan(self, x0: float, y0: float, x1: float, y1: float) -> dict:
        """Blocking sweep. Returns {findings: [{x, y, dz}]}. Leaves the drone hovering at the start point."""

    def land(self) -> dict:
        """Land in place. Must work while a scan is running (threaded server)."""

    def dock(self) -> dict:
        """Land at the stored takeoff pose / plate height."""
