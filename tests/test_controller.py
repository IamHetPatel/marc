"""Mission state machine (docs/ai-manager-plan.md §4): transitions, stale approvals, Stop from every state,
live changes, ask_human, replan budget."""
import threading
import time

import pytest

from manager.controller import Conflict, StaleApproval
from manager.llm import LLMError
from manager.models import PlanDraft

from .conftest import (decision, draft, run_to_end, scripted, step_status, wait_for, wait_status)


def plan_draft(*steps, summary="Test plan"):
    return PlanDraft(summary=summary, steps=list(steps))


SCAN_DOCK = [draft("drone_scan", target="$zone.field_a"), draft("drone_dock")]
SHORT_PLAN = [*SCAN_DOCK, draft("rover_goto", target="$finding.f1"), draft("report", text="done")]


# ---------------------------------------------------------------- lifecycle
def test_idle_by_default(make_ctrl):
    c = make_ctrl()
    st = c.store.to_json()
    assert st["mission"]["status"] == "idle" and st["plan"] == []


def test_without_an_llm_the_canned_plan_is_offered_and_flagged(make_ctrl):
    c = make_ctrl()
    c.start_mission("Check the test zone for anything blocking the exit")
    wait_status(c, "awaiting_approval")
    st = c.store.to_json()
    assert st["mission"]["fallback"] is True and st["mission"]["plan_version"] == 1
    assert st["plan"][0]["action"] == "drone_search" and all(s["status"] == "pending" for s in st["plan"])
    assert [s["id"] for s in st["plan"][:3]] == ["s1", "s2", "s3"]


def test_the_robots_do_not_move_before_approval(make_ctrl):
    c = make_ctrl()
    c.start_mission("Check the exit")
    wait_status(c, "awaiting_approval")
    time.sleep(0.1)
    assert c.world.t == 0.0 and c.world.drone["state"] == "landed" and c.world.drone["z"] == 0.0


def test_cannot_start_a_second_mission_while_one_is_active(make_ctrl):
    c = make_ctrl()
    c.start_mission("a")
    wait_status(c, "awaiting_approval")
    with pytest.raises(Conflict):
        c.start_mission("b")


def test_empty_text_is_rejected(make_ctrl):
    with pytest.raises(ValueError):
        make_ctrl().start_mission("   ")


def test_stale_approval_is_rejected(make_ctrl):
    c = make_ctrl(llm=scripted(plan_draft(*SHORT_PLAN), plan_draft(*SHORT_PLAN, summary="v2")))
    c.start_mission("m")
    wait_status(c, "awaiting_approval")
    v1 = c.store.mission.plan_version
    c.change("also report")
    wait_for(lambda: c.store.mission.plan_version == v1 + 1 and c.store.status == "awaiting_approval", what="v2")
    with pytest.raises(StaleApproval):
        c.approve(v1)
    assert c.store.status == "awaiting_approval"                  # nothing started
    c.approve(v1 + 1)
    assert c.store.status == "running"


def test_approve_is_rejected_when_there_is_nothing_to_approve(make_ctrl):
    c = make_ctrl()
    with pytest.raises(Conflict):
        c.approve(1)


def test_change_in_awaiting_approval_replans_and_keeps_the_old_plan(make_ctrl):
    revised = [draft("drone_scan", target="$zone.field_b"), draft("drone_dock"),
               draft("rover_goto", target="$finding.f1"), draft("report", text="x")]
    c = make_ctrl(llm=scripted(plan_draft(*SHORT_PLAN), plan_draft(*revised, summary="Charging station")))
    c.start_mission("m")
    wait_status(c, "awaiting_approval")
    c.change("check the field B instead")
    wait_for(lambda: c.store.mission.plan_version == 2 and c.store.status == "awaiting_approval", what="revised plan")
    st = c.store.to_json()
    assert st["plan"][0]["args"]["target"] == "$zone.field_b"
    assert st["previous_plan"][0]["args"]["target"] == "$zone.field_a"
    assert c.planner.llm.calls[1][1].count("field B instead") >= 1       # the change reached the prompt


def test_reset_only_when_not_running(make_ctrl):
    c = make_ctrl()
    c.start_mission("m")
    wait_status(c, "awaiting_approval")
    c.reset()
    assert c.store.status == "idle" and c.store.plan == []


# ---------------------------------------------------------------- Stop
@pytest.mark.parametrize("state", ["idle", "planning", "awaiting_approval", "done", "stopped"])
def test_stop_works_from_every_resting_state(make_ctrl, state):
    c = make_ctrl()
    if state == "planning":
        gate = threading.Event()
        c.planner.plan = lambda *a, **k: gate.wait(5) and None
        c.start_mission("m")
        wait_status(c, "planning")
    elif state == "awaiting_approval":
        c.start_mission("m")
        wait_status(c, "awaiting_approval")
    elif state == "done":
        run_to_end(c, "Check the exit")
    elif state == "stopped":
        c.stop()
    out = c.stop()
    assert c.store.status == "stopped" and out["rover"] == "ok" and out["drone"] == "ok"


def test_stop_during_planning_discards_the_late_plan(make_ctrl):
    c = make_ctrl()
    gate = threading.Event()
    real = c.planner.plan

    def slow(*a, **k):
        gate.wait(5)
        return real(*a, **k)

    c.planner.plan = slow
    c.start_mission("m")
    wait_status(c, "planning")
    c.stop()
    gate.set()
    time.sleep(0.2)
    assert c.store.status == "stopped" and c.store.plan == []


def test_stop_during_a_scan_halts_everything(make_ctrl):
    c = make_ctrl(mock_time_scale=50.0)                           # slow enough to catch the scan mid-flight
    c.start_mission("Check the exit")
    wait_status(c, "awaiting_approval")
    c.approve(c.store.mission.plan_version)
    wait_for(lambda: c.store.plan[0].status == "running", what="scan running")
    c.stop()
    assert c.store.status == "stopped"
    time.sleep(0.3)
    assert c.store.status == "stopped"                            # the worker thread must not resurrect it
    assert c.world.drone["state"] in ("landed", "docked")
    assert c.store.plan[0].status in ("failed", "running") and c.store.plan[1].status == "pending"


def test_a_new_mission_can_start_after_stop(make_ctrl):
    c = make_ctrl()
    c.start_mission("a")
    wait_status(c, "awaiting_approval")
    c.stop()
    c.start_mission("b")
    wait_status(c, "awaiting_approval")
    assert c.store.mission.text == "b" and c.store.mission.id == "m2"


# ---------------------------------------------------------------- changes while running
def test_change_while_running_replans_live(make_ctrl):
    new_tail = [draft("drone_scan", target="$zone.field_b"), draft("drone_dock"),
                draft("report", text="checked the field B")]
    llm = scripted(plan_draft(*SHORT_PLAN),
                   decision("continue", "One spot found."),                    # checkpoint after the first scan
                   decision("replan", "Skipping that, checking the field B instead.", new_tail))
    c = make_ctrl(llm=llm, mock_time_scale=100.0)
    c.start_mission("m")
    wait_status(c, "awaiting_approval")
    c.approve(c.store.mission.plan_version)
    wait_for(lambda: c.store.plan[0].status == "running", what="first scan")
    c.change("skip that, check the field B instead")
    wait_status(c, "done", "failed", timeout=20)
    st = c.store.to_json()
    assert st["mission"]["status"] == "done", st["mission"]["error"]
    assert st["previous_plan"] is not None and st["mission"]["plan_version"] == 2
    scans = [s for s in st["plan"] if s["action"] == "drone_scan"]
    assert [s["args"]["target"] for s in scans] == ["$zone.field_a", "$zone.field_b"]
    assert "rover_goto" not in [s["action"] for s in st["plan"]]          # the old tail was dropped, not just reordered
    assert any("field B" in m["text"] for m in st["messages"] if m["role"] == "ai")


def test_change_needing_approval_pauses_then_continues(make_ctrl):
    new_tail = [draft("drone_dock"), draft("report", text="r")]
    llm = scripted(plan_draft(*SCAN_DOCK, draft("report", text="r")),
                   decision("continue"),
                   decision("replan", "Reworked.", new_tail))
    c = make_ctrl(llm=llm, mock_time_scale=100.0, change_needs_approval=True)
    c.start_mission("m")
    wait_status(c, "awaiting_approval")
    c.approve(c.store.mission.plan_version)
    wait_for(lambda: c.store.plan[0].status == "running", what="scan")
    c.change("something different")
    wait_status(c, "awaiting_approval", timeout=20)
    assert c.store.mission.plan_version == 2
    c.approve(2)
    wait_status(c, "done", "failed", timeout=20)
    assert c.store.status == "done"


def test_ask_human_pauses_until_the_answer_arrives(make_ctrl):
    llm = scripted(plan_draft(*SHORT_PLAN),
                   decision("ask_human", "Which object do you mean, the box or the cup?"),
                   decision("continue", "Thanks."))
    c = make_ctrl(llm=llm)
    c.start_mission("m")
    wait_status(c, "awaiting_approval")
    c.approve(c.store.mission.plan_version)
    wait_for(lambda: c.store.mission.pending_question is not None, what="the question")
    assert "box or the cup" in c.store.mission.pending_question
    assert c.store.status == "running"
    c.change("the box")
    wait_status(c, "done", "failed")
    st = c.store.to_json()
    asked = next(s for s in st["plan"] if s["action"] == "ask_human")
    assert asked["result"]["answer"] == "the box" and st["mission"]["pending_question"] is None


def test_stop_while_waiting_for_an_answer(make_ctrl):
    c = make_ctrl(llm=scripted(plan_draft(*SHORT_PLAN), decision("ask_human", "Which one?")))
    c.start_mission("m")
    wait_status(c, "awaiting_approval")
    c.approve(c.store.mission.plan_version)
    wait_for(lambda: c.store.mission.pending_question is not None, what="the question")
    c.stop()
    time.sleep(0.4)
    assert c.store.status == "stopped" and c.store.mission.pending_question is None


def test_cannot_reset_a_running_mission(make_ctrl):
    c = make_ctrl(mock_time_scale=50.0)
    c.start_mission("Check the exit")
    wait_status(c, "awaiting_approval")
    c.approve(c.store.mission.plan_version)
    with pytest.raises(Conflict):
        c.reset()
    c.stop()


# ---------------------------------------------------------------- failure handling
def test_mentor_gate_asks_the_human_then_the_scan_is_retried(make_ctrl):
    c = make_ctrl(mentor=False)
    c.start_mission("Check the exit")
    wait_status(c, "awaiting_approval")
    c.approve(c.store.mission.plan_version)
    wait_for(lambda: c.store.mission.pending_question is not None, what="mentor question")
    assert "safety pilot" in c.store.mission.pending_question
    c.set_mentor(True)
    c.change("mentor is here")
    wait_status(c, "done", "failed")
    st = c.store.to_json()
    assert st["mission"]["status"] == "done", st["mission"]["error"]
    assert "done" in step_status(st, "drone_search")


def test_replan_budget_ends_in_a_failed_mission_with_a_reason(make_ctrl):
    c = make_ctrl(mock_fail={"grip": 1.0})                       # the gripper never works
    st = run_to_end(c, "Bring me the object from the exit")
    assert st["mission"]["status"] == "failed"
    assert "keeps failing" in st["mission"]["error"] or "budget" in st["mission"]["error"].lower()
    grips = [s for s in st["plan"] if s["action"] == "rover_grip" and s["status"] == "failed"]
    assert 2 <= len(grips) <= 3                                    # tried, retried at most twice, then gave up
    assert st["messages"][-1]["text"].startswith("Mission failed")


def test_a_failed_dock_lands_the_drone_and_the_mission_carries_on(make_ctrl):
    c = make_ctrl(mock_time_scale=100.0, dock_mode=True)
    c.start_mission("Check the exit")
    wait_status(c, "awaiting_approval")
    c.approve(c.store.mission.plan_version)
    wait_for(lambda: c.store.plan[0].status == "running", what="scan")
    c.world.move_body(0.5, 0.0, 0.0)                              # the rover gets pushed while the drone is up
    wait_status(c, "done", "failed")
    st = c.store.to_json()
    assert "failed" in step_status(st, "drone_dock") and "done" in step_status(st, "drone_land")


def test_an_empty_search_asks_the_operator_and_a_no_ends_the_mission_cleanly(make_ctrl):
    c = make_ctrl()
    c.start_mission("Check field A")
    wait_status(c, "awaiting_approval")
    c.world.objects.clear()
    c.approve(c.store.mission.plan_version)
    wait_for(lambda: c.store.mission.pending_question is not None, what="the question")
    assert c.store.mission.pending_question == "Nothing found in field A. Search field B?"
    c.change("no")
    wait_status(c, "done", "failed")
    st = c.store.to_json()
    assert st["mission"]["status"] == "done" and all(s["status"] in ("done", "skipped") for s in st["plan"])
    assert any("Search stopped" in m["text"] for m in st["messages"])
    assert c.world.drone["state"] == "landed"                      # the scout is not left hovering


# ---------------------------------------------------------------- planner robustness
def test_slow_planner_falls_back_to_the_canned_plan(make_ctrl):
    def slow(prompt, schema):
        time.sleep(1.0)
        return plan_draft(*SHORT_PLAN)

    c = make_ctrl(llm=scripted(slow), plan_timeout_s=0.2)
    c.start_mission("Check the exit")
    wait_status(c, "awaiting_approval")
    assert c.store.mission.fallback is True
    assert any("longer than" in m.text for m in c.store.messages)
    time.sleep(1.0)                                               # the late LLM answer must not replace the plan
    assert c.store.mission.fallback is True and c.store.mission.plan_version == 1


def test_invalid_llm_plan_is_retried_once_with_the_reasons(make_ctrl):
    bad = plan_draft(draft("drone_scan", target="$zone.field_a"), draft("rover_goto", target="$home"))   # drone still up
    good = plan_draft(*SHORT_PLAN, summary="Fixed")
    llm = scripted(bad, good)
    c = make_ctrl(llm=llm, dock_mode=True)
    c.start_mission("m")
    wait_status(c, "awaiting_approval")
    assert c.store.mission.fallback is False and c.store.mission.reason == "Fixed"
    assert "rejected" in llm.calls[1][1] and "drone is still in the air" in llm.calls[1][1]


def test_two_invalid_plans_fall_back(make_ctrl):
    unknown = plan_draft(draft("rover_goto", target="$home").model_copy(update={"action": "teleport"}))
    c = make_ctrl(llm=scripted(unknown, unknown))
    c.start_mission("m")
    wait_status(c, "awaiting_approval")
    assert c.store.mission.fallback is True


def test_llm_error_falls_back(make_ctrl):
    c = make_ctrl(llm=scripted(LLMError("quota exceeded")))
    c.start_mission("m")
    wait_status(c, "awaiting_approval")
    assert c.store.mission.fallback is True


def test_supervisor_errors_fall_back_to_rules_and_the_mission_still_finishes(make_ctrl):
    llm = scripted(plan_draft(*SHORT_PLAN), LLMError("boom"), LLMError("boom"), LLMError("boom"))
    c = make_ctrl(llm=llm)
    st = run_to_end(c, "m")
    assert st["mission"]["status"] == "done", st["mission"]["error"]       # rules keep the mission moving
    assert any("rule-based" in m["text"] for m in st["messages"] if m["role"] == "ai")


def test_a_missing_target_drone_never_trips_the_watchdog(make_ctrl):
    from manager.adapters.base import RobotError
    c = make_ctrl()
    c.store.set_status("running")                                  # pretend a mission is moving robots
    real = c.base_ctx.drone.pose

    def pose(cf="scout"):
        if cf == "target":
            raise RobotError("target link down")
        return real(cf)

    c.base_ctx.drone.pose = pose
    for _ in range(c.cfg.watchdog_fails * 3):
        c.monitor.poll()
    assert c.store.status == "running"
    assert c.store.robots["target"]["online"] is False and c.store.robots["drone"]["online"] is True


def test_a_dead_scout_still_trips_the_watchdog(make_ctrl):
    from manager.adapters.base import RobotError
    c = make_ctrl()
    c.store.set_status("running")

    def down(cf="scout"):
        raise RobotError("no radio")

    c.base_ctx.drone.pose = down
    for _ in range(c.cfg.watchdog_fails):
        c.monitor.poll()
    assert c.store.status == "stopped" and any("Watchdog" in m.text for m in c.store.messages)
