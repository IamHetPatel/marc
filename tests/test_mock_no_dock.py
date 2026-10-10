"""WP-M1: the farmland world. No docking by default: the scout starts on a launch pad beside the rover, and the
second Crazyflie (the 'crashed drone') can be read through pose(cf="target")."""
import dataclasses

from manager.adapters.mock import FLY_Z
from manager.config import Config
from manager.models import load_arena
from manager.runtime import build_ctx


def test_config_defaults_and_env(monkeypatch):
    c = Config()
    assert (c.dock_mode, c.search_z, c.mark_z, c.separation_m) == (False, 0.6, 0.4, 0.5)
    assert (c.detector_fov_deg, c.detector_noise_m, c.detector_miss_p, c.scenario) == (60.0, 0.05, 0.05, "")
    monkeypatch.setenv("DOCK_MODE", "1")
    monkeypatch.setenv("SCENARIO", "t2_mark_and_drive")
    e = Config.from_env()
    assert e.dock_mode is True and e.scenario == "t2_mark_and_drive"


def test_farmland_arena():
    a = load_arena()
    assert {"field_a", "field_b", "base", "disposal", "test_zone"} <= set(a.zones)
    assert a.zones["base"].contains(a.home.x, a.home.y)
    assert a.zones["test_zone"] == a.drone_cage                       # the whole cage
    (obj,) = a.mock_objects
    assert obj.kind == "hostile_drone" and a.zones["field_a"].contains(obj.x, obj.y)
    assert a.calibrated is False


def test_the_scout_starts_landed_on_a_pad_beside_the_rover(ctx):
    d = ctx.drone.pose()
    assert d["state"] == "landed" and d["z"] == 0.0 and d["ok"] is True
    assert (d["x"], d["y"]) == (ctx.arena.home.x + 0.3, ctx.arena.home.y)


def test_the_scout_takes_off_from_the_floor_and_lands_again(ctx):
    assert ctx.drone.takeoff(0.8)["ok"]
    assert ctx.drone.pose()["state"] == "flying" and ctx.drone.pose()["z"] == 0.8
    assert ctx.drone.takeoff()["ok"]                                      # already flying: harmless
    assert ctx.drone.land()["ok"] and ctx.drone.pose()["state"] == "landed"
    ctx.drone.takeoff()
    assert ctx.drone.pose()["z"] == FLY_Z


def test_dock_without_a_plate_returns_to_the_launch_pad(ctx):
    r = ctx.drone.dock()
    assert r["ok"] is False and "no home recorded" in r["error"]           # never flew: nowhere to return to
    pad = ctx.drone.pose()
    assert pad["home"] is None
    ctx.drone.takeoff()
    assert ctx.drone.pose()["home"] == {"x": pad["x"], "y": pad["y"]}      # like drone_server.py: first takeoff spot
    ctx.drone.goto(2.0, 1.5, 0.6)
    r = ctx.drone.dock()
    p = ctx.drone.pose()
    assert r["ok"] and r["docked"] and p["state"] == "landed" and (p["x"], p["y"], p["z"]) == (pad["x"], pad["y"], 0.0)


def test_the_target_pose_is_readable_like_the_second_crazyflie(ctx):
    t = ctx.drone.pose(cf="target")
    assert t == {"ok": True, "x": 2.4, "y": 1.1, "z": 0.0, "state": "landed"}
    ctx.world.objects[0]["x"] = 2.0                                       # someone moves the wreck by hand
    assert ctx.drone.pose(cf="target")["x"] == 2.0


def test_no_target_means_a_clear_error(ctx):
    ctx.world.objects.clear()
    assert ctx.drone.pose(cf="target") == {"ok": False, "error": "no target drone"}


def test_a_carried_target_moves_with_the_rover(ctx):
    obj = ctx.world.objects[0]
    ctx.world.held = obj
    ctx.world.move_body(0.5, 0.0, 0.0)
    t = ctx.drone.pose(cf="target")
    assert (t["x"], t["y"]) == (round(obj["x"], 3), round(obj["y"], 3)) and t["x"] != 2.4


def test_no_drone_fix_without_docking_and_with_it(cfg):
    plain, _ = build_ctx(cfg)
    docked, _ = build_ctx(dataclasses.replace(cfg, dock_mode=True))
    assert plain.loc.use_drone_fix is False and docked.loc.use_drone_fix is True
    assert docked.drone.pose()["state"] == "docked"                       # DOCK_MODE=1 keeps the old behaviour
