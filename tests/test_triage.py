"""WP-M6: triage with the rover camera, the operator's authorization (enforced in code), disposal, the report."""
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from manager.models import load_arena
from manager.perception import MockPerception
from manager.server import create_app
from manager.tools import run_action

from .conftest import finish, wait_for, wait_status


def go(ctx, name, **args):
    return run_action(ctx, name, args)


def run_scenario(c, name, answers=("yes",)):
    c.load_scenario(name)
    wait_status(c, "awaiting_approval")
    c.approve(c.store.mission.plan_version)
    return finish(c, answers)


def questions(st):
    return [m["text"] for m in st["messages"] if m["text"].endswith("(yes/no)")]


def at_wreck(ctx):
    """Drive the rover next to the finding so inspect/grip see it."""
    go(ctx, "drone_mark", target="2.4,1.1")
    go(ctx, "rover_goto", target="$finding.f1")
    go(ctx, "drone_land")
    assert go(ctx, "rover_search", target="crashed drone")["found"]


# ---------------------------------------------------------------- perception
@pytest.mark.parametrize("kind,category,hazardous", [("hostile_drone", "hostile_drone", True),
                                                      ("own_drone", "own_drone", False),
                                                      ("decoy", "not_a_drone", False),
                                                      ("unknown", "unknown", True)])
def test_mock_triage_by_kind(ctx, kind, category, hazardous):
    ctx.world.objects[0]["kind"] = kind
    at_wreck(ctx)
    r = go(ctx, "rover_inspect", question="what is this")
    assert (r["category"], r["hazardous"]) == (category, hazardous) and r["reason"]


def test_an_empty_floor_is_not_hazardous(ctx):
    ctx.world.objects.clear()
    r = MockPerception(ctx.world).inspect(b"", "?")
    assert r["label"] == "nothing" and r["hazardous"] is False and r["category"] == "unknown"


def test_inspect_records_the_triage_on_the_nearest_finding(ctx):
    at_wreck(ctx)
    r = go(ctx, "rover_inspect", question="what is this")
    f = ctx.store.get_finding("f1")
    assert r["finding_id"] == "f1" and (f.category, f.hazardous, f.status) == ("hostile_drone", True, "inspected")
    assert f.authorized is False and f.label == "crashed drone" and f.image_id == r["image_id"]


def test_inspecting_something_nobody_flagged_registers_it_ahead_of_the_rover(ctx):
    ctx.world.objects[0].update(x=ctx.arena.home.x + 0.8, y=ctx.arena.home.y)           # 0.8 m straight ahead
    r = go(ctx, "rover_inspect", question="what is this")
    (f,) = ctx.store.findings
    assert r["finding_id"] == f.id and f.by == "rover" and f.status == "inspected"
    assert f.x == pytest.approx(ctx.arena.home.x + 0.6, abs=0.01) and f.y == pytest.approx(ctx.arena.home.y, abs=0.01)


def test_inspecting_an_empty_floor_does_not_invent_a_finding(ctx):
    ctx.world.objects.clear()
    r = go(ctx, "rover_inspect", question="anything?")
    assert r["label"] == "nothing" and r["finding_id"] is None and ctx.store.findings == []


def test_reinspection_does_not_downgrade_a_recovered_finding(ctx):
    at_wreck(ctx)
    ctx.store.update_finding("f1", status="recovered")
    go(ctx, "rover_inspect", question="again")
    assert ctx.store.get_finding("f1").status == "recovered"


# ---------------------------------------------------------------- request_authorization
@pytest.mark.parametrize("answer", ["yes", "Yes", "y", "  YES please", "yep"])
def test_a_yes_authorizes(ctx, answer):
    at_wreck(ctx)
    go(ctx, "rover_inspect", question="?")
    asked = []
    ctx.ask = lambda q: asked.append(q) or answer
    r = go(ctx, "request_authorization", finding_id="f1")
    assert r["ok"] and r["authorized"] is True and ctx.store.get_finding("f1").authorized is True
    assert "hostile drone" in asked[0] and "hazardous" in asked[0] and asked[0].endswith("(yes/no)")
    assert any(m.text == asked[0] for m in ctx.store.messages)                         # the log shows the question


@pytest.mark.parametrize("answer", ["no", "No", "nope", "n", "later", ""])
def test_anything_but_a_yes_declines_and_skips_the_item(ctx, answer):
    at_wreck(ctx)
    go(ctx, "rover_inspect", question="?")
    ctx.ask = lambda q: answer
    r = go(ctx, "request_authorization", finding_id="f1")
    assert r == {"ok": False, "authorized": False, "finding_id": "f1", "error": "operator declined"}
    f = ctx.store.get_finding("f1")
    assert f.authorized is False and f.status == "skipped"


def test_no_operator_channel_or_no_answer_is_an_error_not_a_yes(ctx):
    ctx.store.add_findings([{"x": 1, "y": 1}])
    assert go(ctx, "request_authorization", finding_id="f1")["error"] == "no answer received from the operator"
    ctx.ask = lambda q: None                                                          # cancelled while waiting
    assert go(ctx, "request_authorization", finding_id="f1")["ok"] is False
    assert ctx.store.get_finding("f1").authorized is False
    assert "no finding" in go(ctx, "request_authorization", finding_id="f9")["error"]


# ---------------------------------------------------------------- the gate (code, not prompt)
def prepared(ctx):
    at_wreck(ctx)
    go(ctx, "rover_inspect", question="?")
    go(ctx, "rover_arm", mode="pick")
    go(ctx, "rover_grip", mode="open")
    assert go(ctx, "rover_approach", target="crashed drone")["in_reach"]


def test_a_hazardous_finding_blocks_closing_the_gripper_until_authorized(ctx):
    prepared(ctx)
    r = go(ctx, "rover_grip", mode="close")
    assert r["ok"] is False and r["blocked"] == "authorization" and "f1" in r["error"] and ctx.world.held is None
    assert go(ctx, "rover_grip", mode="open")["ok"]                                    # opening is always fine
    ctx.store.update_finding("f1", authorized=True)
    assert go(ctx, "rover_grip", mode="close")["ok"] and ctx.world.held is not None


def test_the_gate_holds_for_manual_control_too(make_ctrl):
    c = make_ctrl()
    ctx = c.base_ctx
    ctx.store.new_mission("t")
    prepared(ctx)
    ctx.store.reset()                                                                  # idle again, findings gone...
    ctx.store.add_findings([{"x": 2.4, "y": 1.1}])                                     # ...re-flag the wreck
    ctx.store.update_finding("f1", hazardous=True, category="hostile_drone", status="inspected")
    c.manual("rover_grip", {"mode": "close"})
    wait_for(lambda: (c.store.manual or {}).get("status") == "failed", what="manual grip refused")
    assert c.store.manual["result"]["blocked"] == "authorization"


def test_non_hazardous_far_or_uninspected_things_do_not_trip_the_gate(ctx):
    ctx.world.objects[0]["kind"] = "own_drone"
    prepared(ctx)
    assert go(ctx, "rover_grip", mode="close")["ok"]                                   # ours: no authorization needed
    ctx.world.held = None
    ctx.store.update_finding("f1", hazardous=True)
    ctx.world.move_body(-1.5, 0, 0)                                                    # hazardous but 1.5+ m away
    assert go(ctx, "rover_grip", mode="close")["ok"]
    ctx.store.update_finding("f1", hazardous=None)                                     # not inspected yet
    ctx.world.move_body(1.2, 0, 0)
    assert go(ctx, "rover_grip", mode="close")["ok"]


def test_a_successful_grip_check_marks_the_finding_recovered(ctx):
    prepared(ctx)
    ctx.store.update_finding("f1", authorized=True)
    go(ctx, "rover_grip", mode="close")
    go(ctx, "rover_arm", mode="carry")
    assert go(ctx, "rover_check_grip")["holding"] is True and ctx.store.get_finding("f1").status == "recovered"


def test_an_empty_grip_check_changes_nothing(ctx):
    prepared(ctx)
    assert go(ctx, "rover_check_grip")["holding"] is False and ctx.store.get_finding("f1").status == "inspected"


# ---------------------------------------------------------------- whole scenarios
def test_t3_recovers_the_hostile_drone_after_a_yes(make_ctrl):
    c = make_ctrl()
    st = run_scenario(c, "t3_recover_hostile")
    assert st["mission"]["status"] == "done", st["mission"]["error"]
    assert len(questions(st)) == 1 and "hostile drone" in questions(st)[0]
    f = st["findings"][0]
    assert (f["category"], f["hazardous"], f["authorized"], f["status"]) == ("hostile_drone", True, True, "recovered")
    wreck = c.world.objects[0]
    assert load_arena().zones["disposal"].contains(wreck["x"], wreck["y"]) and c.world.held is None
    assert all(s["status"] == "done" for s in st["plan"])


def test_t3_leaves_the_drone_alone_after_a_no(make_ctrl):
    c = make_ctrl()
    st = run_scenario(c, "t3_recover_hostile", answers=("no",))
    assert st["mission"]["status"] == "done" and any("did not authorize" in m["text"] for m in st["messages"])
    assert (st["findings"][0]["status"], st["findings"][0]["authorized"]) == ("skipped", False)
    wreck = c.world.objects[0]
    assert (wreck["x"], wreck["y"]) == (2.4, 1.1) and c.world.held is None
    done = [s["action"] for s in st["plan"] if s["status"] == "done"]
    assert "rover_grip" not in done and "rover_arm" not in done and len(questions(st)) == 1


def test_a_decoy_is_inspected_and_left_alone_without_bothering_the_operator(make_ctrl):
    c = make_ctrl()
    st = run_scenario(c, "decoy_bottle")
    assert st["mission"]["status"] == "done", st["mission"]["error"]
    assert questions(st) == [] and any("nothing to recover" in m["text"] for m in st["messages"])
    f = st["findings"][0]
    assert (f["label"], f["category"], f["hazardous"], f["status"]) == ("plastic bottle", "not_a_drone", False, "inspected")
    assert (c.world.objects[0]["x"], c.world.objects[0]["y"]) == (3.0, 2.0) and c.world.held is None
    assert [s["status"] for s in st["plan"]].count("skipped") == len(st["plan"]) - 5


def test_our_own_drone_goes_home_and_needs_no_authorization(make_ctrl):
    c = make_ctrl()
    st = run_scenario(c, "own_drone_home")
    assert st["mission"]["status"] == "done", st["mission"]["error"]
    assert questions(st) == []
    assert (st["findings"][0]["category"], st["findings"][0]["status"]) == ("own_drone", "recovered")
    assert load_arena().zones["base"].contains(c.world.objects[0]["x"], c.world.objects[0]["y"])


def test_the_scripted_hostile_plan_cannot_skip_the_authorization_step(make_ctrl):
    """Remove the question from the plan: the gripper still refuses, so the mission fails instead of grabbing it."""
    from manager.scenarios import load_scenario
    sc = load_scenario("t3_recover_hostile")
    plan = [s for s in sc.plan if s.action != "request_authorization"]
    c = make_ctrl()
    c.load_scenario("t3_recover_hostile")
    c.stop()
    c.submit_plan("no question", plan)
    c.approve(c.store.mission.plan_version)
    st = finish(c)
    assert st["mission"]["status"] == "failed" and "not authorized" in st["mission"]["error"]
    assert c.world.held is None


def test_the_question_is_visible_to_the_dashboard_while_waiting(make_ctrl):
    c = make_ctrl()
    c.load_scenario("t3_recover_hostile")
    c.approve(c.store.mission.plan_version)
    wait_for(lambda: c.store.mission.pending_question is not None, what="the authorization question")
    q = c.store.to_json()["mission"]["pending_question"]
    assert "Authorize the rover to pick it up? (yes/no)" in q and c.store.status == "running"
    c.change("yes")
    finish(c)


# ---------------------------------------------------------------- report
def test_report_has_what_a_handover_needs(make_ctrl):
    c = make_ctrl()
    run_scenario(c, "t3_recover_hostile")
    r = c.build_report()
    assert set(r) == {"mission", "started", "finished", "findings", "steps", "messages", "metrics"}
    assert r["mission"]["id"] == "m1" and r["mission"]["status"] == "done" and r["mission"]["source"] == "scenario"
    assert r["started"] <= r["finished"] and r["findings"][0]["status"] == "recovered"
    assert [s["action"] for s in r["steps"]][:2] == ["drone_mark", "rover_goto"] and r["steps"][-1]["status"] == "done"
    assert any(m["role"] == "human" and m["text"] == "yes" for m in r["messages"])


def test_a_report_file_is_written_when_a_mission_ends(make_ctrl, tmp_path):
    c = make_ctrl()
    run_scenario(c, "t2_mark_and_drive")
    path = tmp_path / "runs" / "m1" / "report.yaml"
    wait_for(path.exists, what="report.yaml")
    data = yaml.safe_load(path.read_text())
    assert data["mission"]["status"] == "done" and data["findings"][0]["id"] == "f1" and data["steps"][0]["id"] == "s1"


def test_failed_and_stopped_missions_leave_a_report_too(make_ctrl, tmp_path):
    c = make_ctrl(mock_fail={"goto": 1.0})
    c.load_scenario("t2_mark_and_drive")
    c.approve(c.store.mission.plan_version)
    st = finish(c)
    assert st["mission"]["status"] == "failed"
    wait_for((tmp_path / "runs" / "m1" / "report.yaml").exists, what="the failed mission's report")   # written just after
    assert yaml.safe_load((tmp_path / "runs" / "m1" / "report.yaml").read_text())["mission"]["status"] == "failed"
    c.start_mission("another")
    wait_status(c, "awaiting_approval")
    c.stop()
    assert yaml.safe_load((tmp_path / "runs" / "m2" / "report.yaml").read_text())["mission"]["status"] == "stopped"


def test_an_old_report_with_the_same_id_is_kept(make_ctrl, tmp_path):
    folder = tmp_path / "runs" / "m1"
    folder.mkdir(parents=True)
    (folder / "report.yaml").write_text("old: true\n")
    c = make_ctrl()
    run_scenario(c, "t2_mark_and_drive")
    wait_for(lambda: len(list(folder.glob("report*.yaml"))) == 2, what="old report to be kept")
    assert yaml.safe_load((folder / "report.yaml").read_text())["mission"]["id"] == "m1"
    assert any(p.read_text() == "old: true\n" for p in folder.glob("report.*.yaml"))


def test_state_carries_the_mission_timestamps(make_ctrl):
    c = make_ctrl()
    t0 = time.time()
    st = run_scenario(c, "t2_mark_and_drive")
    assert t0 - 1 <= st["mission"]["started_at"] <= st["mission"]["finished_at"] <= time.time() + 1


def test_report_endpoint(make_ctrl):
    c = make_ctrl()
    with TestClient(create_app(c)) as client:
        assert client.get("/report").status_code == 404
        client.post("/scenarios/t2_mark_and_drive/load")
        assert client.get("/report").json()["mission"]["status"] == "awaiting_approval"
        client.post("/approve", json={"plan_version": 1})
        wait_status(c, "done", "failed")
        r = client.get("/report").json()
        assert r["mission"]["status"] == "done" and r["findings"][0]["image_id"] and r["finished"]
