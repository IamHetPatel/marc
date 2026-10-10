"""The HTTP contract the dashboard builds against (docs/ai-manager-plan.md §9)."""
import time

import pytest
from fastapi.testclient import TestClient

from manager.server import create_app



@pytest.fixture
def client(make_ctrl):
    ctrl = make_ctrl()
    with TestClient(create_app(ctrl)) as c:
        c.ctrl = ctrl
        yield c


def poll(client, pred, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = client.get("/state").json()
        if pred(st):
            return st
        time.sleep(0.02)
    raise AssertionError("timed out; last state: " + str(st["mission"]))


def test_state_when_idle(client):
    st = client.get("/state").json()
    assert st["mission"]["status"] == "idle" and st["plan"] == [] and st["mentor_present"] is True


def test_mission_approve_run_flow(client):
    assert client.post("/mission", json={"text": "Check the exit"}).json()["ok"]
    st = poll(client, lambda s: s["mission"]["status"] == "awaiting_approval")
    assert st["plan"] and st["mission"]["plan_version"] == 1
    r = client.post("/approve", json={"plan_version": st["mission"]["plan_version"]})
    assert r.status_code == 200
    st = poll(client, lambda s: s["mission"]["status"] in ("done", "failed"))
    assert st["mission"]["status"] == "done"
    image_id = st["findings"][0]["image_id"]
    img = client.get(f"/images/{image_id}.jpg")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and img.content[:2] == b"\xff\xd8"


def test_stale_approval_is_409(client):
    client.post("/mission", json={"text": "Check the exit"})
    poll(client, lambda s: s["mission"]["status"] == "awaiting_approval")
    r = client.post("/approve", json={"plan_version": 99})
    assert r.status_code == 409 and "plan changed" in r.json()["detail"]
    assert client.get("/state").json()["mission"]["status"] == "awaiting_approval"


def test_approve_with_nothing_pending_is_409(client):
    assert client.post("/approve", json={"plan_version": 1}).status_code == 409


def test_second_mission_while_active_is_409(client):
    client.post("/mission", json={"text": "a"})
    poll(client, lambda s: s["mission"]["status"] == "awaiting_approval")
    assert client.post("/mission", json={"text": "b"}).status_code == 409


def test_empty_mission_is_422_and_bad_body_is_422(client):
    assert client.post("/mission", json={"text": "  "}).status_code == 422
    assert client.post("/mission", json={}).status_code == 422


def test_change_without_a_mission_is_409(client):
    assert client.post("/change", json={"text": "hello"}).status_code == 409


def test_stop_always_works_and_reports_both_robots(client):
    r = client.post("/stop")
    assert r.status_code == 200 and r.json()["robots"] == {"rover": "ok", "drone": "ok"}
    assert client.get("/state").json()["mission"]["status"] == "stopped"


def test_mentor_toggle(client):
    assert client.post("/mentor", json={"present": False}).json()["mentor_present"] is False
    assert client.get("/state").json()["mentor_present"] is False


def test_reset(client):
    client.post("/mission", json={"text": "a"})
    poll(client, lambda s: s["mission"]["status"] == "awaiting_approval")
    assert client.post("/reset").status_code == 200
    assert client.get("/state").json()["mission"]["status"] == "idle"


def test_unknown_image_is_404(client):
    assert client.get("/images/img_9999.jpg").status_code == 404


def test_cors_is_open_for_the_dashboard(client):
    r = client.options("/state", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"})
    assert r.headers["access-control-allow-origin"] in ("*", "http://localhost:3000")


def test_telemetry_monitor_fills_robot_status(client):
    st = poll(client, lambda s: s["robots"]["rover"].get("online") and s["robots"]["drone"].get("online"))
    assert st["robots"]["rover"]["pose_source"] == "start_cross+odometry"           # no docking: no drone fix
    assert st["robots"]["drone"]["state"] == "landed" and st["robots"]["drone"]["battery_v"] > 3.3
    assert st["metrics"]["rover_rtt_ms"] is not None


def test_the_target_crazyflie_shows_up_as_ground_truth(client):
    st = poll(client, lambda s: s["robots"]["target"].get("online"))
    assert st["robots"]["target"]["pose"] == {"x": 2.4, "y": 1.1, "z": 0.0}
    client.ctrl.world.objects[0]["x"] = 2.0                           # someone carries the wreck
    st = poll(client, lambda s: s["robots"]["target"]["pose"]["x"] == 2.0)
    assert st["robots"]["target"]["online"] is True
