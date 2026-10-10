"""Plan validation, the canned plan, the rule supervisor, perception geometry."""
import pytest

from manager.fallback_plans import demo_plan, pick_zone, wants_fetch
from manager.models import Step, load_arena
from manager.perception import GeminiPerception, _PointItem, _Points
from manager.planner import validate_drafts
from manager.supervisor import RuleSupervisor, Situation, Trigger
from manager.llm import ScriptedLLM

from .conftest import draft


def test_valid_plan_passes():
    assert validate_drafts([draft("drone_scan", target="$zone.field_a"), draft("drone_land"),
                            draft("rover_goto", target="$finding.f1"), draft("report", text="x")]) == []


@pytest.mark.parametrize("steps,fragment", [
    ([], "no steps"),
    ([draft("drone_scan", target="$zone.field_a"), draft("rover_goto", target="$home")], "drone is still in the air"),
    ([draft("drone_scan", target="$zone.field_a"), draft("rover_search", target="box")], "drone is still in the air"),
    ([draft("rover_grip", mode="squeeze")], "must be one of"),
    ([draft("rover_goto")], "needs target"),
])
def test_invalid_plans_are_described(steps, fragment):
    problems = validate_drafts(steps, dock_mode=True)
    assert problems and fragment in " ".join(problems)


def test_unknown_action_and_wrong_robot_are_rejected():
    bad = draft("rover_goto", target="$home").model_copy(update={"action": "fly_to_moon"})
    assert "unknown action" in validate_drafts([bad])[0]
    wrong = draft("rover_goto", target="$home").model_copy(update={"robot": "drone"})
    assert "belongs to robot" in validate_drafts([wrong])[0]


def test_without_docking_rover_motion_with_the_drone_up_is_a_runtime_matter():
    steps = [draft("drone_goto", x=1, y=1, z=0.5), draft("rover_goto", target="$home")]
    assert validate_drafts(steps) == []                    # the separation rule is enforced when the step runs
    assert validate_drafts(steps, dock_mode=True)


def test_landing_makes_rover_motion_legal_again():
    assert validate_drafts([draft("drone_goto", x=1, y=1, z=0.5), draft("drone_land"),
                            draft("rover_goto", target="$home")], dock_mode=True) == []


def test_validation_respects_a_drone_that_is_already_up():
    assert validate_drafts([draft("rover_goto", target="$home")], drone_out=True, dock_mode=True)
    assert not validate_drafts([draft("drone_dock"), draft("rover_goto", target="$home")], drone_out=True,
                               dock_mode=True)


# ---------------------------------------------------------------- canned plan
def test_canned_plan_is_always_valid_and_picks_the_zone_from_the_text():
    arena = load_arena()
    for text in ("Check field A", "look at field B", "scan the test zone", "bring me the wreck from field B"):
        for dock_mode in (False, True):
            assert validate_drafts(demo_plan(text, arena, dock_mode), dock_mode=dock_mode) == [], (text, dock_mode)
    assert pick_zone("search field B please", arena) == "field_b"
    assert pick_zone("a drone crashed in field_a", arena) == "field_a"
    assert pick_zone("hello", arena) == "field_a"


def test_canned_plan_uses_land_without_docking_and_dock_with_it():
    arena = load_arena()
    assert "drone_dock" not in [d.action for d in demo_plan("Check field A", arena, dock_mode=False)]
    assert "drone_dock" in [d.action for d in demo_plan("Check field A", arena, dock_mode=True)]


def test_fetch_words_add_the_pickup_sequence():
    arena = load_arena()
    inspect_only = [d.action for d in demo_plan("Check field A", arena)]
    fetch = [d.action for d in demo_plan("Bring me what is in field A", arena)]
    assert "rover_approach" not in inspect_only and "rover_approach" in fetch
    assert wants_fetch("Please remove the box") and not wants_fetch("Check the exit")


# ---------------------------------------------------------------- rule supervisor
def sit(steps, findings=(), replans_left=2):
    return Situation(mission="m", steps=steps, findings=list(findings), robots={}, drone_out=False,
                     replans_left=replans_left)


def done(action, robot, args=None, result=None, status="done"):
    return Step(id="s1", robot=robot, action=action, args=args or {}, status=status, result=result)


def test_rules_stop_when_the_scan_finds_nothing():
    step = done("drone_scan", "drone", {"target": "$zone.field_a"}, {"ok": True, "findings": []})
    d = RuleSupervisor().decide(Trigger("checkpoint", step), sit([step]))
    assert d.decision == "done" and "nothing" in d.reason


def test_rules_drop_the_pickup_when_the_object_is_not_grippable():
    inspect = done("rover_inspect", "rover", result={"ok": True, "grippable": False, "label": "wall panel"})
    tail = [Step(id="s2", robot="rover", action="rover_arm", args={"mode": "pick"}),
            Step(id="s3", robot="rover", action="rover_approach", args={"target": "x"}),
            Step(id="s4", robot="manager", action="report", args={"text": "r"})]
    d = RuleSupervisor().decide(Trigger("checkpoint", inspect), sit([inspect, *tail]))
    assert d.decision == "replan" and [s.action for s in d.remaining_steps] == ["report"]


def test_rules_retry_once_budget_permitting_then_abort():
    step = done("rover_goto", "rover", {"target": "$home"}, {"ok": False, "error": "timed out"}, "failed")
    d = RuleSupervisor().decide(Trigger("step_failed", step, "timed out"), sit([step], replans_left=1))
    assert d.decision == "replan" and d.remaining_steps[0].action == "rover_goto"
    d = RuleSupervisor().decide(Trigger("step_failed", step, "timed out"), sit([step], replans_left=0))
    assert d.decision == "abort"


def test_rules_never_retry_an_unreachable_robot():
    step = done("rover_goto", "rover", {"target": "$home"}, {"ok": False, "error": "x", "offline": "rover"}, "failed")
    assert RuleSupervisor().decide(Trigger("step_failed", step), sit([step])).decision == "abort"


def test_rules_ask_a_human_when_the_mentor_gate_blocks():
    step = done("drone_scan", "drone", {"target": "$zone.field_a"}, {"ok": False, "error": "locked", "blocked": "mentor"},
                "failed")
    assert RuleSupervisor().decide(Trigger("step_failed", step), sit([step])).decision == "ask_human"


def test_rules_admit_they_cannot_apply_a_typed_change():
    d = RuleSupervisor().decide(Trigger("human_change", None, "skip the left"), sit([]))
    assert d.decision == "continue" and "unchanged" in d.reason


# ---------------------------------------------------------------- perception geometry
def test_er_points_are_converted_from_normalised_yx_to_pixels():
    arena = load_arena()
    llm = ScriptedLLM([_Points(points=[_PointItem(point=[500, 250], label="cup")])])
    p = GeminiPerception(llm, arena.camera).point(b"jpeg", "cup")
    assert p == {"col": 0.25 * 640, "row": 0.5 * 360, "label": "cup"}                # [y, x] -> (col, row)


def test_er_returns_none_when_nothing_is_pointed_at():
    llm = ScriptedLLM([_Points(points=[])])
    assert GeminiPerception(llm, load_arena().camera).point(b"jpeg", "cup") is None
