"""Map perception: the rover camera steps answer from Lighthouse positions, no AI (PERCEPTION=map)."""
from types import SimpleNamespace

from manager.detector import SimDetector, Target
from manager.models import Camera, load_arena
from manager.perception import MapPerception


def _perception(x, y, yaw, targets):
    p = MapPerception(load_arena())
    det = SimDetector(drone=None)
    det.set_targets(targets)
    pose = SimpleNamespace(x=x, y=y, yaw=yaw)
    p.bind(SimpleNamespace(loc=SimpleNamespace(rover_pose=lambda: pose), detector=det, store=None))
    return p


WRECK = [Target(id="t1", kind="hostile_drone", x=1.0, y=0.0)]


def test_a_target_behind_the_rover_is_not_seen():
    assert _perception(0, 0, 180, WRECK).point(b"", "crashed drone") is None


def test_a_far_target_ahead_is_centred_and_above_the_grip_line():
    cam = Camera()
    p = _perception(0, 0, 0, WRECK).point(b"", "crashed drone")
    assert abs(p["col"] - cam.width / 2) < 1 and p["row"] < cam.grip_line_row


def test_a_target_left_of_centre_shows_left_and_reaches_the_grip_line_when_close():
    cam = Camera()
    left = _perception(0, 0, -20, WRECK).point(b"", "x")     # rover turned right: the wreck is to its left
    assert left["col"] < cam.width / 2
    near = _perception(0.78, 0, 0, WRECK).point(b"", "x")    # 0.22 m ahead
    assert near["row"] >= cam.grip_line_row


def test_inspect_uses_the_scenario_kind_and_says_it_is_map_mode():
    r = _perception(0.5, 0, 0, WRECK).inspect(b"", "what is it?")
    assert r["category"] == "hostile_drone" and r["hazardous"] and "Map mode" in r["answer"]
    assert _perception(-2, 0, 0, WRECK).inspect(b"", "?")["label"] == "nothing"


def test_holding_only_when_the_target_is_in_gripper_reach():
    assert _perception(0.75, 0, 0, WRECK).holding(b"")["holding"]
    assert not _perception(0.0, 0, 0, WRECK).holding(b"")["holding"]
