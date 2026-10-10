"""WP-M7: the simulated downward camera, the lawnmower it flies, and drone_search."""
import math
import random

import pytest

from manager.adapters.base import RobotError
from manager.detector import SimDetector, Target, search_waypoints
from manager.models import Rect
from manager.tools import run_action


def go(ctx, name, **args):
    return run_action(ctx, name, args)


def det(drone=None, **kw):
    return SimDetector(drone, rng=random.Random(1), **{"noise_m": 0.05, "miss_p": 0.0, **kw})


# ---------------------------------------------------------------- the search pattern
@pytest.mark.parametrize("rect", [Rect(x0=1.2, y0=0.3, x1=3.7, y1=1.6), Rect(x0=0, y0=0, x1=1, y1=3),
                                  Rect(x0=0.3, y0=0.3, x1=3.7, y1=2.7)])
def test_every_point_of_the_area_is_inside_some_footprint(rect):
    r = 0.346
    path = search_waypoints(rect, r)
    for i in range(41):
        for j in range(41):
            px, py = rect.x0 + (rect.x1 - rect.x0) * i / 40, rect.y0 + (rect.y1 - rect.y0) * j / 40
            assert min(math.hypot(px - x, py - y) for x, y in path) <= r + 1e-9, (px, py)


def test_waypoints_stay_inside_the_area_and_alternate_direction():
    rect = Rect(x0=1.2, y0=0.3, x1=3.7, y1=1.6)
    path = search_waypoints(rect, 0.346)
    assert all(rect.contains(x, y) for x, y in path)
    lanes = {}
    for x, y in path:
        lanes.setdefault(round(y, 6), []).append(x)
    first, second = list(lanes.values())[:2]
    assert first == sorted(first) and second == sorted(second, reverse=True)
    assert all(abs(b - a) <= 0.346 + 1e-9 for lane in lanes.values() for a, b in zip(lane, lane[1:], strict=False))


def test_a_thin_area_gets_one_lane_and_a_tiny_one_a_single_waypoint():
    thin = search_waypoints(Rect(x0=0, y0=0, x1=3, y1=0.4), 0.346)
    assert len({round(y, 6) for _, y in thin}) == 1 and all(y == pytest.approx(0.2) for _, y in thin)
    assert search_waypoints(Rect(x0=1, y0=1, x1=1.2, y1=1.2), 0.346) == [(1.1, 1.1)]


# ---------------------------------------------------------------- the detector
def test_footprint_grows_with_height():
    d = det()
    assert d.footprint_radius(0.6) == pytest.approx(0.6 * math.tan(math.radians(30)))
    assert d.footprint_radius(0.6) == pytest.approx(0.346, abs=1e-3) and d.footprint_radius(1.2) > d.footprint_radius(0.6)


def test_a_target_inside_the_footprint_is_seen_with_noise_and_outside_is_not():
    d = det()
    d.set_targets([Target(id="t1", source="fixed", x=2.0, y=1.0)])
    hits = d.observe({"x": 2.2, "y": 1.0, "z": 0.6})
    assert len(hits) == 1 and hits[0]["target_id"] == "t1"
    assert math.hypot(hits[0]["x"] - 2.0, hits[0]["y"] - 1.0) == pytest.approx(hits[0]["error_m"], abs=2e-3)
    assert hits[0]["error_m"] < 0.25 and (hits[0]["x"], hits[0]["y"]) != (2.0, 1.0)
    assert d.observe({"x": 2.5, "y": 1.0, "z": 0.6}) == []                  # 0.5 m away, footprint is 0.35 m
    assert d.observe({"x": 2.5, "y": 1.0, "z": 1.2})                        # same spot, higher: it fits now
    assert d.observe({"x": 2.0, "y": 1.0, "z": 0.0}) and not d.observe({"x": 2.2, "y": 1.0, "z": 0.0})


def test_the_miss_probability():
    d = det(miss_p=0.5)
    d.set_targets([Target(id="t1", source="fixed", x=1.0, y=1.0)])
    seen = sum(bool(d.observe({"x": 1.0, "y": 1.0, "z": 0.6})) for _ in range(400))
    assert 140 < seen < 260
    d.miss_p = 1.0
    assert d.observe({"x": 1.0, "y": 1.0, "z": 0.6}) == []


def test_live_targets_follow_the_second_crazyflie(ctx):
    d = ctx.detector
    assert d.truth() == [{"id": "t1", "x": 2.4, "y": 1.1}]
    ctx.world.objects[0]["x"] = 2.0
    assert d.truth()[0]["x"] == 2.0
    ctx.world.objects.clear()
    assert d.truth() == []                                                   # no target drone: nothing to see


def test_an_unreachable_target_link_just_means_nothing_is_seen(ctx, monkeypatch):
    def down(cf="scout"):
        raise RobotError("radio")
    monkeypatch.setattr(ctx.drone, "pose", down)
    assert ctx.detector.truth() == []


def test_fixed_and_live_targets_mix(ctx):
    ctx.detector.set_targets([Target(id="t1", kind="decoy", source="fixed", x=3.0, y=2.0),
                              Target(id="t2", source="live")])
    assert {t["id"]: (t["x"], t["y"]) for t in ctx.detector.truth()} == {"t1": (3.0, 2.0), "t2": (2.4, 1.1)}


# ---------------------------------------------------------------- drone_search
def test_search_finds_the_target_within_the_noise_and_stops_early(ctx):
    r = go(ctx, "drone_search", target="$zone.field_a")
    assert r["ok"] and len(r["findings"]) == 1
    f = r["findings"][0]
    assert f["id"] == "f1" and f["by"] == "scout" and math.hypot(f["x"] - 2.4, f["y"] - 1.1) < 0.25
    assert 0 < r["waypoints_flown"] < len(r["search_path"])                 # first detection ends the search
    assert ctx.store.metrics["detection_error_m"] < 0.25
    assert ctx.drone_out and ctx.world.drone["state"] == "flying" and ctx.world.drone["z"] == ctx.cfg.search_z


def test_search_really_flies_the_pattern(ctx):
    r = go(ctx, "drone_search", target="$zone.field_a")
    path = r["search_path"]
    assert path == [list(p) for p in search_waypoints(Rect(x0=1.2, y0=0.3, x1=3.7, y1=1.6),
                                                      ctx.detector.footprint_radius(ctx.cfg.search_z))]
    last = path[r["waypoints_flown"] - 1]
    assert (ctx.world.drone["x"], ctx.world.drone["y"]) == tuple(last)       # it is hovering where it last looked
    assert ctx.world.t > r["waypoints_flown"] * 0.3                          # and that took simulated flight time


def test_search_mode_all_flies_the_whole_pattern(ctx):
    r = go(ctx, "drone_search", target="$zone.field_a", mode="all")
    assert r["waypoints_flown"] == len(r["search_path"]) and len(r["findings"]) >= 1


def test_nothing_in_the_area_is_a_normal_result(ctx):
    r = go(ctx, "drone_search", target="$zone.field_b")                      # the wreck is in field A
    assert r["ok"] is True and r["findings"] == [] and r["waypoints_flown"] == len(r["search_path"])
    assert ctx.store.findings == [] and any("found nothing" in m.text for m in ctx.store.messages)


def test_a_detector_that_always_misses_finds_nothing(ctx):
    ctx.detector.miss_p = 1.0
    r = go(ctx, "drone_search", target="$zone.field_a")
    assert r["ok"] and r["findings"] == [] and r["waypoints_flown"] == len(r["search_path"])


def test_the_search_target_can_be_moved_by_hand(ctx):
    ctx.world.objects[0].update(x=1.6, y=0.5)                                # now near the first lane
    r = go(ctx, "drone_search", target="$zone.field_a")
    f = r["findings"][0]
    assert math.hypot(f["x"] - 1.6, f["y"] - 0.5) < 0.25 and r["waypoints_flown"] <= 3


def test_search_with_explicit_coordinates_and_a_zone_clipped_to_the_cage(ctx):
    r = go(ctx, "drone_search", x0=1.2, y0=0.3, x1=3.7, y1=1.6)
    assert r["ok"] and r["clipped"] is False
    ctx.world.drone.update(state="landed", z=0.0)
    r = go(ctx, "drone_search", target="$zone.disposal")
    assert r["ok"] and r["clipped"] is True and r["findings"] == []


def test_search_obeys_the_gates(ctx):
    assert go(ctx, "drone_search", x0=-3, y0=0, x1=1, y1=1)["ok"] is False
    assert go(ctx, "drone_search", target="$zone.field_a", mode="sometimes")["ok"] is False
    ctx.store.mentor_present = False
    assert go(ctx, "drone_search", target="$zone.field_a")["blocked"] == "mentor"
    assert ctx.world.drone["state"] == "landed"
    ctx.store.mentor_present = True
    ctx.world.drone["battery_v"] = 3.0
    assert "battery" in go(ctx, "drone_search", target="$zone.field_a")["error"]


def test_cancelling_mid_search_stops_at_the_next_waypoint(ctx):
    real, calls = ctx.drone.goto, []

    def goto(x, y, z):
        calls.append((x, y))
        if len(calls) == 2:
            ctx.cancel.set()
        return real(x, y, z)

    ctx.drone.goto = goto
    r = go(ctx, "drone_search", target="$zone.field_b", mode="all")
    assert r["ok"] is False and r["error"] == "cancelled" and r["waypoints_flown"] == 2 and len(calls) == 2


def test_a_dying_battery_aborts_the_search_and_keeps_what_was_found(ctx):
    real, n = ctx.drone.goto, []

    def goto(x, y, z):
        n.append(1)
        if len(n) == 3:
            ctx.world.drone["battery_v"] = 3.0
        return real(x, y, z)

    ctx.drone.goto = goto
    r = go(ctx, "drone_search", target="$zone.field_b", mode="all")
    assert r["ok"] is False and r["blocked"] == "battery" and r["waypoints_flown"] == 3


def test_a_found_spot_can_be_marked_and_driven_to(ctx):
    r = go(ctx, "drone_search", target="$zone.field_a")
    fid = r["findings"][0]["id"]
    assert go(ctx, "drone_mark", target=f"$finding.{fid}")["pose"]["z"] == ctx.cfg.mark_z
    assert go(ctx, "rover_goto", target=f"$finding.{fid}")["arrived"]
    assert len(ctx.store.findings) == 1                                       # marking did not duplicate it
