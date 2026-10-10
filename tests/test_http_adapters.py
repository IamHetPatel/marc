"""RoverHttp / DroneHttp against fake robot servers over real sockets, and a full mission through them."""
import dataclasses

import pytest

from manager.adapters.base import RobotError
from manager.adapters.drone_http import DroneHttp
from manager.adapters.mock import build_mock
from manager.adapters.rover_http import RoverHttp
from manager.controller import Controller
from manager.localization import Localization
from manager.models import load_arena
from manager.perception import MockPerception
from manager.planner import Planner
from manager.safety import Safety
from manager.state import Store
from manager.supervisor import LLMSupervisor
from manager.tools import Ctx, run_action

from .conftest import run_to_end
from .fake_robot_servers import Served, drone_app, rover_app


@pytest.fixture
def stack(cfg):
    """Mock world behind two HTTP servers, driven by the real HTTP clients."""
    arena = load_arena()
    world, m_rover, m_drone, clock, faults = build_mock(arena, cfg)
    with Served(rover_app(m_rover)) as r_srv, Served(drone_app(m_drone)) as d_srv:
        cfg = dataclasses.replace(cfg, rover=r_srv.url, drone=d_srv.url, perception="mock")
        rover, drone = RoverHttp(cfg.rover, cfg), DroneHttp(cfg.drone)
        store = Store()
        store.mentor_present = True
        ctx = Ctx(cfg=cfg, arena=arena, store=store, rover=rover, drone=drone,
                  loc=Localization(rover, drone, arena, clock), perception=MockPerception(world),
                  safety=Safety(cfg, arena, store, rover, drone), clock=clock)
        yield ctx, world, r_srv, d_srv


def test_clients_round_trip(stack):
    ctx, world, *_ = stack
    assert set(ctx.rover.pose()) == {"x", "y", "yaw"}
    assert ctx.rover.snapshot()[:2] == b"\xff\xd8"
    assert ctx.rover.grip("open")["ok"] and ctx.rover.arm("pick")["ok"] and ctx.rover.stop()["ok"]
    assert ctx.drone.pose()["state"] == "docked" and ctx.rover.rtt_ms is not None


def test_speed_sign_convention_survives_the_wire(stack):
    """Manager says +y = left. The fake server speaks SDK (+y = right). With the default
    rover_speed_y_sign=-1 a strafe-left command must move the rover left."""
    ctx, world, *_ = stack
    y0 = world.rover[1]
    ctx.rover.speed(0.0, 0.2, 0.0, 1.0)
    assert world.rover[1] - y0 == pytest.approx(0.2, abs=1e-6)               # yaw 0: left = +y world


def test_wrong_sign_config_would_drive_the_wrong_way(stack):
    ctx, world, *_ = stack
    flipped = RoverHttp(ctx.rover.http.base, dataclasses.replace(ctx.cfg, rover_speed_y_sign=1.0))
    y0 = world.rover[1]
    flipped.speed(0.0, 0.2, 0.0, 1.0)
    assert world.rover[1] < y0                                                # proves the config knob matters


def test_goto_over_http_reaches_a_point_left_of_the_start(stack):
    ctx, world, *_ = stack
    ctx.store.new_mission("t")
    r = run_action(ctx, "rover_goto", {"x": 0.3, "y": 1.5})                  # straight "left" from home (yaw 0)
    assert r["arrived"] and abs(world.rover[1] - 1.5) < 0.2 and abs(world.rover[0] - 0.3) < 0.2


def test_unreachable_robot_is_a_robot_error_and_a_clean_tool_result(stack):
    ctx, world, r_srv, d_srv = stack
    ctx.store.new_mission("t")
    dead = RoverHttp("http://127.0.0.1:9", ctx.cfg)
    with pytest.raises(RobotError):
        dead.pose()
    ctx.rover = dead
    r = run_action(ctx, "rover_inspect", {"question": "what is this"})
    assert r["ok"] is False and r["offline"] == "rover"


def test_relative_arm_mode_sends_deltas(stack):
    ctx, *_ = stack
    sent = []
    rover = RoverHttp(ctx.rover.http.base, dataclasses.replace(ctx.cfg, rover_arm_mode="relative"))
    rover.http.json = lambda path, timeout=None, **p: sent.append((path, p)) or {"ok": True}
    rover.arm("pick")
    rover.arm("carry")
    assert sent[0] == ("/arm", {"x": 140, "y": -40}) and sent[1] == ("/arm", {"x": -80, "y": 120})


def test_full_mission_through_the_http_adapters(stack):
    ctx, world, *_ = stack
    ctrl = Controller(ctx, Planner(None, ctx.arena), LLMSupervisor(None, ctx.arena))
    ctrl.world = world
    ctrl.start()
    try:
        st = run_to_end(ctrl, "Find the object at the exit and bring it back to me", timeout=30)
    finally:
        ctrl.shutdown()
    assert st["mission"]["status"] == "done", st["mission"]["error"]
    assert st["robots"]["rover"].get("holding") is True
    assert world.drone["state"] == "docked"
