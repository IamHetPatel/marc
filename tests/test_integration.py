"""The squads' pieces together: the floor arena against drone_server.py's cage, the telemetry monitor against what the
real servers say, the rover video relay, and the mothership driving the real drone_server.py process (mock radio)."""
import dataclasses
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import requests

from manager.adapters.base import RobotError
from manager.controller import Monitor
from manager.models import Rect, load_arena
from manager.perception import NoPerception
from manager.runtime import build_controller, build_ctx
from manager.server import create_app
from manager.tools import run_action
from rover import robot_server as rs

from .fake_robot_servers import Served

ROOT = Path(__file__).resolve().parent.parent
FLOOR = str(ROOT / "manager" / "arena.floor.json")


def _inside(inner: Rect, outer: Rect) -> bool:
    i, o = inner.normalized(), outer.normalized()
    return o.x0 <= i.x0 and i.x1 <= o.x1 and o.y0 <= i.y0 and i.y1 <= o.y1


# ---------------------------------------------------------------- the floor arena
def test_floor_arena_cage_is_the_drone_servers_cage():
    """Two geofences for one cage: if the drone squad re-measures, both files must move together."""
    m = re.search(r'DEFAULT_CAGE = "([^"]+)"', (ROOT / "drone_server.py").read_text())
    x0, y0, x1, y1, z_max = (float(v) for v in m.group(1).split(","))
    a = load_arena(FLOOR)
    assert (a.drone_cage.x0, a.drone_cage.y0, a.drone_cage.x1, a.drone_cage.y1, a.drone_z_max) == (x0, y0, x1, y1, z_max)


def test_floor_arena_is_consistent():
    a = load_arena(FLOOR)
    assert _inside(a.drone_cage, a.bounds), "the map and the rover geofence must cover the flight cage"
    for name, zone in a.zones.items():
        assert _inside(zone, a.bounds), f"zone {name} sticks out of the arena"
    assert a.zones["base"].contains(a.home.x, a.home.y)
    assert all(a.bounds.contains(o.x, o.y) for o in a.mock_objects)


def test_arena_env_selects_the_floor(monkeypatch):
    monkeypatch.setenv("ARENA", FLOOR)
    assert load_arena().drone_cage.x0 < 0                     # Lighthouse frame: origin inside the cage


# ---------------------------------------------------------------- telemetry
def test_lost_tracking_is_flagged_and_the_last_good_pose_kept(cfg):
    ctrl = build_controller(cfg, use_env_llm=False)
    mon = Monitor(ctrl)
    mon.poll()
    good = ctrl.store.robots["drone"]["pose"]
    ctrl.base_ctx.drone.pose = lambda cf="scout": (
        {"ok": False, "error": "pose is far outside the cage: Lighthouse tracking lost", "x": 99.0, "y": 99.0,
         "z": 0.0, "state": "landed", "battery_v": 3.9} if cf == "scout" else {"ok": False})
    mon.poll()
    d = ctrl.store.robots["drone"]
    assert d["online"] and d["pose_ok"] is False and "tracking lost" in d["pose_error"]
    assert d["pose"] == good


def test_health_is_polled_trimmed_and_never_trips_the_watchdog(cfg):
    ctrl = build_controller(cfg, use_env_llm=False)
    mon = Monitor(ctrl)
    mon.poll()
    assert ctrl.store.robots["rover"]["health"] == {"battery_pct": None, "camera": "mock", "last": None,
                                                    "mock": True, "video": False, "error": None}

    def dead():
        raise RobotError("no /health")
    ctrl.base_ctx.drone.health = dead
    mon.poll_health()
    assert mon.fails["drone"] == 0


def test_a_real_rover_without_a_gemini_key_still_starts(cfg):
    """Floor bring-up (manual driving) needs no AI; only seeing with the camera does."""
    ctx, _ = build_ctx(dataclasses.replace(cfg, rover="http://127.0.0.1:9", perception="auto"), llm=None)
    assert isinstance(ctx.perception, NoPerception)
    with pytest.raises(Exception, match="GEMINI_API_KEY"):
        ctx.perception.inspect(b"", "what is it?")


# ---------------------------------------------------------------- rover video relay
@pytest.fixture
def rover_server():
    ep = rs.FakeEP()
    rover = rs.Rover(ep)
    rover.setup()
    camera = rs.Camera(lambda: rs.fake_frames(ep, fps=30)).start()
    server = rs.RobotServer(("127.0.0.1", 0), rover, camera, ep.label, mock=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    camera.stop()
    server.shutdown()
    server.server_close()


def test_the_mothership_relays_the_rover_video(cfg, rover_server):
    ctrl = build_controller(dataclasses.replace(cfg, rover=rover_server), use_env_llm=False)
    with Served(create_app(ctrl)) as mgr:
        r = requests.get(mgr.url + "/rover/video", stream=True, timeout=5)
        try:
            assert r.status_code == 200 and r.headers["content-type"].startswith("multipart/x-mixed-replace")
            assert next(r.iter_content(4096)).startswith(b"--frame")
        finally:
            r.close()


def test_no_video_relay_for_a_mock_rover(cfg):
    with Served(create_app(build_controller(cfg, use_env_llm=False))) as mgr:
        assert requests.get(mgr.url + "/rover/video", timeout=5).status_code == 404


# ---------------------------------------------------------------- the real drone_server.py process
@pytest.fixture
def drone_server():
    pytest.importorskip("flask")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "MOCK": "1", "MOCK_TIME_SCALE": "50", "PORT": str(port)}
    proc = subprocess.Popen([sys.executable, str(ROOT / "drone_server.py")], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                requests.get(url + "/health", timeout=0.5)
                break
            except requests.RequestException:
                time.sleep(0.1)
        else:
            pytest.fail("drone_server.py did not start")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_the_mothership_flies_the_real_drone_server(cfg, drone_server, monkeypatch):
    monkeypatch.setenv("ARENA", FLOOR)
    ctx, _ = build_ctx(dataclasses.replace(cfg, drone=drone_server))
    ctx.store.new_mission("integration")
    ctx.store.mentor_present = True
    assert run_action(ctx, "drone_takeoff", {})["ok"]
    r = run_action(ctx, "drone_goto", {"x": 0.3, "y": 0.2, "z": 0.6})
    assert r["ok"] and r["pose"]["x"] == pytest.approx(0.3)
    refused = run_action(ctx, "drone_goto", {"x": 2.4, "y": 1.1, "z": 0.6})
    assert refused["ok"] is False and "outside the drone cage" in refused["error"]
    assert run_action(ctx, "drone_land", {})["ok"]
    assert ctx.drone.pose()["state"] == "landed"
    assert ctx.drone.health()["cage"]["x0"] == ctx.arena.drone_cage.x0
