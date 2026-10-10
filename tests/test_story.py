"""WP-M8: the planner and supervisor on the farmland story (drone recovery)."""
import pytest

from manager import cli
from manager.fallback_plans import GENERIC_TARGET, demo_plan, pick_zone, wants_fetch
from manager.llm import ScriptedLLM
from manager.models import PlanDraft, Step, StepDraft, SupervisorDecision, load_arena
from manager.planner import Planner, rules_text, validate_drafts
from manager.supervisor import LLMSupervisor, RuleSupervisor, Situation, Trigger

from .conftest import draft, run_to_end

ARENA = load_arena()
HOSTILE = "A hostile drone crashed in field A. Find it and move it to the disposal zone."


def actions(drafts):
    return [d.action for d in drafts]


# ---------------------------------------------------------------- prompt rules
def test_the_rules_tell_the_recovery_story():
    r = rules_text(False)
    for must in ("drone_search", "drone_mark", "rover_goto", "drone_land", "rover_inspect", "request_authorization",
                 "$zone.disposal", "$home", "One item per trip", "not a drone", "report"):
        assert must in r, must
    assert "rides on the rover" not in r and "drone_dock" not in r             # docking rules only with DOCK_MODE=1


def test_the_docking_rule_only_appears_in_dock_mode():
    assert "rides on the rover" in rules_text(True) and "drone_dock" in rules_text(True)
    assert [ln[:2] for ln in rules_text(False).splitlines() if ln[:1].isdigit()][-1] == "7."
    assert [ln[:2] for ln in rules_text(True).splitlines() if ln[:1].isdigit()][-1] == "8."


def test_the_planner_prompt_carries_rules_zones_and_the_full_vocabulary():
    llm = ScriptedLLM([PlanDraft(summary="s", steps=demo_plan(HOSTILE, ARENA))])
    Planner(llm, ARENA).plan(HOSTILE)
    prompt = llm.calls[0][1]
    for must in ("field_a", "disposal", "drone_search", "request_authorization", "rover_snapshot", HOSTILE,
                 "recover crashed drones"):
        assert must in prompt, must
    assert "rides on the rover" not in prompt


def test_the_supervisor_prompt_knows_the_triage_rules():
    llm = ScriptedLLM([SupervisorDecision(decision="continue")])
    step = Step(id="s5", robot="rover", action="rover_inspect", status="done",
                result={"ok": True, "category": "own_drone"})
    LLMSupervisor(llm, ARENA).decide(Trigger("checkpoint", step), sit([step]))
    prompt = llm.calls[0][1]
    assert "own_drone -> deliver to $home" in prompt and "not_a_drone -> done" in prompt


# ---------------------------------------------------------------- the canned plan
def test_the_canned_plan_runs_search_mark_drive_land_search_inspect():
    plan = demo_plan("Find the drone in field B", ARENA)
    assert actions(plan) == ["drone_search", "drone_mark", "rover_goto", "drone_land", "rover_search", "rover_inspect",
                             "report"]
    assert plan[0].target == "$zone.field_b" and plan[1].target == plan[2].target == "$finding.f1"
    assert plan[4].target == GENERIC_TARGET


def test_the_canned_plan_for_a_recovery_asks_before_it_picks_up_and_delivers():
    plan = demo_plan(HOSTILE, ARENA)
    a = actions(plan)
    assert a[6:] == ["request_authorization", "rover_arm", "rover_grip", "rover_approach", "rover_grip", "rover_arm",
                     "rover_check_grip", "rover_goto", "rover_grip", "rover_arm", "report"]
    assert plan[6].finding_id == "f1" and plan[13].target == "$zone.disposal"
    assert [(d.action, d.mode) for d in plan if d.mode] == [("rover_arm", "pick"), ("rover_grip", "open"),
                                                            ("rover_grip", "close"), ("rover_arm", "carry"),
                                                            ("rover_grip", "open"), ("rover_arm", "home")]


@pytest.mark.parametrize("text", [HOSTILE, "Check field B", "Find the wreck", "recover it from the test zone", ""])
@pytest.mark.parametrize("dock_mode", [False, True])
def test_the_canned_plan_always_validates(text, dock_mode):
    assert validate_drafts(demo_plan(text, ARENA, dock_mode), dock_mode=dock_mode) == []


def test_in_dock_mode_the_scout_lands_before_the_rover_drives():
    a = actions(demo_plan(HOSTILE, ARENA, dock_mode=True))
    assert a[:3] == ["drone_search", "drone_dock", "rover_goto"] and "drone_mark" not in a and "drone_land" not in a


def test_zone_and_fetch_detection():
    assert pick_zone("search field B", ARENA) == "field_b" and pick_zone("a drone is down", ARENA) == "field_a"
    for text in ("move it to the disposal zone", "Recover the wreck", "please collect it", "bring it back"):
        assert wants_fetch(text), text
    assert not wants_fetch("Find the drone in field A") and not wants_fetch("Check field B")


def test_the_default_cli_mission_is_the_recovery_story():
    assert cli.DEFAULT_MISSION == HOSTILE
    assert wants_fetch(cli.DEFAULT_MISSION) and pick_zone(cli.DEFAULT_MISSION, ARENA) == "field_a"


# ---------------------------------------------------------------- rule supervisor
def sit(steps, findings=()):
    return Situation(mission="m", steps=steps, findings=list(findings), robots={}, drone_out=False, replans_left=2)


def st(action, robot, args=None, result=None, status="done", id="s1"):
    return Step(id=id, robot=robot, action=action, args=args or {}, status=status, result=result)


def pending(*pairs):
    return [Step(id=f"p{i}", robot=r, action=a, args=args) for i, (a, r, args) in enumerate(pairs)]


@pytest.mark.parametrize("zone,question", [("field_a", "Nothing found in field A. Search field B?"),
                                           ("field_b", "Nothing found in field B. Search field A?")])
def test_an_empty_search_asks_whether_to_try_the_other_field(zone, question):
    step = st("drone_search", "drone", {"target": f"$zone.{zone}"}, {"ok": True, "findings": []})
    d = RuleSupervisor().decide(Trigger("checkpoint", step), sit([step]))
    assert d.decision == "ask_human" and d.reason == question


def test_an_empty_search_of_some_other_area_just_ends():
    step = st("drone_search", "drone", {"target": "$zone.test_zone"}, {"ok": True, "findings": []})
    assert RuleSupervisor().decide(Trigger("checkpoint", step), sit([step])).decision == "done"


def test_a_search_that_finds_something_continues():
    step = st("drone_search", "drone", {"target": "$zone.field_a"}, {"ok": True, "findings": [{"id": "f1"}]})
    d = RuleSupervisor().decide(Trigger("checkpoint", step), sit([step]))
    assert d.decision == "continue" and "1 object" in d.reason


def test_a_yes_to_the_other_field_replans_with_a_new_search_in_front_of_the_rest():
    ask = st("ask_human", "manager", {"question": "Nothing found in field A. Search field B?"}, {"ok": True, "answer": "Yes!"})
    rest = pending(("drone_mark", "drone", {"target": "$finding.f1"}), ("rover_goto", "rover", {"target": "$finding.f1"}))
    d = RuleSupervisor().decide(Trigger("checkpoint", ask), sit([ask, *rest]))
    assert d.decision == "replan" and actions(d.remaining_steps) == ["drone_search", "drone_mark", "rover_goto"]
    assert d.remaining_steps[0].target == "$zone.field_b"


def test_a_no_to_the_other_field_ends_the_search():
    ask = st("ask_human", "manager", {"question": "Nothing found in field B. Search field A?"}, {"ok": True, "answer": "no"})
    d = RuleSupervisor().decide(Trigger("checkpoint", ask), sit([ask, *pending(("drone_mark", "drone", {"target": "$finding.f1"}))]))
    assert d.decision == "done" and "operator" in d.reason


def test_any_other_answer_just_continues():
    ask = st("ask_human", "manager", {"question": "Is a mentor here?"}, {"ok": True, "answer": "yes"})
    assert RuleSupervisor().decide(Trigger("checkpoint", ask), sit([ask])).decision == "continue"


def test_a_declined_authorization_skips_the_item():
    step = st("request_authorization", "manager", {"finding_id": "f1"}, {"ok": False, "error": "operator declined"}, "failed")
    d = RuleSupervisor().decide(Trigger("step_failed", step, "operator declined"), sit([step]))
    assert d.decision == "done" and "did not authorize" in d.reason


def test_our_own_drone_goes_home_and_skips_the_question():
    inspect = st("rover_inspect", "rover", result={"ok": True, "category": "own_drone", "hazardous": False, "grippable": True})
    rest = pending(("request_authorization", "manager", {"finding_id": "f1"}), ("rover_arm", "rover", {"mode": "pick"}),
                   ("rover_goto", "rover", {"target": "$zone.disposal"}), ("report", "manager", {"text": "x"}))
    d = RuleSupervisor().decide(Trigger("checkpoint", inspect), sit([inspect, *rest]))
    assert d.decision == "replan" and actions(d.remaining_steps) == ["rover_arm", "rover_goto", "report"]
    assert d.remaining_steps[1].target == "$home" and "ours" in d.reason


def test_a_hostile_drone_keeps_the_authorization_and_the_disposal_zone():
    inspect = st("rover_inspect", "rover", result={"ok": True, "category": "hostile_drone", "hazardous": True, "grippable": True})
    rest = pending(("request_authorization", "manager", {"finding_id": "f1"}), ("rover_goto", "rover", {"target": "$zone.disposal"}))
    assert RuleSupervisor().decide(Trigger("checkpoint", inspect), sit([inspect, *rest])).decision == "continue"


def test_not_a_drone_ends_the_mission():
    inspect = st("rover_inspect", "rover", result={"ok": True, "category": "not_a_drone", "hazardous": False})
    d = RuleSupervisor().decide(Trigger("checkpoint", inspect), sit([inspect]))
    assert d.decision == "done" and "nothing to recover" in d.reason


def test_an_ungrippable_object_drops_the_whole_pickup_including_the_authorization_question():
    inspect = st("rover_inspect", "rover", result={"ok": True, "category": "hostile_drone", "hazardous": True, "grippable": False})
    rest = pending(("request_authorization", "manager", {"finding_id": "f1"}), ("rover_arm", "rover", {"mode": "pick"}),
                   ("rover_grip", "rover", {"mode": "close"}), ("rover_goto", "rover", {"target": "$zone.disposal"}),
                   ("rover_arm", "rover", {"mode": "home"}), ("report", "manager", {"text": "x"}))
    d = RuleSupervisor().decide(Trigger("checkpoint", inspect), sit([inspect, *rest]))
    assert d.decision == "replan" and actions(d.remaining_steps) == ["report"]


# ---------------------------------------------------------------- end to end on the mock, no LLM
def asked(st_):
    return [m["text"] for m in st_["messages"] if m["text"].endswith("(yes/no)") or m["text"].endswith("?")]


def test_a_typed_recovery_mission_end_to_end(make_ctrl):
    c = make_ctrl()
    st_ = run_to_end(c, HOSTILE)
    assert st_["mission"]["status"] == "done", st_["mission"]["error"]
    assert st_["mission"]["source"] == "fallback" and st_["findings"][0]["status"] == "recovered"
    assert len([q for q in asked(st_) if "Authorize" in q]) == 1
    assert c.base_ctx.arena.zones["disposal"].contains(c.world.objects[0]["x"], c.world.objects[0]["y"])


def test_declining_leaves_the_wreck_where_it_is(make_ctrl):
    c = make_ctrl()
    st_ = run_to_end(c, HOSTILE, answers=("no",))
    assert st_["mission"]["status"] == "done" and st_["findings"][0]["status"] == "skipped"
    assert (c.world.objects[0]["x"], c.world.objects[0]["y"]) == (2.4, 1.1) and c.world.held is None


def test_our_own_drone_is_brought_home_without_asking(make_ctrl):
    c = make_ctrl()
    c.world.objects[0].update(kind="own_drone", label="own drone", color="#2471a3")
    st_ = run_to_end(c, "One of our drones went down in field A. Find it and bring it back.")
    assert st_["mission"]["status"] == "done", st_["mission"]["error"]
    assert not [q for q in asked(st_) if "Authorize" in q]
    assert c.base_ctx.arena.zones["base"].contains(c.world.objects[0]["x"], c.world.objects[0]["y"])
    assert (st_["findings"][0]["category"], st_["findings"][0]["status"]) == ("own_drone", "recovered")


def test_an_empty_field_a_then_field_b_on_the_operators_yes(make_ctrl):
    c = make_ctrl()
    c.world.objects[0].update(x=2.4, y=2.2)                                    # the wreck is in field B
    st_ = run_to_end(c, HOSTILE, answers=("yes", "yes"))
    assert st_["mission"]["status"] == "done", st_["mission"]["error"]
    qs = asked(st_)
    assert qs[0] == "Nothing found in field A. Search field B?" and "Authorize" in qs[1]
    searches = [s for s in st_["plan"] if s["action"] == "drone_search"]
    assert [s["args"]["target"] for s in searches] == ["$zone.field_a", "$zone.field_b"]
    assert c.base_ctx.arena.zones["disposal"].contains(c.world.objects[0]["x"], c.world.objects[0]["y"])


def test_inspect_only_missions_never_touch_the_object(make_ctrl):
    c = make_ctrl()
    st_ = run_to_end(c, "Find the drone in field A and tell me what it is")
    assert st_["mission"]["status"] == "done" and not asked(st_)
    assert (c.world.objects[0]["x"], c.world.objects[0]["y"]) == (2.4, 1.1)
    assert st_["findings"][0]["status"] == "inspected" and "rover_grip" not in [s["action"] for s in st_["plan"]]


def test_an_llm_authored_recovery_plan_is_accepted_and_runs(make_ctrl):
    plan = PlanDraft(summary="Search, mark, check, ask, recover", steps=demo_plan(HOSTILE, ARENA))
    c = make_ctrl(llm=ScriptedLLM([plan] + [SupervisorDecision(decision="continue")] * 4))
    st_ = run_to_end(c, HOSTILE)
    assert st_["mission"]["source"] == "ai" and st_["mission"]["status"] == "done", st_["mission"]["error"]
    assert st_["findings"][0]["status"] == "recovered"


def test_dock_mode_still_completes_a_recovery(make_ctrl):
    c = make_ctrl(dock_mode=True)
    st_ = run_to_end(c, HOSTILE)
    assert st_["mission"]["status"] == "done", st_["mission"]["error"]
    assert "drone_dock" in [s["action"] for s in st_["plan"] if s["status"] == "done"]
    assert c.world.drone["state"] == "docked"


def test_validation_still_rejects_what_the_prompt_forbids():
    assert validate_drafts([draft("drone_search", target="$zone.field_a"), draft("rover_goto", target="$home")],
                           dock_mode=True)
    assert validate_drafts([StepDraft(robot="rover", action="teleport")])
