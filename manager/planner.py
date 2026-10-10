"""Mission text -> validated Plan. One structured-output LLM call; invalid output is re-requested once,
then we fall back to the canned plan (plan §8, §11). Nothing here moves a robot."""
import json
import logging
from dataclasses import dataclass
from typing import Optional

from .fallback_plans import demo_plan
from .llm import LLMError
from .models import Arena, PlanDraft, Step, StepDraft
from .tools import REGISTRY, validate_step, vocabulary_text

log = logging.getLogger("manager.planner")

MAX_STEPS = 30
ROVER_MOTION = {n for n, s in REGISTRY.items() if s.moves_rover}
DRONE_LAUNCH = {"drone_scan", "drone_goto", "drone_takeoff", "drone_mark", "drone_search"}
DRONE_RECOVER = {"drone_dock", "drone_land"}

HEADER = """\
You are the mission planner of FleetPilot, a human-supervised team of two robots that recover crashed drones from
farmland. A drone has come down somewhere in a field. It may be hostile and carry explosives, or it may be one of ours.

ROBOTS
- scout (robot name "drone"): a small Crazyflie. Fast. Flies only inside the flight cage. It searches an area with a
  downward detector and marks spots by hovering over them. It cannot identify what it finds and cannot carry anything.
- rover: a RoboMaster EP ground robot. Slow. Front camera, arm and gripper. It drives to a marked spot, identifies the
  object with its camera, and can pick it up and carry it.

RULES
"""

DOCK_RULE = (
    "The drone rides on the rover. The rover must NOT move while the drone is in the air: after drone_search, "
    "drone_scan or drone_goto always call drone_dock (or drone_land) before any rover_goto, rover_search or "
    "rover_approach. Skip drone_mark in that case.")


OVERHEAD_RULE = (
    'An overhead camera on the ceiling sees the whole field. If the scout is offline, or the mission asks for the '
    'overhead camera, use camera_locate instead of drone_search and drone_mark (then rover_goto "$finding.f1" and '
    "continue as usual, without drone_land).")


def rules_text(dock_mode: bool, overhead: bool = False) -> str:
    """The rules block shared by the planner and supervisor prompts. The docking rule only exists in DOCK_MODE,
    the overhead-camera rule only when the camera is installed."""
    rules = [
        "The scout searches and marks; the rover inspects, picks up and carries. Never send the rover to search a "
        "whole field.",
        *([DOCK_RULE] if dock_mode else []),
        *([OVERHEAD_RULE] if overhead else []),
        'A recovery goes in this order: drone_search the field named in the mission, drone_mark "$finding.f1", '
        'rover_goto "$finding.f1", drone_land, rover_search for the object, rover_inspect. If the mission only asks '
        "to look, check or find, stop there and report.",
        'Detections get ids f1, f2, ... They do not exist yet when you plan: write "$finding.f1". Use "$zone.<name>" '
        'for named zones (field_a, field_b, base, disposal) and "$home" for the start position.',
        "Never pick anything up without rover_inspect first. A hostile or unknown drone is hazardous: ask the "
        'operator with request_authorization (finding_id "f1") before the pick-up. The gripper refuses without it.',
        "The pick-up sequence is: rover_arm pick, rover_grip open, rover_approach, rover_grip close, rover_arm carry, "
        'rover_check_grip, then rover_goto "$zone.disposal" for a hostile drone or "$home" for one of ours, then '
        "rover_grip open and rover_arm home. One item per trip. Something that is not a drone is reported, not picked up.",
        "Use only the actions listed below, with the argument names shown. Leave unused fields null.",
        "3 to 20 steps, ending with a report step for the human. Each step's \"why\" is one short sentence a "
        "non-technical person understands.",
    ]
    return HEADER + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)) + "\n"


@dataclass
class PlanResult:
    drafts: list              # StepDrafts: the controller assigns step ids when it commits the plan
    fallback: bool
    summary: str = ""


def drafts_to_steps(store, drafts: list) -> list:
    return [Step(id=store.next_step_id(), robot=d.robot, action=d.action, args=d.args(), why=d.why)
            for d in drafts]


def step_to_draft(step: Step) -> StepDraft:
    return StepDraft(robot=step.robot, action=step.action, why=step.why, **step.args)


def validate_drafts(drafts: list, *, drone_out: bool = False, dock_mode: bool = False) -> list:
    """Static checks on LLM output. Returns a list of human-readable problems (empty = fine).
    Without docking there is no 'rover must wait for the drone' rule; the separation check happens at run time."""
    problems = []
    if not drafts:
        return ["the plan has no steps"]
    if len(drafts) > MAX_STEPS:
        problems.append(f"too many steps ({len(drafts)} > {MAX_STEPS})")
    for i, d in enumerate(drafts, 1):
        if (msg := validate_step(d.action, d.robot, d.args())):
            problems.append(f"step {i}: {msg}")
            continue
        if d.action in DRONE_LAUNCH:
            drone_out = True
        elif d.action in DRONE_RECOVER:
            drone_out = False
        elif dock_mode and d.action in ROVER_MOTION and drone_out:
            problems.append(f"step {i}: {d.action} while the drone is still in the air (dock it first)")
    return problems


def arena_text(arena: Arena) -> str:
    b, c = arena.bounds, arena.drone_cage
    zones = "\n".join(f"  - {n}: x {r.x0}..{r.x1}, y {r.y0}..{r.y1}" for n, r in arena.zones.items())
    return (f"ARENA (metres, Lighthouse frame): x {b.x0}..{b.x1}, y {b.y0}..{b.y1}; the drone may only fly in "
            f"x {c.x0}..{c.x1}, y {c.y0}..{c.y1}; home = ({arena.home.x}, {arena.home.y}).\nNamed zones:\n{zones}")


def compact_plan(steps: list) -> str:
    return json.dumps([{"action": s.action, "args": s.args, "status": s.status} for s in steps])


class Planner:
    def __init__(self, llm, arena: Arena, dock_mode: bool = False, overhead: bool = False):
        self.llm, self.arena, self.dock_mode, self.overhead = llm, arena, dock_mode, overhead

    def fallback(self, text: str) -> PlanResult:
        return PlanResult(demo_plan(text, self.arena, self.dock_mode), True,
                          "Using the built-in demo plan (the AI planner was not available).")

    def plan(self, text: str, *, previous: Optional[list] = None, change: Optional[str] = None,
             robots: Optional[dict] = None) -> PlanResult:
        if self.llm is None:
            return self.fallback(text)
        prompt = self._prompt(text, previous, change, robots)
        problems: list = []
        for attempt in range(2):
            try:
                draft = self.llm.generate(prompt + self._retry_note(problems), PlanDraft, tag="plan")
            except LLMError as e:
                log.warning("planner LLM failed: %s", e)
                break
            problems = validate_drafts(draft.steps, dock_mode=self.dock_mode)
            if not problems:
                return PlanResult(draft.steps, False, draft.summary)
            log.warning("plan rejected (attempt %d): %s", attempt + 1, problems)
        return self.fallback(text)

    def _prompt(self, text, previous, change, robots) -> str:
        parts = [rules_text(self.dock_mode, self.overhead), arena_text(self.arena), "ROBOT STATUS\n" + json.dumps(robots or {}),
                 "ACTIONS\n" + vocabulary_text(),
                 'Reply with JSON {"summary": <one sentence>, "steps": [...]}. Each step: robot, action, why, '
                 "and only the argument fields that action documents.",
                 f"MISSION\n{text}"]
        if previous and change:
            parts.append(f"The human saw this plan:\n{compact_plan(previous)}\nand asked for this change: {change}\n"
                         "Write the complete revised plan.")
        return "\n\n".join(parts)

    @staticmethod
    def _retry_note(problems: list) -> str:
        if not problems:
            return ""
        return "\n\nYour previous answer was rejected for these reasons; fix them:\n- " + "\n- ".join(problems)
