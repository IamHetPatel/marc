"""WP-M2: drone tools without docking, and the geometric separation rule that replaces 'rover waits for the drone'."""
import pytest

from manager.adapters.base import RobotError
from manager.tools import run_action


def go(ctx, name, **args):
    return run_action(ctx, name, args)


# ---------------------------------------------------------------- takeoff
def test_takeoff_hovers_at_search_height(ctx):
    r = go(ctx, "drone_takeoff")
    assert r["ok"] and r["pose"]["state"] == "flying" and r["pose"]["z"] == ctx.cfg.search_z
    assert ctx.drone_out is True


def test_takeoff_needs_the_mentor_and_a_charged_battery(ctx):
    ctx.world.drone["battery_v"] = 3.0
    assert "battery" in go(ctx, "drone_takeoff")["error"] and ctx.world.drone["state"] == "landed"
    ctx.world.drone["battery_v"] = 4.0
    ctx.store.mentor_present = False
    r = go(ctx, "drone_takeoff")
    assert r["blocked"] == "mentor" and ctx.world.drone["state"] == "landed"


# ---------------------------------------------------------------- mark
def test_mark_flies_to_mark_height_and_registers_a_finding(ctx):
    r = go(ctx, "drone_mark", target="2.4,1.1")
    assert r["ok"] and r["finding_id"] == "f1"
    assert (r["pose"]["x"], r["pose"]["y"], r["pose"]["z"]) == (2.4, 1.1, ctx.cfg.mark_z)
    (f,) = ctx.store.findings
    assert (f.id, f.x, f.y, f.by) == ("f1", 2.4, 1.1, "scout")
    assert any("f1" in m.text for m in ctx.store.messages)


def test_mark_takes_off_by_itself_and_leaves_the_drone_up(ctx):
    assert ctx.world.drone["state"] == "landed"
    assert go(ctx, "drone_mark", x=2.0, y=1.0)["ok"]
    assert ctx.world.drone["state"] == "flying" and ctx.drone_out


def test_marked_spot_can_be_referenced_by_the_rover_and_it_stops_short(ctx):
    go(ctx, "drone_mark", target="2.4,1.1")
    r = go(ctx, "rover_goto", target="$finding.f1")                   # the drone hovers at mark height: allowed
    assert r["ok"] and r["arrived"]
    gap = ((ctx.world.rover[0] - 2.4) ** 2 + (ctx.world.rover[1] - 1.1) ** 2) ** 0.5
    assert 0.4 < gap < 0.9                                            # 0.6 m standoff, not on top of the wreck


def test_marking_the_same_spot_twice_does_not_duplicate_the_finding(ctx):
    a = go(ctx, "drone_mark", target="2.4,1.1")
    b = go(ctx, "drone_mark", target="2.45,1.12")
    c = go(ctx, "drone_mark", target="$finding.f1")
    assert a["finding_id"] == b["finding_id"] == c["finding_id"] == "f1" and len(ctx.store.findings) == 1


def test_mark_refuses_spots_outside_the_cage_and_unknown_references(ctx):
    r = go(ctx, "drone_mark", x=9.0, y=1.0)
    assert r["ok"] is False and "cage" in r["error"] and ctx.world.drone["state"] == "landed"
    assert go(ctx, "drone_mark", target="$finding.f9")["unresolved"] == "finding"


# ---------------------------------------------------------------- separation rule
def hover(ctx, x, y, z):
    assert go(ctx, "drone_takeoff")["ok"]
    assert go(ctx, "drone_goto", x=x, y=y, z=z)["ok"]


def test_a_low_drone_over_the_rovers_goal_blocks_the_goto(ctx):
    hover(ctx, 2.0, 1.0, 0.6)
    r = go(ctx, "rover_goto", x=2.1, y=1.0)
    assert r["ok"] is False and r["blocked"] == "separation" and "drone_mark" in r["error"]
    assert ctx.world.rover[:2] == [ctx.arena.home.x, ctx.arena.home.y]          # nothing moved


def test_the_same_goal_is_fine_once_the_drone_is_at_mark_height(ctx):
    hover(ctx, 2.0, 1.0, ctx.cfg.mark_z)
    assert go(ctx, "rover_goto", x=2.1, y=1.0)["arrived"]


def test_a_low_drone_far_from_the_goal_does_not_block(ctx):
    hover(ctx, 3.2, 2.4, 0.6)
    assert go(ctx, "rover_goto", x=0.8, y=0.8)["arrived"]


def test_a_low_drone_hovering_next_to_the_rover_blocks_turning_in_place(ctx):
    assert go(ctx, "drone_takeoff")["ok"]                  # the pad is 0.3 m from the rover, z = search height
    r = go(ctx, "rover_search", target="crashed drone")
    assert r["ok"] is False and r["blocked"] == "separation"
    go(ctx, "drone_goto", x=3.0, y=2.0, z=0.6)             # fly away: now the rover may turn
    assert "blocked" not in go(ctx, "rover_search", target="crashed drone")


def test_a_landed_drone_never_blocks(ctx):
    assert go(ctx, "rover_goto", x=0.6, y=0.3)["arrived"]  # right on top of the launch pad


def test_the_rule_fails_open_when_the_drone_link_is_down(ctx, monkeypatch):
    def down(*a, **k):
        raise RobotError("no radio")
    monkeypatch.setattr(ctx.drone, "pose", down)
    assert go(ctx, "rover_goto", x=1.0, y=1.0)["arrived"]  # the watchdog, not this rule, owns a dead link


def test_dock_mode_keeps_the_stricter_old_rule(dock_ctx):
    assert go(dock_ctx, "drone_takeoff")["ok"]
    r = go(dock_ctx, "rover_goto", x=3.0, y=2.5)           # nowhere near the drone, still refused
    assert r["blocked"] == "drone_out"


# ---------------------------------------------------------------- planner validation without docking
def test_the_executor_checks_drone_mark_references_before_running(ctx):
    from manager.tools import check_refs
    assert check_refs(ctx, "drone_mark", {"target": "$finding.f1"}).kind == "finding"
    assert check_refs(ctx, "drone_mark", {"target": "2.4,1.1"}) is None


@pytest.mark.parametrize("cf,expected", [("scout", "landed"), ("target", "landed")])
def test_pose_for_both_crazyflies(ctx, cf, expected):
    p = ctx.drone.pose(cf=cf)
    assert p["ok"] and p["state"] == expected
