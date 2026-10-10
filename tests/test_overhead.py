"""Overhead webcam backup (manager/overhead.py): calibration from floor tags, rover tracking, locating objects, and
how the rest of the manager uses it. Frames are synthetic but real images with real ArUco tags."""
import dataclasses
import math
import threading

import pytest

cv2 = pytest.importorskip("cv2")

from fastapi.testclient import TestClient

from manager.llm import ScriptedLLM
from manager.localization import Localization
from manager.models import load_arena
from manager.overhead import OverheadCamera, OverheadDetector, OverheadError, write_tags
from manager.overhead_mock import MockOverheadSource, MockPointer, SyntheticView
from manager.planner import rules_text
from manager.runtime import build_ctx
from manager.server import create_app
from manager.tools import run_action


class FakeWorld:
    def __init__(self, rover=(1.0, 1.0, 0.0), objects=()):
        self.lock = threading.RLock()
        self.rover = list(rover)
        self.objects = [dict(o) for o in objects]


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


DRONE = {"label": "crashed drone", "x": 2.4, "y": 1.1, "height": 0.05, "color": "#c0392b"}
BOTTLE = {"label": "bottle", "x": 1.2, "y": 2.4, "height": 0.2, "color": "#2980b9"}


def make_cam(world, arena=None, width=1920, height=1080, clock=None, **overhead):
    arena = arena or load_arena()
    if overhead:
        arena = arena.model_copy(update={"overhead": arena.overhead.model_copy(update=overhead)})
    view = SyntheticView(arena, width, height)
    cam = OverheadCamera(MockOverheadSource(world, view, arena), arena.overhead, arena.bounds,
                         llm=MockPointer(world, view), clock=clock or Clock())
    return cam


def tick(cam):
    cam.process(cam.source.read())


# ---------------------------------------------------------------- calibration
def test_four_floor_tags_calibrate_the_camera():
    cam = make_cam(FakeWorld())
    assert cam.status()["calibrated"] is False
    tick(cam)
    st = cam.status()
    assert st["calibrated"] and st["floor_tags_seen"] == 4 and st["reproj_err_cm"] < 1.0
    for x, y in [(0.0, 0.0), (4.0, 3.0), (2.0, 1.5), (3.3, 0.4)]:
        px = cam.source.view.to_px(x, y)
        gx, gy = cam.pixel_to_floor(*px)
        assert math.dist((gx, gy), (x, y)) < 0.01


def test_without_calibration_pointing_is_refused():
    cam = make_cam(FakeWorld(objects=[DRONE]), floor_tags={"0": [-0.1, -0.1], "1": [4.1, -0.1], "2": [4.1, 3.1],
                                                           "3": [-0.1, 3.1], "4": [9.0, 9.0]})
    shown = {"0": [-0.1, -0.1], "3": [-0.1, 3.1]}    # only 2 of the 5 configured tags are in the picture
    cam.source.cfg = cam.source.cfg.model_copy(update={"floor_tags": shown})
    with pytest.raises(OverheadError, match="2 of 5 floor tags"):
        cam.locate("crashed drone")


def test_a_hidden_floor_tag_keeps_the_last_calibration():
    cam = make_cam(FakeWorld())
    tick(cam)
    H = cam.H
    shown = {k: v for k, v in cam.source.cfg.floor_tags.items() if k != "0"}
    cam.source.cfg = cam.source.cfg.model_copy(update={"floor_tags": shown})     # something now covers tag 0
    tick(cam)
    assert cam.status()["floor_tags_seen"] == 3 and cam.status()["calibrated"] and cam.H is H


def test_a_bumped_camera_recalibrates_itself():
    arena = load_arena()
    world = FakeWorld()
    cam = make_cam(world, arena)
    tick(cam)
    cam.source.view = SyntheticView(arena, 1920, 1080, margin_m=0.6)     # same camera, zoomed out a bit
    tick(cam)
    px = cam.source.view.to_px(3.0, 2.0)
    assert math.dist(cam.pixel_to_floor(*px), (3.0, 2.0)) < 0.01


# ---------------------------------------------------------------- rover tracking
@pytest.mark.parametrize("pose", [(1.0, 1.0, 0.0), (3.2, 2.1, 90.0), (0.6, 2.5, -135.0), (2.0, 0.5, 33.0)])
def test_the_rover_tag_gives_position_and_heading(pose):
    cam = make_cam(FakeWorld(rover=pose))
    tick(cam)
    r = cam.rover_pose()
    assert math.dist((r["x"], r["y"]), pose[:2]) < 0.02
    assert abs((r["yaw"] - pose[2] + 180) % 360 - 180) < 2.0
    assert cam.status()["rover_seen"]


def test_parallax_of_the_raised_tag_is_corrected():
    """A tag 0.3 m up under a 2.5 m camera, near the arena corner, looks ~20 cm further out than it is."""
    pose = (3.6, 2.6, 0.0)
    cam = make_cam(FakeWorld(rover=pose))
    tick(cam)
    assert math.dist((cam.rover["x"], cam.rover["y"]), pose[:2]) < 0.02
    naive = make_cam(FakeWorld(rover=pose))
    naive.cfg = naive.cfg.model_copy(update={"rover_tag_height_m": 0.0})    # same picture, read as if the tag were on the floor
    tick(naive)
    assert math.dist((naive.rover["x"], naive.rover["y"]), pose[:2]) > 0.15


def test_an_angled_camera_corrects_parallax_towards_the_floor_point_below_it():
    """Railing camera beside the net: the raised tag is pushed away from the lens's floor point, not the image centre."""
    pose = (3.4, 2.5, 0.0)
    cam = make_cam(FakeWorld(rover=pose), camera_floor_xy=[2.0, -1.5], camera_height_m=4.0)
    tick(cam)
    assert math.dist((cam.rover["x"], cam.rover["y"]), pose[:2]) < 0.02
    centred = make_cam(FakeWorld(rover=pose), camera_floor_xy=[2.0, -1.5], camera_height_m=4.0)
    centred.cfg = centred.cfg.model_copy(update={"camera_floor_xy": None})    # same picture, straight-down assumption
    tick(centred)
    assert math.dist((centred.rover["x"], centred.rover["y"]), pose[:2]) > 0.1


def test_tag_mounting_offset_and_rotation_are_applied():
    pose = (2.0, 1.5, 60.0)
    cam = make_cam(FakeWorld(rover=pose), rover_tag_offset_m=[-0.08, 0.05], rover_tag_yaw_offset_deg=90.0)
    tick(cam)
    r = cam.rover_pose()
    assert math.dist((r["x"], r["y"]), pose[:2]) < 0.02 and abs(r["yaw"] - 60.0) < 2.0


def test_rover_pose_reports_its_age():
    clock = Clock()
    cam = make_cam(FakeWorld(), clock=clock)
    assert cam.rover_pose() is None
    tick(cam)
    clock.t += 3.0
    assert cam.rover_pose()["age_s"] == pytest.approx(3.0) and cam.status()["rover_seen"] is False


# ---------------------------------------------------------------- locating objects
def test_locate_turns_a_pointed_pixel_into_metres():
    cam = make_cam(FakeWorld(objects=[DRONE, BOTTLE]))
    hits = cam.locate("crashed drone")
    assert [h["label"] for h in hits] == ["crashed drone"]
    assert math.dist((hits[0]["x"], hits[0]["y"]), (2.4, 1.1)) < 0.03
    assert len(cam.locate("object")) == 2 and cam.locate("cat") == []


def test_points_outside_the_arena_are_dropped():
    cam = make_cam(FakeWorld(objects=[{**DRONE, "x": 4.2, "y": 1.0}]))
    assert cam.locate("crashed drone") == []


def test_locate_needs_an_llm():
    cam = make_cam(FakeWorld(objects=[DRONE]))
    cam.llm = None
    with pytest.raises(OverheadError, match="GEMINI_API_KEY"):
        cam.locate("crashed drone")


def test_the_preview_is_a_jpeg_once_there_is_a_frame():
    cam = make_cam(FakeWorld(objects=[DRONE]))
    assert cam.preview_jpeg() is None
    cam.locate("crashed drone")
    assert cam.preview_jpeg()[:2] == b"\xff\xd8"


# ---------------------------------------------------------------- the scout's detector, backed by the camera
def test_the_detector_reports_only_what_is_under_the_scout_and_calls_the_llm_once_per_search():
    clock = Clock()
    cam = make_cam(FakeWorld(objects=[DRONE]))
    calls = []
    real = cam.llm.generate
    cam.llm.generate = lambda *a, **k: calls.append(1) or real(*a, **k)
    det = OverheadDetector(cam, "crashed drone", fov_deg=60.0, refresh_s=10.0, clock=clock)
    assert det.observe({"x": 1.0, "y": 1.0, "z": 0.6}) == []
    (hit,) = det.observe({"x": 2.3, "y": 1.2, "z": 0.6})
    assert math.dist((hit["x"], hit["y"]), (2.4, 1.1)) < 0.03 and hit["error_m"] is None
    assert len(calls) == 1
    clock.t += 11
    det.observe({"x": 2.3, "y": 1.2, "z": 0.6})
    assert len(calls) == 2


def test_a_failing_camera_makes_the_detector_see_nothing_instead_of_crashing():
    cam = make_cam(FakeWorld(objects=[DRONE]))
    cam.llm = None
    assert OverheadDetector(cam, "crashed drone", clock=Clock()).observe({"x": 2.4, "y": 1.1, "z": 0.6}) == []


# ---------------------------------------------------------------- localization
class FakeOdomRover:
    def __init__(self):
        self.odom = {"x": 0.0, "y": 0.0, "yaw": 0.0}

    def pose(self):
        return dict(self.odom)


class SimClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t


class FakeOverhead:
    def __init__(self):
        self.sighting = None

    def rover_pose(self):
        return self.sighting


def test_a_fresh_overhead_sighting_is_the_rovers_position_and_odometry_carries_it_forward():
    arena = load_arena()
    rover, ov, clock = FakeOdomRover(), FakeOverhead(), SimClock()
    loc = Localization(rover, None, arena, clock, use_drone_fix=False, overhead=ov)
    assert loc.rover_pose().source == "start_cross+odometry"
    ov.sighting = {"x": 2.0, "y": 1.0, "yaw": 90.0, "t": 5.0, "age_s": 0.1}
    p = loc.rover_pose()
    assert (round(p.x, 3), round(p.y, 3), round(p.yaw, 1), p.source) == (2.0, 1.0, 90.0, "overhead_camera")
    rover.odom = {"x": 0.5, "y": 0.0, "yaw": 0.0}        # wheels: 0.5 m forward in the odometry frame
    clock.t = 2.0                                         # and no new sighting for a while
    p = loc.rover_pose()
    assert (round(p.x, 3), round(p.y, 3), p.source) == (2.0, 1.5, "fix+odometry")


def test_a_stale_or_repeated_sighting_does_not_reset_the_fix():
    arena = load_arena()
    rover, ov, clock = FakeOdomRover(), FakeOverhead(), SimClock()
    loc = Localization(rover, None, arena, clock, use_drone_fix=False, overhead=ov)
    ov.sighting = {"x": 2.0, "y": 1.0, "yaw": 0.0, "t": 5.0, "age_s": 0.1}
    loc.rover_pose()
    rover.odom = {"x": 0.4, "y": 0.0, "yaw": 0.0}
    assert round(loc.rover_pose().x, 3) == 2.4            # same capture again: odometry still counts
    ov.sighting = {"x": 0.0, "y": 0.0, "yaw": 0.0, "t": 6.0, "age_s": 5.0}
    assert round(loc.rover_pose().x, 3) == 2.4            # too old to trust


# ---------------------------------------------------------------- wiring: tool, runtime, server, planner
@pytest.fixture
def ov_ctx(cfg):
    c, world = build_ctx(dataclasses.replace(cfg, overhead_cam="mock"))
    c.world = world
    c.store.new_mission("test")
    yield c
    c.overhead.halt()


def test_camera_locate_registers_a_finding_with_the_overhead_photo(ov_ctx):
    res = run_action(ov_ctx, "camera_locate", {"target": "crashed drone"})
    assert res["ok"] and res["image_id"]
    (f,) = res["findings"]
    assert f["id"] == "f1" and f["by"] == "overhead" and f["label"] == "crashed drone"
    assert math.dist((f["x"], f["y"]), (2.4, 1.1)) < 0.03
    assert ov_ctx.store.get_finding("f1").image_id == res["image_id"]


def test_camera_locate_first_or_all(ov_ctx):
    ov_ctx.world.objects.append({**BOTTLE, "label": "crashed drone two"})
    assert len(run_action(ov_ctx, "camera_locate", {"target": "crashed drone"})["findings"]) == 1
    assert len(run_action(ov_ctx, "camera_locate", {"target": "crashed drone", "mode": "all"})["findings"]) == 2


def test_camera_locate_without_a_camera_is_an_error_not_a_crash(ctx):
    res = run_action(ctx, "camera_locate", {"target": "crashed drone"})
    assert res["ok"] is False and "OVERHEAD_CAM" in res["error"]


def test_the_mock_overhead_drives_drone_search_but_never_the_mock_rovers_position(ov_ctx):
    assert type(ov_ctx.detector).__name__ == "OverheadDetector"
    assert ov_ctx.loc.overhead is None


def test_a_real_overhead_steers_a_real_rover_only(cfg, monkeypatch):
    import manager.runtime as rt
    monkeypatch.setattr(rt, "build_overhead", lambda *a: FakeOverhead())
    real_rover = dataclasses.replace(cfg, rover="http://127.0.0.1:9", overhead_cam="0", detector="sim")
    c, _ = build_ctx(real_rover, llm=ScriptedLLM([]))
    assert isinstance(c.loc.overhead, FakeOverhead)
    c, _ = build_ctx(dataclasses.replace(real_rover, rover="mock"), llm=ScriptedLLM([]))
    assert c.loc.overhead is None


def test_overhead_jpg_and_status_on_the_server(make_ctrl):
    c = make_ctrl(overhead_cam="mock")
    with TestClient(create_app(c)) as client:
        for _ in range(50):
            r = client.get("/overhead.jpg")
            if r.status_code == 200:
                break
            threading.Event().wait(0.05)
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        c.monitor.poll()
        o = client.get("/state").json()["robots"]["overhead"]
    assert o["online"] and o["calibrated"] and o["floor_tags_seen"] == 4


def test_overhead_jpg_is_404_without_a_camera(make_ctrl):
    with TestClient(create_app(make_ctrl())) as client:
        assert client.get("/overhead.jpg").status_code == 404


def test_the_planner_hears_about_the_overhead_camera_only_when_it_is_installed():
    assert "camera_locate" in rules_text(False, overhead=True)
    assert "camera_locate" not in rules_text(False)


def test_printed_tags_are_detectable(tmp_path):
    arena = load_arena()
    paths = write_tags(str(tmp_path), arena.overhead)
    assert len(paths) == len(arena.overhead.floor_tags) + 1
    det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50))
    found = set()
    for p in paths:
        _, ids, _ = det.detectMarkers(cv2.imread(str(p), cv2.IMREAD_GRAYSCALE))
        found |= set(ids.flatten().tolist())
    assert found == {*(int(k) for k in arena.overhead.floor_tags), arena.overhead.rover_tag_id}
