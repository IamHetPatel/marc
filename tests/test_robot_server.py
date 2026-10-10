"""rover/robot_server.py over real sockets: the fake EP behind it, the mothership's RoverHttp client in front.

The fake EP speaks the SDK's frame (+y right, +yaw clockwise), so these tests also prove the server's sign
conversion to the API frame (+y left, +yaw counter-clockwise).
"""
import threading
import time

import pytest
import requests

from manager.adapters.base import RobotError
from manager.adapters.rover_http import RoverHttp
from manager.config import Config
from rover import robot_server as rs


@pytest.fixture
def served():
    ep = rs.FakeEP()
    rover = rs.Rover(ep)
    rover.drive_speed, rover.turn_speed, rover.grip_wait, rover.poll_s = 1.0, 180.0, 0.05, 0.02
    rover.setup()
    camera = rs.Camera(lambda: rs.fake_frames(ep, fps=50)).start()
    stop = threading.Event()
    threading.Thread(target=rover.watchdog, args=(stop,), daemon=True).start()
    server = rs.RobotServer(("127.0.0.1", 0), rover, camera, ep.label, mock=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, ep, rover, RoverHttp(base, Config())
    stop.set()
    camera.stop()
    server.shutdown()
    server.server_close()


def get(base, path, **params):
    return requests.get(base + path, params=params, timeout=10).json()


def test_health(served):
    base, *_ = served
    h = get(base, "/health")
    assert h["ok"] and h["battery"] == 87 and h["mock"] is True and h["moving"] is False


def test_drive_forward_updates_pose(served):
    base, ep, rover, client = served
    r = client.drive(0.2, 0.0, 0.0)
    assert r["ok"] and r["moved"]["x"] == pytest.approx(0.2, abs=0.01)
    assert client.pose()["x"] == pytest.approx(0.2, abs=0.01)


def test_left_and_ccw_are_converted_to_dji_signs(served):
    """API +y = left becomes SDK y < 0; API +z = turn left becomes SDK z < 0."""
    base, ep, rover, client = served
    assert client.drive(0.0, 0.2, 0.0)["moved"]["y"] == pytest.approx(0.2, abs=0.01)
    assert ep.y == pytest.approx(-0.2, abs=0.01)
    assert client.drive(0.0, 0.0, 45.0)["moved"]["yaw"] == pytest.approx(45, abs=1)
    assert ep.yaw == pytest.approx(-45, abs=1)
    assert any(c.startswith("chassis move x 0.000 y -0.200") for c in ep.log)


def test_pose_is_in_the_start_frame_after_turning(served):
    base, ep, rover, client = served
    client.drive(0.0, 0.0, 90.0)
    client.drive(0.2, 0.0, 0.0)                       # facing +y of the start frame now
    p = client.pose()
    assert p["x"] == pytest.approx(0.0, abs=0.02) and p["y"] == pytest.approx(0.2, abs=0.02)
    assert p["yaw"] == pytest.approx(90, abs=1)
    assert get(base, "/pose", reset=1) == {"ok": True, "x": 0.0, "y": 0.0, "yaw": 0.0}


def test_speed_stops_by_itself(served):
    base, ep, rover, client = served
    r = client.speed(0.3, 0.0, 0.0, 0.3)
    assert r["ok"] and ep.vel[0] == pytest.approx(0.3)
    time.sleep(0.6)
    assert ep.vel == (0.0, 0.0, 0.0) and rover.speed_until is None
    x = ep.x
    time.sleep(0.2)
    assert ep.x == x


def test_speed_is_clamped_and_t_is_capped(served):
    base, *_ = served
    r = get(base, "/speed", x=5, z=-500, t=60)
    assert r["x"] == rs.MAX_SPEED and r["z"] == -rs.MAX_YAW_RATE and r["t"] == rs.MAX_SPEED_T
    get(base, "/stop")


def test_stop_interrupts_a_drive_and_busy_is_reported(served):
    base, ep, rover, client = served
    rover.drive_speed = 0.2                            # 1 m takes 5 s
    out = {}
    t = threading.Thread(target=lambda: out.update(client.drive(1.0, 0.0, 0.0)))
    t.start()
    time.sleep(0.3)
    assert get(base, "/speed", x=0.1)["error"].startswith("busy")
    t0 = time.monotonic()
    assert client.stop()["ok"]
    t.join(timeout=3)
    assert time.monotonic() - t0 < 1.0 and out["ok"] is False and out["error"] == "stopped"
    assert ep.move is None and ep.vel == (0.0, 0.0, 0.0)


def test_bad_requests_are_ok_false(served):
    base, *_ = served
    assert "within" in get(base, "/drive", x=5)["error"]
    assert "number" in get(base, "/speed", x="fast")["error"]
    assert get(base, "/grip", action="squeeze")["ok"] is False
    assert get(base, "/arm", preset="wave")["ok"] is False
    r = requests.get(base + "/fly", timeout=5)
    assert r.status_code == 404 and "/drive" in r.json()["endpoints"]


def test_gripper(served):
    base, ep, rover, client = served
    assert client.grip("open") == {"ok": True, "state": "partly open"}     # still moving after 0.05 s
    time.sleep(1.0)
    assert get(base, "/grip", action="close")["ok"] and ep.gripper == "closed"


def test_arm_presets_relative_moves_and_reach(served):
    base, ep, rover, client = served
    assert client.arm("pick")["ok"] and ep.arm == list(rs.ARM_PRESETS["pick"])
    r = get(base, "/arm", x=-20, y=10)
    assert r["ok"] and (r["x"], r["y"]) == (160, -20)
    assert get(base, "/arm")["x"] == 160
    refused = get(base, "/arm", x=100)                  # 260 mm is out of the fake's reach
    assert refused["ok"] is False and "fail" in refused["error"]


def test_snapshot_and_teleop_page(served):
    base, ep, rover, client = served
    for _ in range(50):
        if requests.get(base + "/snapshot", timeout=5).status_code == 200:
            break
        time.sleep(0.05)
    assert client.snapshot()[:2] == b"\xff\xd8"
    page = requests.get(base + "/", timeout=5)
    assert page.status_code == 200 and "/speed?" in page.text


def test_unreachable_robot_is_503_and_a_robot_error(served):
    base, ep, rover, client = served

    def dead(command):
        raise ConnectionRefusedError("no route to EP")
    rover.link.cmd = dead
    rover._battery = (None, -1e9)
    assert requests.get(base + "/health", timeout=5).status_code == 503
    with pytest.raises(RobotError):
        client.pose()
