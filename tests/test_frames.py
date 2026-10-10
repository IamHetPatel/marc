"""Rover and drone in one frame: anchoring the rover from a Crazyflie, the out-of-area guard, the launch pad and the
dashboard's map view."""
import pytest
from fastapi.testclient import TestClient

from manager.controller import Conflict
from manager.models import load_arena
from manager.server import create_app
from manager.tools import run_action


# ---------------------------------------------------------------- anchor from a Crazyflie
def test_anchor_from_the_target_crazyflie_puts_the_rover_in_the_drone_frame(make_ctrl):
    c = make_ctrl()
    c.world.objects[0].update(x=1.7, y=2.1)                     # the target Crazyflie now sits on the rover's plate
    pose = c.rover_anchor("target")
    p = c.base_ctx.loc.rover_pose()
    assert (pose["x"], pose["y"]) == (1.7, 2.1)
    assert (round(p.x, 2), round(p.y, 2), p.source) == (1.7, 2.1, "crazyflie+odometry")


def test_odometry_carries_on_from_the_anchor(ctx):
    ctx.loc.anchor(1.0, 1.0, 90.0)                              # rover faces +y
    ctx.rover.drive(0.5, 0.0, 0.0)                              # half a metre forward = +y in the Lighthouse frame
    p = ctx.loc.rover_pose()
    assert abs(p.x - 1.0) < 0.05 and abs(p.y - 1.5) < 0.1 and abs(p.yaw - 90.0) < 3


def test_anchor_refuses_a_flying_crazyflie_and_a_running_mission(make_ctrl):
    c = make_ctrl()
    c.base_ctx.drone.takeoff()
    with pytest.raises(Conflict, match="flying"):
        c.rover_anchor("scout")
    with pytest.raises(ValueError):
        c.rover_anchor("rover")


def test_post_rover_anchor(make_ctrl):
    c = make_ctrl()
    with TestClient(create_app(c)) as client:
        r = client.post("/rover/anchor", json={"cf": "target"})
        assert r.status_code == 200 and r.json()["pose"]["x"] == 2.4


# ---------------------------------------------------------------- out-of-area guard
def test_goto_stops_when_the_rovers_position_is_outside_the_mapped_area(ctx):
    ctx.loc.anchor(6.0, 1.5, 0.0)                               # a wrong anchor: the rover "is" 2 m outside the net
    r = run_action(ctx, "rover_goto", {"x": 3.5, "y": 1.5})
    assert r["ok"] is False and r["arrived"] is False and "left the mapped area" in r["error"]


def test_a_goal_outside_the_arena_is_refused_before_moving(ctx):
    r = run_action(ctx, "rover_goto", {"x": 7.0, "y": 1.5})
    assert r["ok"] is False and "outside the arena" in r["error"]


# ---------------------------------------------------------------- launch pad, drone_home, map view
def test_drone_home_flies_back_to_the_first_takeoff_spot(ctx):
    ctx.store.mentor_present = True
    pad = ctx.drone.pose()
    assert run_action(ctx, "drone_mark", {"x": 2.0, "y": 1.5})["ok"]
    r = run_action(ctx, "drone_home", {})
    p = ctx.drone.pose()
    assert r["ok"] and r["docked"] and (p["x"], p["y"], p["state"]) == (pad["x"], pad["y"], "landed")
    assert ctx.drone_out is False


def test_drone_home_needs_the_mentor_to_take_off_again(ctx):
    ctx.store.mentor_present = False                            # landed away from the pad: going home means flying
    r = run_action(ctx, "drone_home", {})
    assert r["blocked"] == "mentor"


def test_mark_height_is_low_enough_for_the_rover_and_does_not_block_it(ctx):
    ctx.store.mentor_present = True
    assert ctx.cfg.mark_z == 0.4
    assert run_action(ctx, "drone_mark", {"x": 2.0, "y": 1.0})["pose"]["z"] == 0.4
    assert run_action(ctx, "rover_goto", {"x": 2.1, "y": 1.0})["arrived"]


def test_the_floor_view_covers_the_whole_net_and_the_arena_endpoint_sends_it(make_ctrl):
    a = load_arena("manager/arena.floor.json")
    v = a.view_rect()
    for x, y in ((2.06, -0.71), (1.96, 1.18), (-1.35, 1.48)):      # measured net corners
        assert v.contains(x, y)
    assert v.contains(a.bounds.x0, a.bounds.y0) and v.contains(a.drone_cage.x1, a.drone_cage.y1)
    with TestClient(create_app(make_ctrl())) as client:
        body = client.get("/arena").json()
        assert set(body["view"]) == {"x0", "y0", "x1", "y1"} and "launch_pad" in body


def test_a_launch_pad_outside_the_arena_grows_the_view_and_starts_the_mock_drone_there():
    a = load_arena("manager/arena.json").model_copy(update={"launch_pad": None})
    from manager.models import Pose
    a = a.model_copy(update={"launch_pad": Pose(x=-0.5, y=0.4)})
    assert a.view_rect().x0 == -0.5


# ---------------------------------------------------------------- ROVER_RIDER: a Crazyflie riding on the EP
@pytest.fixture
def rider_ctx(cfg):
    import dataclasses
    from manager.runtime import build_ctx
    c, world = build_ctx(dataclasses.replace(cfg, rover_rider="target"))
    c.world = world
    c.store.new_mission("test")
    c.store.mentor_present = True
    return c


def test_with_a_rider_the_rover_follows_lighthouse_even_from_a_wrong_start(rider_ctx):
    c = rider_ctx
    c.world.rover = [1.5, 2.0, 30.0]                 # NOT on the start cross: odometry alone would be far off
    p = c.loc.rover_pose()
    assert p.source == "lighthouse_rider"
    assert (round(p.x, 2), round(p.y, 2), round(p.yaw)) == (1.5, 2.0, 30)
    r = run_action(c, "rover_goto", {"x": 2.5, "y": 1.0})
    assert r["arrived"] and abs(c.world.rover[0] - 2.5) < 0.2 and abs(c.world.rover[1] - 1.0) < 0.2


def test_with_the_target_riding_the_detector_ignores_it_and_the_map_has_no_truth(rider_ctx, make_ctrl):
    assert rider_ctx.detector.live_ok is False
    assert all(t["x"] != rider_ctx.world.rover[0] for t in rider_ctx.detector.truth())
