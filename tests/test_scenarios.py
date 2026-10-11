"""WP-M5: submitted plans and scenario files. Every plan source ends in the same approve-and-run path."""
import io

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from manager import cli
from manager.controller import Conflict, InvalidPlan
from manager.models import StepDraft
from manager.scenarios import UnknownScenario, list_scenarios, load_scenario
from manager.server import create_app

from .conftest import draft, finish, wait_for, wait_status

SCENARIOS = [s["name"] for s in list_scenarios()]


def answer_questions(ctrl, answers=("yes",), timeout=30.0):
    """Approve the current plan and wait for the end, answering any question the mission asks."""
    ctrl.approve(ctrl.store.mission.plan_version)
    return finish(ctrl, answers, timeout)


# ---------------------------------------------------------------- submit_plan
def test_a_submitted_plan_waits_for_approval_and_then_runs(make_ctrl):
    c = make_ctrl()
    plan = [draft("drone_mark", target="2.4,1.1"), draft("rover_goto", target="$finding.f1"), draft("drone_land"),
            draft("report", text="done")]
    mission_id = c.submit_plan("Mark and drive", plan)
    st = c.store.to_json()
    assert mission_id == "m1" and st["mission"]["status"] == "awaiting_approval"
    assert st["mission"]["source"] == "manual" and st["mission"]["plan_version"] == 1 and st["mission"]["fallback"] is False
    assert st["mission"]["text"] == "Mark and drive" and [s["status"] for s in st["plan"]] == ["pending"] * 4
    assert c.world.t == 0.0                                            # nothing moves before Approve
    st = answer_questions(c)
    assert st["mission"]["status"] == "done", st["mission"]["error"]


def test_an_invalid_plan_is_rejected_before_it_becomes_a_mission(make_ctrl):
    c = make_ctrl()
    bad = [draft("rover_grip", mode="squeeze"), StepDraft(robot="rover", action="teleport")]
    with pytest.raises(InvalidPlan) as e:
        c.submit_plan("x", bad)
    assert len(e.value.problems) == 2 and "must be one of" in str(e.value) and "unknown action" in str(e.value)
    assert c.store.status == "idle" and c.store.mission.id is None
    with pytest.raises(InvalidPlan):
        c.submit_plan("x", [])


def test_submitting_while_a_mission_is_active_is_a_conflict(make_ctrl):
    c = make_ctrl()
    c.submit_plan("a", [draft("report", text="a")])
    with pytest.raises(Conflict):
        c.submit_plan("b", [draft("report", text="b")])
    c.stop()
    c.submit_plan("b", [draft("report", text="b")])                    # after a stop it is fine again
    assert c.store.mission.text == "b"


def test_ai_plans_record_their_source(make_ctrl):
    c = make_ctrl()
    c.start_mission("Check field A")
    wait_status(c, "awaiting_approval")
    assert c.store.mission.source == "fallback" and c.store.mission.fallback is True


def test_auto_approve_applies_to_submitted_plans(make_ctrl):
    c = make_ctrl(auto_approve=True)
    c.submit_plan("x", [draft("report", text="hi")])
    wait_status(c, "done")


# ---------------------------------------------------------------- scenario files
def test_the_shipped_scenarios_exist():
    assert {"t2_mark_and_drive", "t4_search_field_a"} <= set(SCENARIOS)
    assert all(s["title"] and isinstance(s["has_plan"], bool) for s in list_scenarios())


def test_a_scenario_id_is_its_file_name_and_names_cannot_escape_the_directory():
    assert load_scenario("t2_mark_and_drive").name == "t2_mark_and_drive"
    for bad in ("nope", "../README", "..", "a/b", "t2_mark_and_drive.yaml", ""):
        with pytest.raises(UnknownScenario):
            load_scenario(bad)


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_shipped_scenario_is_valid(name, make_ctrl):
    from manager.planner import validate_drafts
    sc = load_scenario(name)
    assert sc.targets, "a scenario says what is in the field"
    if sc.plan:
        assert validate_drafts(sc.plan) == []
    else:
        assert sc.mission


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_shipped_scenario_runs_to_the_end_on_mocks(name, make_ctrl, monkeypatch):
    sc = load_scenario(name)
    if sc.arena == "floor":                                         # its coordinates are on the measured floor
        monkeypatch.setenv("ARENA", str(Path(__file__).resolve().parent.parent / "manager" / "arena.floor.json"))
    needs_overhead = any(d.action == "camera_locate" for d in sc.plan)
    c = make_ctrl(overhead_cam="mock" if needs_overhead else "")
    c.load_scenario(name)
    assert c.store.mission.source == "scenario" or not load_scenario(name).plan
    wait_status(c, "awaiting_approval")
    st = answer_questions(c)
    assert st["mission"]["status"] == "done", (name, st["mission"]["error"])


def test_loading_a_scenario_replaces_the_world_and_the_detector_targets(make_ctrl, tmp_path):
    c = make_ctrl()
    c.world.held = c.world.objects[0]
    c.load_scenario("t2_mark_and_drive")
    assert [o["label"] for o in c.world.objects] == ["crashed drone"] and c.world.held is None
    assert [(t.id, t.source) for t in c.base_ctx.detector.targets] == [("t1", "live")]
    assert c.base_ctx.drone.pose(cf="target")["ok"]


def test_scenario_t2_ends_with_a_photo_of_the_object(make_ctrl):
    c = make_ctrl()
    c.load_scenario("t2_mark_and_drive")
    st = answer_questions(c)
    assert st["mission"]["status"] == "done"
    assert st["findings"][0]["id"] == "f1" and st["findings"][0]["image_id"]
    assert [m["text"] for m in st["messages"]][-2:] == ["Crash site reached and photographed.", "Mission complete."]


def test_scenario_t4_really_searches(make_ctrl):
    c = make_ctrl()
    c.load_scenario("t4_search_field_a")
    st = answer_questions(c)
    assert st["mission"]["status"] == "done", st["mission"]["error"]
    search = next(s for s in st["plan"] if s["action"] == "drone_search")
    assert search["result"]["waypoints_flown"] > 1 and st["metrics"]["detection_error_m"] < 0.3


def test_loading_during_a_mission_is_a_conflict(make_ctrl):
    c = make_ctrl()
    c.load_scenario("t2_mark_and_drive")
    objects = list(c.world.objects)
    with pytest.raises(Conflict):
        c.load_scenario("t4_search_field_a")
    assert c.world.objects == objects                                  # and it did not touch the world


# ---------------------------------------------------------------- HTTP
@pytest.fixture
def client(make_ctrl):
    ctrl = make_ctrl()
    with TestClient(create_app(ctrl)) as c:
        c.ctrl = ctrl
        yield c


def test_post_plan(client):
    body = {"title": "Marked drive", "steps": [{"robot": "drone", "action": "drone_mark", "target": "2.4,1.1"},
                                                {"robot": "manager", "action": "report", "text": "ok"}]}
    r = client.post("/plan", json=body)
    assert r.status_code == 200 and r.json() == {"ok": True, "mission_id": "m1"}
    st = client.get("/state").json()
    assert st["mission"]["source"] == "manual" and st["mission"]["status"] == "awaiting_approval"
    assert client.post("/approve", json={"plan_version": 1}).status_code == 200
    wait_status(client.ctrl, "done", "failed")


def test_post_plan_validation_and_conflicts(client):
    r = client.post("/plan", json={"title": "x", "steps": [{"robot": "rover", "action": "rover_grip", "mode": "squeeze"}]})
    assert r.status_code == 422 and "must be one of" in r.json()["detail"]
    assert client.post("/plan", json={"title": "x", "steps": []}).status_code == 422
    assert client.post("/plan", json={"title": "x"}).status_code == 422
    ok = {"title": "x", "steps": [{"robot": "manager", "action": "report", "text": "a"}]}
    assert client.post("/plan", json=ok).status_code == 200
    assert client.post("/plan", json=ok).status_code == 409


def test_scenario_endpoints(client):
    listing = client.get("/scenarios").json()
    assert {"name": "t2_mark_and_drive", "title": "Scout marks the crash site, rover drives there",
            "has_plan": True, "arena": "mock"} in listing
    assert not any(s["name"].startswith("floor_") for s in listing)                 # this server runs the mock field
    assert client.post("/scenarios/nope/load").status_code == 404
    assert client.post("/scenarios/..%2FREADME/load").status_code == 404
    r = client.post("/scenarios/t2_mark_and_drive/load")
    assert r.status_code == 200 and r.json()["mission_id"] == "m1"
    st = client.get("/state").json()
    assert st["mission"]["source"] == "scenario" and st["plan"][0]["action"] == "drone_mark"
    assert client.post("/scenarios/t4_search_field_a/load").status_code == 409


def test_scenario_from_the_environment_loads_at_startup(monkeypatch):
    monkeypatch.setenv("SCENARIO", "t2_mark_and_drive")
    monkeypatch.setenv("MOCK_TIME_SCALE", "5000")
    with TestClient(create_app()) as c:
        wait_for(lambda: c.get("/state").json()["mission"]["status"] == "awaiting_approval", what="scenario to load")
        assert c.get("/state").json()["mission"]["source"] == "scenario"


def test_a_bad_scenario_in_the_environment_does_not_stop_the_server(monkeypatch):
    monkeypatch.setenv("SCENARIO", "does_not_exist")
    with TestClient(create_app()) as c:
        assert c.get("/state").json()["mission"]["status"] == "idle"


# ---------------------------------------------------------------- CLI
def test_cli_runs_a_scenario(make_ctrl):
    c = make_ctrl()
    out = io.StringIO()
    assert cli.run(c, "", auto_approve=True, mentor=True, timeout_s=30, out=out, scenario="t2_mark_and_drive") == "done"
    assert "drone_mark" in out.getvalue() and "Mission complete." in out.getvalue()


# ---------------------------------------------------------------- one field per scenario
FLOOR = str(Path(__file__).resolve().parent.parent / "manager" / "arena.floor.json")


def test_the_list_only_offers_scenarios_that_fit_the_field():
    mock = {s["name"] for s in list_scenarios("mock")}
    floor = {s["name"] for s in list_scenarios("floor")}
    assert "t3_recover_hostile" in mock and "t3_recover_hostile" not in floor
    assert {"floor_recover_hostile", "floor_search_field_a", "floor_mark_and_drive"} <= floor - mock


def test_a_scenario_for_the_other_field_is_refused_with_the_reason(make_ctrl):
    from manager.controller import Conflict
    with pytest.raises(Conflict, match="floor field"):
        make_ctrl().load_scenario("floor_recover_hostile")


def test_the_floor_recovery_runs_with_the_target_crazyflie_riding_on_the_rover(make_ctrl, monkeypatch):
    """Tomorrow's setup: ROVER_RIDER=target (the target Crazyflie on the EP), the wreck at a fixed spot."""
    monkeypatch.setenv("ARENA", FLOOR)
    c = make_ctrl(rover_rider="target")
    c.load_scenario("floor_recover_hostile")
    wait_status(c, "awaiting_approval")
    st = answer_questions(c)
    assert st["mission"]["status"] == "done", st["mission"]["error"]
    assert c.base_ctx.loc.rover_pose().source in ("lighthouse_rider", "rider+odometry")
    assert st["findings"][0]["status"] == "recovered"


def test_the_report_page_is_readable_escaped_and_carries_the_photo(make_ctrl):
    c = make_ctrl()
    c.load_scenario("t3_recover_hostile")
    wait_status(c, "awaiting_approval")
    answer_questions(c)
    c.store.say("human", "<script>alert(1)</script>")
    with TestClient(create_app(c)) as client:
        r = client.get("/report.html")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert "Mission complete" in r.text and "Save as PDF" in r.text and "data:image/jpeg;base64," in r.text
        assert "<script>alert(1)" not in r.text and "&lt;script&gt;" in r.text
        assert client.get("/report").json()["mission"]["status"] == "done"            # the JSON record stays
    assert (Path(c.cfg.runs_dir) / c.store.mission.id / "report.html").is_file()


@pytest.mark.parametrize("name", [n for n in SCENARIOS if load_scenario(n).arena == "floor"])
def test_floor_scenarios_run_without_ai_in_map_mode(name, make_ctrl, monkeypatch):
    """The no-AI floor demo: search, approach, triage and the grip check all steer on positions."""
    monkeypatch.setenv("ARENA", str(Path(__file__).resolve().parent.parent / "manager" / "arena.floor.json"))
    c = make_ctrl(perception="map")
    c.load_scenario(name)
    wait_status(c, "awaiting_approval")
    st = answer_questions(c)
    assert st["mission"]["status"] == "done", (name, st["mission"]["error"])
    assert any("Map mode" in m["text"] for m in st["messages"]) or "inspect" not in str(load_scenario(name).plan)


def test_floor_mark_and_drive_parks_the_rover_ready_to_grip(make_ctrl, monkeypatch):
    """gap 0.18: the rover parks grip_reach_m + 0.18 m from the spot, facing it."""
    from manager.tools import distance
    monkeypatch.setenv("ARENA", FLOOR)
    c = make_ctrl(rover_rider="target")
    c.load_scenario("floor_mark_and_drive")
    st = answer_questions(c)
    assert st["mission"]["status"] == "done", st["mission"]["error"]
    import math
    a = c.base_ctx.arena
    f, p = st["findings"][0], c.base_ctx.loc.rover_pose()
    assert abs(distance(p.x, p.y, f["x"], f["y"]) - (a.grip_reach_m + 0.18)) <= 2 * a.goto_tolerance_m
    bearing = (math.degrees(math.atan2(f["y"] - p.y, f["x"] - p.x)) - p.yaw + 180) % 360 - 180
    assert abs(bearing) <= 10                                     # facing it: the gripper points at the spot
    assert [s["status"] for s in st["plan"]] == ["done"] * 4  # and the scout went home after
