"""Whole missions on the mock world. Deterministic (seeded), free, run on every change."""

from manager.models import PlanDraft

from .conftest import decision, draft, run_to_end, scripted, step_status


def test_inspect_mission_end_to_end(make_ctrl):
    c = make_ctrl()
    st = run_to_end(c, "Check the test zone for anything blocking the exit")
    assert st["mission"]["status"] == "done", st["mission"]["error"]
    assert [s["status"] for s in st["plan"]] == ["done"] * len(st["plan"])
    f = st["findings"][0]
    assert f["label"] == "crashed drone" and f["confidence"] and f["image_id"]
    assert c.store.get_image(f["image_id"])[:2] == b"\xff\xd8"           # a real JPEG for the dashboard
    assert st["messages"][-1]["text"] == "Mission complete."
    assert st["robots"]["drone"] is not None


def test_fetch_mission_delivers_the_object_to_the_disposal_zone(make_ctrl):
    c = make_ctrl()
    st = run_to_end(c, "A hostile drone crashed in field A. Find it and move it to the disposal zone.")
    assert st["mission"]["status"] == "done", st["mission"]["error"]
    wreck = next(o for o in c.world.objects if o["label"] == "crashed drone")
    assert c.base_ctx.arena.zones["disposal"].contains(wreck["x"], wreck["y"])
    assert st["robots"]["rover"]["holding"] is False and c.world.held is None          # delivered and released
    assert c.world.drone["state"] == "landed"


def test_state_shape_matches_the_dashboard_contract(make_ctrl):
    c = make_ctrl()
    st = run_to_end(c, "Check the exit")
    assert {"mission", "plan", "current_step", "robots", "findings", "messages", "last_image_id",
            "mentor_present", "metrics"} <= set(st)
    assert {"id", "text", "status", "plan_version", "fallback"} <= set(st["mission"])
    step = st["plan"][0]
    assert {"id", "robot", "action", "args", "why", "status", "result"} <= set(step)
    assert step["args"] == {"target": "$zone.field_a"}                       # references stay unresolved in /state
    assert {"id", "x", "y", "label", "confidence", "image_id"} <= set(st["findings"][0])
    assert {"t", "role", "text"} == set(st["messages"][0])


def test_nothing_blocking_means_no_rover_trip(make_ctrl):
    c = make_ctrl()
    c.world.objects.clear()
    c.world.rng.random = lambda: 0.99
    st = run_to_end(c, "Check field A", answers=("no",))                       # nothing found: "search field B?" -> no
    assert st["mission"]["status"] == "done"
    assert step_status(st, "rover_goto") == ["skipped"]


def test_an_object_that_cannot_be_grabbed_is_only_reported(make_ctrl):
    c = make_ctrl()
    for o in c.world.objects:
        o["grippable"] = False
    st = run_to_end(c, "Bring me whatever is at the exit")
    assert st["mission"]["status"] == "done", st["mission"]["error"]
    assert "rover_approach" not in [s["action"] for s in st["plan"] if s["status"] != "skipped"]
    assert any("cannot be grabbed" in m["text"] for m in st["messages"])
    assert c.world.held is None


def test_flaky_robots_still_complete_via_replanning(make_ctrl):
    """goto fails 40% of the time; the rule supervisor retries (within budget) and the mission still lands."""
    outcomes = []
    for seed in range(6):
        c = make_ctrl(mock_fail={"goto": 0.4}, mock_seed=seed)
        outcomes.append(run_to_end(c, "Check the exit")["mission"]["status"])
        c.shutdown()
    assert outcomes.count("done") >= 4, outcomes


def test_llm_authored_plan_runs_like_the_canned_one(make_ctrl):
    plan = PlanDraft(summary="Scan, check, report", steps=[
        draft("drone_scan", target="$zone.field_a"), draft("drone_land"),
        draft("rover_goto", target="$finding.f1"), draft("rover_search", target="crashed drone"),
        draft("rover_inspect", question="Is this blocking the exit?"),
        draft("report", text="The crashed drone is in field A.", finding_id="f1")])
    c = make_ctrl(llm=scripted(plan, decision("continue", "Box found."), decision("continue", "Confirmed.")))
    st = run_to_end(c, "Check the exit")
    assert st["mission"]["status"] == "done" and st["mission"]["fallback"] is False
    assert st["mission"]["reason"] == "Confirmed."                        # the latest supervisor one-liner
    assert any(m["text"] == "The crashed drone is in field A." for m in st["messages"])


def test_mission_timeout_fails_cleanly_and_lands_the_drone(make_ctrl):
    c = make_ctrl(mission_timeout_s=3.0)                                  # a scan alone takes ~10 simulated seconds
    st = run_to_end(c, "Check the exit")
    assert st["mission"]["status"] == "failed" and "timeout" in st["mission"]["error"].lower()
    assert c.world.drone["state"] in ("landed", "docked")


def test_findings_are_deduplicated_across_scans(make_ctrl):
    c = make_ctrl()
    ctx = c.base_ctx
    a = ctx.store.add_findings([{"x": 3.0, "y": 0.5, "dz": 0.2}])
    b = ctx.store.add_findings([{"x": 3.1, "y": 0.55, "dz": 0.25}])
    assert [f.id for f in a] == [f.id for f in b] == ["f1"] and len(ctx.store.findings) == 1
