"""The riding Crazyflie as the rover's ONLY position source (ROVER_RIDER=target): glitch filter, calibration of the
mount, and lining the gripper up on a known spot."""
import dataclasses
import math
from pathlib import Path

import pytest

from manager.detector import Target
from manager.localization import RiderLocalization
from manager.models import Pose, load_arena
from manager.runtime import build_ctx
from manager.tools import run_action

FLOOR = str(Path(__file__).resolve().parent.parent / "manager" / "arena.floor.json")


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Rider:
    """The drone server's /pose?cf=target: whatever the test sets."""

    def __init__(self):
        self.read = None

    def pose(self, cf="scout"):
        return self.read or {"ok": False, "error": "no pose"}


def _loc(mount=Pose(x=0.0, y=0.0, yaw=0.0)):
    arena = load_arena(FLOOR)
    arena.rider_mount = mount
    clock, drone = Clock(), Rider()
    return RiderLocalization(None, drone, arena, clock, mount_file=None), clock, drone


def _at(drone, clock, x, y, yaw=0.0, dt=0.1):
    clock.t += dt
    drone.read = {"ok": True, "x": x, "y": y, "z": 0.25, "yaw": yaw, "state": "landed"}


def test_the_mount_turns_the_crazyflie_pose_into_the_rover_centre():
    loc, clock, drone = _loc(Pose(x=-0.1, y=-0.1, yaw=30.0))
    _at(drone, clock, 1.0, 0.0, yaw=120.0)                 # Crazyflie turned 30 deg more than the rover
    p = loc.rover_pose()
    assert p.ok and p.source == "lighthouse_rider" and p.yaw == pytest.approx(90.0)
    assert (p.x, p.y) == (pytest.approx(0.9), pytest.approx(0.1))   # it sits behind-right of the centre


def test_tracking_glitches_are_rejected_and_a_real_move_is_accepted():
    loc, clock, drone = _loc()
    _at(drone, clock, 0.0, 0.0)
    assert loc.rover_pose().as_dict()["x"] == 0.0
    _at(drone, clock, 1.763, -4.05)                        # the dropout seen on the floor on 2026-10-11
    assert loc.rover_pose().as_dict()["y"] == 0.0 and "off the map" in loc.last_reject
    _at(drone, clock, 1.0, 0.5)                            # 1.1 m in 0.1 s: no EP drives like that
    assert loc.rover_pose().as_dict()["x"] == 0.0 and "jumped" in loc.last_reject
    _at(drone, clock, 0.05, 0.0)                           # normal motion goes through
    assert loc.rover_pose().as_dict()["x"] == 0.05
    for _ in range(3):                                     # someone carried it: three reads agree on the new place
        _at(drone, clock, 1.5, 0.8)
        p = loc.rover_pose()
    assert (p.x, p.y) == (1.5, 0.8)


def test_a_lost_rider_is_held_briefly_then_reported_not_trustworthy():
    loc, clock, drone = _loc()
    assert loc.rover_pose().source == "rider_waiting" and not loc.rover_pose().ok
    _at(drone, clock, 0.3, 0.2)
    loc.rover_pose()
    drone.read = None
    clock.t += 0.5
    assert loc.rover_pose().ok
    clock.t += 1.0
    p = loc.rover_pose()
    assert not p.ok and p.source == "rider_lost" and (p.x, p.y) == (0.3, 0.2)


def test_anchoring_and_the_start_cross_cannot_override_the_rider(make_ctrl, monkeypatch):
    monkeypatch.setenv("ARENA", FLOOR)
    c = make_ctrl(rover_rider="target")
    with pytest.raises(Exception, match="riding Crazyflie"):
        c.rover_at_home()
    with pytest.raises(Exception, match="riding Crazyflie"):
        c.rover_anchor("target")


# ---------------------------------------------------------------- on the mock floor
@pytest.fixture
def floor(cfg, monkeypatch):
    monkeypatch.setenv("ARENA", FLOOR)
    ctx, world = build_ctx(dataclasses.replace(cfg, rover_rider="target", perception="map"))
    ctx.world = world
    ctx.store.new_mission("test")
    ctx.store.mentor_present = True
    wreck = {"label": "bottle", "kind": "hostile_drone", "x": -0.2, "y": 0.0, "height": 0.2, "grippable": True,
             "color": "#c00"}
    world.objects[:] = [wreck]
    ctx.detector.set_targets([Target(id="t1", kind="hostile_drone", x=-0.2, y=0.0)])
    return ctx, world, wreck


def test_calibration_measures_a_crooked_mount(floor):
    ctx, world, _ = floor
    world.rider_mount = Pose(x=-0.12, y=-0.10, yaw=35.0)      # how it REALLY sits; the arena file says otherwise
    start = list(world.rover)
    r = run_action(ctx, "rover_calibrate_rider", {})
    assert r["ok"], r
    m = r["mount"]
    assert (m["x"], m["y"], m["yaw"]) == (pytest.approx(-0.12, abs=0.01), pytest.approx(-0.10, abs=0.01),
                                          pytest.approx(35.0, abs=1.0))
    assert math.hypot(world.rover[0] - start[0], world.rover[1] - start[1]) < 0.02     # back where it started
    p = ctx.loc.rover_pose()
    assert (p.x, p.y, p.yaw) == (pytest.approx(world.rover[0], abs=0.01), pytest.approx(world.rover[1], abs=0.01),
                                 pytest.approx(world.rover[2], abs=1.0))


def test_calibration_is_manual_only():
    from manager.tools import vocabulary_text
    assert "rover_calibrate_rider" not in vocabulary_text()


def test_the_gripper_lines_up_on_the_marked_spot(floor):
    ctx, world, wreck = floor
    world.rider_mount = Pose(x=-0.12, y=-0.10, yaw=35.0)
    assert run_action(ctx, "rover_calibrate_rider", {})["ok"]
    (f1,) = ctx.store.add_findings([{"x": -0.2, "y": 0.0, "by": "drone"}])
    r = run_action(ctx, "rover_goto", {"target": f"$finding.{f1.id}"})
    assert r["ok"] and abs(r["facing_error_deg"]) <= 10, r
    r = run_action(ctx, "rover_approach", {"target": "bottle"})
    assert r["ok"] and r["in_reach"] and r["error_cm"] <= 2.0, r
    fwd, left = world.body_coords(wreck)                                   # the truth, not the estimate
    assert fwd == pytest.approx(ctx.arena.rover.grip_reach_m, abs=0.025) and abs(left) < 0.025


def test_a_lost_rider_stops_the_drive_instead_of_guessing(floor, monkeypatch):
    ctx, world, _ = floor
    ctx.loc.rover_pose()
    monkeypatch.setattr(ctx.drone, "pose", lambda cf="scout": {"ok": False, "error": "no pose"})
    r = run_action(ctx, "rover_goto", {"x": 0.5, "y": 0.5})
    assert r["ok"] is False and "lost the rover's position" in r["error"]


def test_blend_mode_keeps_the_rider_plus_odometry_localization(cfg, monkeypatch):
    """ROVER_RIDER_MODE=blend: the rider fix bridged by wheel odometry, as used in the floor demonstrations."""
    from manager.localization import Localization
    monkeypatch.setenv("ARENA", FLOOR)
    ctx, _ = build_ctx(dataclasses.replace(cfg, rover_rider="target", rover_rider_mode="blend"))
    assert type(ctx.loc) is Localization and ctx.loc.rider == "target"
