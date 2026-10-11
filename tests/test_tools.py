"""Tool-level behaviour: validation, safety gates, the drone/rover separation rule, localization."""
import dataclasses

import pytest

from manager.adapters.base import RobotError
from manager.runtime import build_ctx
from manager.tools import (REGISTRY, UnresolvedRef, check_refs, resolve_location, run_action, succeeded,
                           validate_step, vocabulary_text)


def go(ctx, name, **args):
    return run_action(ctx, name, args)


def scan_and_recover(ctx):
    """Scan, then bring the drone down the way the active mode does it."""
    assert go(ctx, "drone_scan", target="$zone.field_a")["ok"]
    assert go(ctx, "drone_dock" if ctx.cfg.dock_mode else "drone_land")["ok"]


# ---------------------------------------------------------------- validation
def test_every_action_has_a_docstring_for_the_prompt():
    assert {"drone_scan", "drone_goto", "drone_dock", "drone_land", "drone_takeoff", "drone_mark", "rover_goto",
            "rover_search", "rover_approach", "rover_inspect", "rover_grip", "rover_check_grip", "rover_arm",
            "report", "ask_human"} <= set(REGISTRY)
    for spec in REGISTRY.values():
        assert spec.doc, f"{spec.name} needs a docstring: it is the planner prompt"
    assert "drone_scan" in vocabulary_text()


@pytest.mark.parametrize("name,args,fragment", [
    ("nope", {}, "unknown action"),
    ("rover_grip", {}, "needs mode"),
    ("rover_grip", {"mode": "squeeze"}, "must be one of"),
    ("rover_goto", {}, "needs target or x+y"),
    ("drone_goto", {"x": 1, "y": 1}, "needs x+y+z"),
    ("drone_scan", {"x0": 0, "y0": 0}, "needs"),
])
def test_bad_args_are_returned_not_raised(ctx, name, args, fragment):
    r = run_action(ctx, name, args)
    assert r["ok"] is False and fragment in r["error"]


def test_validate_step_checks_robot_ownership():
    assert validate_step("rover_goto", "drone", {"target": "$home"}) == "rover_goto belongs to robot 'rover', not 'drone'"
    assert validate_step("rover_goto", "rover", {"target": "$home"}) is None


# ---------------------------------------------------------------- references
def test_resolve_location_forms(ctx):
    ctx.store.add_findings([{"x": 3.0, "y": 0.5, "dz": 0.2}])
    assert resolve_location(ctx, "$finding.f1")["kind"] == "finding"
    assert resolve_location(ctx, "f1")["x"] == 3.0
    z = resolve_location(ctx, "$zone.field_a")
    assert z["kind"] == "zone" and z["rect"] is not None
    assert resolve_location(ctx, "field_a")["kind"] == "zone"
    assert resolve_location(ctx, "1.5, 2")["x"] == 1.5
    assert resolve_location(ctx, "$home")["kind"] == "home"
    with pytest.raises(UnresolvedRef) as e:
        resolve_location(ctx, "$finding.f9")
    assert e.value.kind == "finding"
    with pytest.raises(UnresolvedRef):
        resolve_location(ctx, "$zone.nowhere")


def test_check_refs_flags_findings_that_do_not_exist_yet(ctx):
    assert check_refs(ctx, "rover_goto", {"target": "$finding.f1"}).kind == "finding"
    assert check_refs(ctx, "rover_search", {"target": "the box"}) is None      # descriptions are never references
    r = go(ctx, "rover_goto", target="$finding.f1")
    assert r["ok"] is False and r["unresolved"] == "finding"


# ---------------------------------------------------------------- safety gates
def test_mentor_gate_locks_drone_tools(ctx):
    ctx.store.mentor_present = False
    r = go(ctx, "drone_scan", target="$zone.field_a")
    assert r["ok"] is False and r["blocked"] == "mentor"
    assert go(ctx, "drone_land")["ok"]               # landing is never gated


def test_drone_geofence(ctx):
    r = go(ctx, "drone_goto", x=9.0, y=1.0, z=0.5)
    assert r["ok"] is False and "outside the drone cage" in r["error"]
    r = go(ctx, "drone_goto", x=1.0, y=1.0, z=5.0)
    assert r["ok"] is False and "z=" in r["error"]
    r = go(ctx, "drone_scan", x0=-5, y0=0, x1=1, y1=1)
    assert r["ok"] is False and not ctx.drone_out


def test_zone_scan_is_clipped_to_the_cage(ctx):
    r = go(ctx, "drone_scan", target="$zone.disposal")         # sticks out of the cage on two sides
    assert r["ok"] and r["clipped"] is True
    cage = ctx.arena.drone_cage
    assert r["area"][2] <= cage.x1 and r["area"][1] >= cage.y0


def test_rover_geofence(ctx):
    r = go(ctx, "rover_goto", x=9.0, y=9.0)
    assert r["ok"] is False and "off the map" in r["error"]


def test_low_battery_blocks_scan(ctx):
    ctx.world.drone["battery_v"] = 3.0
    r = go(ctx, "drone_scan", target="$zone.field_a")
    assert r["ok"] is False and "battery" in r["error"]


def test_dock_mode_rover_cannot_move_while_the_drone_is_out(dock_ctx):
    ctx = dock_ctx
    assert go(ctx, "drone_scan", target="$zone.field_a")["ok"]
    assert ctx.drone_out
    for name, args in (("rover_goto", {"target": "$home"}), ("rover_search", {"target": "box"}),
                       ("rover_approach", {"target": "box"})):
        r = go(ctx, name, **args)
        assert r["ok"] is False and r["blocked"] == "drone_out", name
    assert go(ctx, "drone_dock")["ok"] and not ctx.drone_out
    assert go(ctx, "rover_goto", target="$home")["ok"]


def test_dock_fails_if_the_rover_moved_while_the_drone_was_up(dock_ctx):
    ctx = dock_ctx
    assert go(ctx, "drone_scan", target="$zone.field_a")["ok"]
    ctx.world.move_body(0.5, 0, 0)                       # something pushed the rover
    r = go(ctx, "drone_dock")
    assert r["ok"] is False and "moved" in r["error"] and ctx.drone_out
    assert go(ctx, "drone_land")["ok"] and not ctx.drone_out


# ---------------------------------------------------------------- robustness
def test_tools_never_raise_when_a_robot_is_unreachable(ctx, monkeypatch):
    def boom(*a, **k):
        raise RobotError("connection refused")
    monkeypatch.setattr(ctx.rover, "snapshot", boom)
    r = go(ctx, "rover_inspect", question="what is this")
    assert r["ok"] is False and r["offline"] == "rover"


def test_tool_bugs_become_error_results(ctx, monkeypatch):
    monkeypatch.setattr(ctx.perception, "inspect", lambda *a: 1 / 0)
    r = go(ctx, "rover_inspect", question="x")
    assert r["ok"] is False and "ZeroDivisionError" in r["error"]


def test_fault_injection(cfg):
    c, _ = build_ctx(dataclasses.replace(cfg, mock_fail={"goto": 1.0, "grip": 1.0}))
    c.store.new_mission("t")
    r = go(c, "rover_goto", target="$home")
    assert r["ok"] is False and r["injected"] is True
    assert go(c, "rover_grip", mode="open")["injected"] is True


def test_cancel_stops_a_goto_mid_drive(ctx):
    ctx.cancel.set()
    assert go(ctx, "rover_goto", target="$home") == {"ok": False, "error": "cancelled"}


# ---------------------------------------------------------------- the demo sequence
def test_full_pick_and_return_sequence(ctx):
    scan_and_recover(ctx)
    assert go(ctx, "rover_goto", target="$finding.f1")["arrived"]
    assert go(ctx, "rover_search", target="crashed drone")["found"]
    insp = go(ctx, "rover_inspect", question="what is this")
    assert insp["label"] == "crashed drone" and ctx.store.findings[0].label == "crashed drone"
    assert ctx.store.findings[0].image_id == insp["image_id"]
    assert go(ctx, "rover_arm", mode="pick")["ok"] and go(ctx, "rover_grip", mode="open")["ok"]
    approach = go(ctx, "rover_approach", target="crashed drone")
    assert approach["in_reach"] and approach["steps"] >= 2
    assert go(ctx, "rover_grip", mode="close")["blocked"] == "authorization"     # hostile: the gate says no
    ctx.ask = lambda question: "yes"
    assert go(ctx, "request_authorization", finding_id="f1")["authorized"] is True
    assert go(ctx, "rover_grip", mode="close")["ok"] and go(ctx, "rover_arm", mode="carry")["ok"]
    assert go(ctx, "rover_check_grip")["holding"] is True
    assert go(ctx, "rover_goto", target="$home")["arrived"]
    box = next(o for o in ctx.world.objects if o["label"] == "crashed drone")
    assert abs(box["x"] - ctx.arena.home.x) < 0.6 and abs(box["y"] - ctx.arena.home.y) < 0.6


def test_search_reports_not_found_for_something_that_is_not_there(ctx):
    r = go(ctx, "rover_search", target="banana")
    assert r["ok"] is False and r["found"] is False
    assert not succeeded("rover_search", r)


def test_check_grip_is_a_failure_when_the_gripper_is_empty(ctx):
    r = go(ctx, "rover_check_grip")
    assert r["holding"] is False and not succeeded("rover_check_grip", r)


def test_goto_stops_short_of_a_finding(ctx):
    ctx.store.add_findings([{"x": 3.0, "y": 1.5, "dz": 0.2}])
    r = go(ctx, "rover_goto", target="$finding.f1")
    assert r["arrived"]
    d = ((ctx.world.rover[0] - 3.0) ** 2 + (ctx.world.rover[1] - 1.5) ** 2) ** 0.5
    assert 0.4 < d < 0.9


# ---------------------------------------------------------------- localization
def test_pose_source_follows_the_drone_in_dock_mode(dock_ctx):
    ctx = dock_ctx
    p = ctx.loc.rover_pose()
    assert p.source == "docked_drone" and abs(p.x - ctx.arena.home.x) < 0.05
    assert go(ctx, "drone_scan", target="$zone.field_a")["ok"]
    ctx.clock.sleep(1.0)
    assert ctx.loc.rover_pose().source == "fix+odometry"


def test_odometry_carries_the_last_fix_forward(dock_ctx):
    ctx = dock_ctx
    ctx.loc.rover_pose()                                  # docked fix at home
    ctx.world.drone["state"] = "landed"                    # the drone is no longer on the plate
    ctx.clock.sleep(1.0)
    ctx.rover.drive(1.5, 0.0, 0.0)
    ctx.clock.sleep(1.0)
    p = ctx.loc.rover_pose()
    true_x, true_y, _ = ctx.world.rover
    assert p.source == "fix+odometry"
    assert abs(p.x - true_x) < 0.15 and abs(p.y - true_y) < 0.15      # drift stays small over 1.5 m


def test_without_docking_the_pose_comes_from_the_start_cross_and_odometry(ctx):
    p = ctx.loc.rover_pose()
    assert p.source == "start_cross+odometry" and (p.x, p.y) == (ctx.arena.home.x, ctx.arena.home.y)


def test_real_rover_does_not_trust_a_mock_drone_pose(cfg):
    """With a real rover and a mock drone, the drone's 'docked' pose says nothing about the rover."""
    from manager.adapters.mock import build_mock
    from manager.localization import Localization
    from manager.models import load_arena
    arena = load_arena()
    _, rover, drone, clock, _ = build_mock(arena, cfg)
    loc = Localization(rover, drone, arena, clock, use_drone_fix=False)
    assert loc.rover_pose().source == "start_cross+odometry"
