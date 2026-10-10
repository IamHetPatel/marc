"""WP-M4: manual control goes through the same validation and safety as a plan step."""
import pytest
from fastapi.testclient import TestClient

from manager.controller import Conflict
from manager.server import create_app
from manager.tools import run_action

from .conftest import wait_for, wait_status


def wait_manual(c, status, timeout=15.0):
    wait_for(lambda: (c.store.manual or {}).get("status") == status, timeout, f"manual {status} (now {c.store.manual})")


# ---------------------------------------------------------------- controller
def test_state_has_a_manual_field_that_starts_empty(make_ctrl):
    assert make_ctrl().store.to_json()["manual"] is None


def test_manual_goto_runs_on_mocks_and_ends_done(make_ctrl):
    c = make_ctrl()
    c.manual("rover_goto", {"x": 1.0, "y": 1.0})
    wait_manual(c, "done")
    m = c.store.to_json()["manual"]
    assert m["action"] == "rover_goto" and m["args"] == {"x": 1.0, "y": 1.0} and m["result"]["arrived"] is True
    assert abs(c.world.rover[0] - 1.0) < 0.2 and abs(c.world.rover[1] - 1.0) < 0.2


def test_a_refused_manual_action_ends_failed_with_the_reason(make_ctrl):
    c = make_ctrl(mentor=False)
    c.manual("drone_takeoff", {})
    wait_manual(c, "failed")
    assert c.store.manual["result"]["blocked"] == "mentor"
    assert c.world.drone["state"] == "landed"


def test_manual_is_refused_during_a_mission(make_ctrl):
    c = make_ctrl()
    c.start_mission("Check field A")
    wait_status(c, "awaiting_approval")
    with pytest.raises(Conflict):
        c.manual("rover_snapshot", {})
    c.approve(c.store.mission.plan_version)
    with pytest.raises(Conflict):
        c.manual("rover_snapshot", {})
    wait_status(c, "done", "failed")
    c.manual("rover_snapshot", {})                                     # between missions again: fine
    wait_manual(c, "done")


def test_only_one_manual_action_at_a_time(make_ctrl):
    c = make_ctrl(mock_time_scale=20.0)
    c.manual("rover_goto", {"x": 3.5, "y": 2.5})
    with pytest.raises(Conflict, match="still running"):
        c.manual("rover_snapshot", {})
    c.stop()
    wait_manual(c, "failed")


def test_stop_halts_a_manual_goto(make_ctrl):
    c = make_ctrl(mock_time_scale=20.0)
    c.manual("rover_goto", {"x": 3.5, "y": 2.5})
    wait_for(lambda: c.world.rover[0] > 0.6, what="the rover to start moving")
    c.stop()
    wait_manual(c, "failed")
    assert c.store.manual["result"]["error"] == "cancelled"
    x = c.world.rover[0]
    assert x < 3.0
    import time
    time.sleep(0.2)
    assert c.world.rover[0] == x                                       # it really stopped


def test_manual_works_again_after_a_stop(make_ctrl):
    c = make_ctrl()
    c.stop()                                                           # spends the epoch's cancel flag
    c.manual("rover_snapshot", {})
    wait_manual(c, "done")
    assert c.store.manual["result"]["ok"] is True


def test_a_plan_cannot_be_approved_while_a_manual_action_is_moving_robots(make_ctrl):
    c = make_ctrl(mock_time_scale=20.0)
    c.manual("rover_goto", {"x": 3.5, "y": 2.5})
    c.start_mission("Check field A")
    wait_status(c, "awaiting_approval")
    with pytest.raises(Conflict, match="manual action"):
        c.approve(c.store.mission.plan_version)
    c.stop()


def test_a_new_mission_clears_the_manual_record(make_ctrl):
    c = make_ctrl()
    c.manual("rover_snapshot", {})
    wait_manual(c, "done")
    c.start_mission("Check field A")
    assert c.store.manual is None


# ---------------------------------------------------------------- the two manual-only tools
def test_rover_snapshot_stores_an_image(ctx):
    r = run_action(ctx, "rover_snapshot", {})
    assert r["ok"] and ctx.store.get_image(r["image_id"])[:2] == b"\xff\xd8" and ctx.store.last_image_id == r["image_id"]


def test_rover_drive_moves_relative_to_the_robot(ctx):
    r = run_action(ctx, "rover_drive", {"x": 0.5, "y": 0.0, "z": 0.0})
    assert r["ok"] and r["pose"]["x"] == pytest.approx(ctx.arena.home.x + 0.5, abs=0.1)
    run_action(ctx, "rover_drive", {"x": 0.0, "y": 0.0, "z": 90.0})
    y0 = ctx.world.rover[1]
    run_action(ctx, "rover_drive", {"x": 0.4, "y": 0.0, "z": 0.0})        # now facing +y
    assert ctx.world.rover[1] == pytest.approx(y0 + 0.4, abs=0.05)


def test_rover_drive_is_clamped_and_cannot_leave_the_arena(ctx):
    run_action(ctx, "rover_drive", {"x": 5.0, "y": 0.0, "z": 0.0})
    assert ctx.world.rover[0] == pytest.approx(ctx.arena.home.x + 1.0, abs=0.05)         # 1 m cap per call
    r = run_action(ctx, "rover_drive", {"x": -1.0, "y": -1.0, "z": 0.0})
    assert r["ok"] is False and "outside the arena" in r["error"]


def test_rover_drive_obeys_the_separation_rule(ctx):
    assert run_action(ctx, "drone_takeoff", {})["ok"]                  # scout hovers low beside the rover
    r = run_action(ctx, "rover_drive", {"x": 0.2, "y": 0.0, "z": 0.0})
    assert r["blocked"] == "separation"


# ---------------------------------------------------------------- HTTP
@pytest.fixture
def client(make_ctrl):
    ctrl = make_ctrl()
    with TestClient(create_app(ctrl)) as c:
        c.ctrl = ctrl
        yield c


def test_post_manual_is_accepted_and_visible_in_state(client):
    r = client.post("/manual", json={"robot": "rover", "action": "rover_snapshot", "args": {}})
    assert r.status_code == 202 and r.json() == {"ok": True}
    wait_manual(client.ctrl, "done")
    st = client.get("/state").json()
    assert st["manual"]["status"] == "done" and st["last_image_id"] == st["manual"]["result"]["image_id"]


def test_post_manual_validates_the_action(client):
    assert client.post("/manual", json={"robot": "rover", "action": "teleport"}).status_code == 422
    assert client.post("/manual", json={"robot": "drone", "action": "rover_goto", "args": {"x": 1, "y": 1}}).status_code == 422
    r = client.post("/manual", json={"robot": "rover", "action": "rover_grip", "args": {"mode": "squeeze"}})
    assert r.status_code == 422 and "must be one of" in r.json()["detail"]
    assert client.post("/manual", json={"robot": "rover", "action": "rover_goto", "args": {}}).status_code == 422


def test_post_manual_during_a_mission_is_409(client):
    client.post("/mission", json={"text": "Check field A"})
    wait_status(client.ctrl, "awaiting_approval")
    r = client.post("/manual", json={"robot": "rover", "action": "rover_snapshot", "args": {}})
    assert r.status_code == 409


def test_arena_endpoint_describes_the_map(client):
    a = client.get("/arena").json()
    assert {"bounds", "drone_cage", "zones", "home", "camera", "calibrated"} <= set(a)
    assert {"field_a", "field_b", "base", "disposal"} <= set(a["zones"])
    assert a["zones"]["field_a"] == {"x0": 1.2, "y0": 0.3, "x1": 3.7, "y1": 1.6}
    assert a["bounds"] == {"x0": 0.0, "y0": 0.0, "x1": 4.0, "y1": 3.0} and "mock_objects" not in a
