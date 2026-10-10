import time

import pytest

from manager.config import Config
from manager.llm import ScriptedLLM
from manager.models import StepDraft, SupervisorDecision
from manager.runtime import build_controller, build_ctx
from manager.tools import REGISTRY


@pytest.fixture
def cfg(tmp_path):
    """Mock world at 5000x speed: a whole mission takes well under a second of wall time."""
    return Config(mock_time_scale=5000.0, mock_seed=3, runs_dir=str(tmp_path / "runs"))


@pytest.fixture
def dock_cfg(cfg):
    """DOCK_MODE=1: the old 'drone rides the rover plate' behaviour (ai-manager-plan §7)."""
    import dataclasses
    return dataclasses.replace(cfg, dock_mode=True)


@pytest.fixture
def dock_ctx(dock_cfg):
    c, world = build_ctx(dock_cfg)
    c.world = world
    c.store.new_mission("test")
    c.store.mentor_present = True
    return c


@pytest.fixture
def ctx(cfg):
    c, world = build_ctx(cfg)
    c.world = world
    c.store.new_mission("test")
    c.store.mentor_present = True
    return c


@pytest.fixture
def make_ctrl(cfg):
    made = []

    def make(llm=None, auto_approve=False, mentor=True, **cfg_overrides):
        import dataclasses
        ctrl = build_controller(dataclasses.replace(cfg, **cfg_overrides), llm=llm, auto_approve=auto_approve,
                                use_env_llm=False)
        ctrl.start()
        ctrl.set_mentor(mentor)
        made.append(ctrl)
        return ctrl

    yield make
    for c in made:
        c.shutdown()


def wait_for(pred, timeout=15.0, what="condition"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def wait_status(ctrl, *statuses, timeout=15.0):
    wait_for(lambda: ctrl.store.status in statuses, timeout, f"status in {statuses} (now {ctrl.store.status})")


def finish(ctrl, answers=("yes",), timeout=20.0):
    """Wait for a running mission to end, answering each question it asks (the last answer repeats)."""
    t0, asked = time.time(), 0
    while ctrl.store.status not in ("done", "failed", "stopped"):
        if time.time() - t0 > timeout:
            raise AssertionError(f"mission did not finish; status {ctrl.store.status}, "
                                 f"question {ctrl.store.mission.pending_question!r}")
        if ctrl.store.mission.pending_question is not None:
            ctrl.change(answers[min(asked, len(answers) - 1)])
            asked += 1
            # consumed = the executor took the answer. (Watching pending_question is not enough: the next question can
            # replace it faster than we poll.)
            wait_for(lambda: ctrl._answer is None or ctrl.store.status in ("done", "failed", "stopped"),
                     what="the answer to be consumed")
        time.sleep(0.005)
    return ctrl.store.to_json()


def run_to_end(ctrl, text, timeout=20.0, answers=("yes",)):
    """Plan, approve, and wait for the mission to finish, answering questions."""
    ctrl.start_mission(text)
    wait_status(ctrl, "awaiting_approval")
    ctrl.approve(ctrl.store.mission.plan_version)
    return finish(ctrl, answers, timeout)


def draft(action, **kw) -> StepDraft:
    return StepDraft(robot=REGISTRY[action].robot, action=action, why="test", **kw)


def decision(kind, reason="", steps=()):
    return SupervisorDecision(decision=kind, reason=reason, remaining_steps=list(steps))


def scripted(*responses) -> ScriptedLLM:
    return ScriptedLLM(responses)


def step_status(state, action):
    return [s["status"] for s in state["plan"] if s["action"] == action]
